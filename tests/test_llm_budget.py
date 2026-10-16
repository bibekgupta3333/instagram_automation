import httpx
import pytest

from studio.llm.budget import (
    BudgetConfig,
    BudgetDeferred,
    BudgetGuard,
    Ledger,
    TokenBucket,
    cost_usd,
)
from studio.llm.client import OpenAICompatClient


@pytest.fixture
def guard(tmp_path):
    return BudgetGuard(
        BudgetConfig(monthly_cap_usd=25.0, per_channel_usd=2.0), Ledger(tmp_path / "ledger.sqlite")
    )


def spend(guard, channel, usd):
    guard.ledger.record(
        channel=channel, job_id="j", step="polish", route="bedrock", model="sonnet-5-5", usd=usd
    )


def test_ledger_default_path(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("STUDIO_LEDGER_PATH", raising=False)
    led = Ledger()
    assert led.path.as_posix() == "output/ledger.sqlite"
    assert (tmp_path / "output/ledger.sqlite").exists()


def test_quality_role_deferred_when_monthly_cap_hit(guard):
    spend(guard, "c01", 24.999)
    with pytest.raises(BudgetDeferred) as ei:
        guard.admit(
            role="polish", channel="c02", model_key="sonnet-5-5", est_in=10_000, max_out=4000
        )
    assert "monthly" in ei.value.reason


def test_channel_cap_then_shared_pool(guard):
    spend(guard, "c01", 2.0)  # own $2 cap used up; $5 shared pool still free
    guard.admit(role="polish", channel="c01", model_key="sonnet-5-5", est_in=1000, max_out=1000)
    spend(guard, "c03", 7.0)  # c03 drains the shared pool
    with pytest.raises(BudgetDeferred, match="shared pool"):
        guard.admit(role="polish", channel="c01", model_key="sonnet-5-5", est_in=1000, max_out=1000)


def test_accuracy_critical_weight(guard):
    assert guard.channel_cap("c05", accuracy_critical=True) == pytest.approx(3.0)


def test_token_bucket_defers_with_retry_after():
    t = [0.0]
    b = TokenBucket(60_000, clock=lambda: t[0])
    assert b.try_take(50_000) == 0.0
    wait = b.try_take(20_000)
    assert wait == pytest.approx(10.0)
    t[0] += 10
    assert b.try_take(20_000) == 0.0


def test_quality_deferred_by_bucket(guard):
    with pytest.raises(BudgetDeferred) as ei:
        guard.admit(
            role="fact_check", channel="c01", model_key="opus-5-5", est_in=90_000, max_out=20_000
        )
    assert ei.value.retry_after and ei.value.retry_after > 0


def _client(guard, handler):
    return OpenAICompatClient(
        "http://gw.test",
        transport=httpx.MockTransport(handler),
        budget=guard,
        sleep=lambda s: None,
        max_retries=1,
    )


def test_quality_role_queues_instead_of_local_fallback(guard):
    spend(guard, "c01", 25.0)
    calls = []
    c = _client(guard, lambda req: calls.append(req) or httpx.Response(500))
    with pytest.raises(BudgetDeferred):
        c.complete([{"role": "user", "content": "x"}], role="polish")
    assert calls == []  # never sent anywhere, not even to local


def test_defaults_cover_bedrock_bulk_work(monkeypatch):
    for k in ("STUDIO_BEDROCK_MONTHLY_USD", "STUDIO_BEDROCK_CHANNEL_USD"):
        monkeypatch.delenv(k, raising=False)
    cfg = BudgetConfig.from_env()
    assert cfg.monthly_cap_usd == 30.0 and cfg.per_channel_usd == 2.5
    assert cfg.per_channel_usd * 10 + cfg.shared_pool_usd == cfg.monthly_cap_usd


def test_haiku_draft_cost_and_long_context_multiplier():
    assert cost_usd("haiku-5-5", {"inputTokens": 4000, "outputTokens": 1000}) == pytest.approx(
        0.0009
    )
    big = {"inputTokens": 150_000, "outputTokens": 1000}
    assert cost_usd("haiku-5-5", big) == pytest.approx(5 * (150_000 * 0.10 + 1000 * 0.5) / 1e6)
    assert cost_usd("sonnet-5-5", big) == pytest.approx((150_000 * 2 + 1000 * 10) / 1e6)


def test_gateway_draft_goes_to_haiku_and_is_billed(guard):
    import json

    models = []

    def handler(req):
        models.append(json.loads(req.content)["model"])
        return httpx.Response(
            200,
            json={
                "model": "bedrock-haiku",
                "choices": [{"message": {"content": "drafted"}}],
                "usage": {"prompt_tokens": 4000, "completion_tokens": 1000},
            },
        )

    c = _client(guard, handler).bind(channel="c02", job_id="j9")
    assert c.complete([{"role": "user", "content": "x"}], role="draft") == "drafted"
    assert models == ["bedrock-haiku"]
    assert guard.ledger.month_to_date()["c02"] == pytest.approx(0.0009)


def test_bulk_role_queues_when_over_budget(guard):
    spend(guard, "c09", 25.0)
    calls = []
    c = _client(guard, lambda req: calls.append(req) or httpx.Response(200))
    with pytest.raises(BudgetDeferred, match="monthly"):
        c.complete([{"role": "user", "content": "x"}], role="metadata")
    assert calls == []


def test_bulk_role_throttled_defers(guard):
    c = _client(guard, lambda req: httpx.Response(429))
    with pytest.raises(BudgetDeferred, match="Bedrock unavailable"):
        c.complete([{"role": "user", "content": "x"}], role="draft")
