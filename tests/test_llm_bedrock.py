import json
import sqlite3

import pytest

from studio.llm import bedrock as bedrock_mod
from studio.llm.bedrock import (
    VERDICT_SCHEMA,
    BedrockConverseClient,
    SchemaError,
    auth_mode,
    validate,
)
from studio.llm.budget import BudgetConfig, BudgetDeferred, BudgetGuard, Ledger


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    import os

    for k in list(os.environ):
        if k.startswith(("STUDIO_BEDROCK_MODEL_", "BEDROCK_")):
            monkeypatch.delenv(k)
    monkeypatch.delenv("STUDIO_BEDROCK_EFFORT", raising=False)


def converse_resp(content, usage=None):
    return {
        "output": {"message": {"role": "assistant", "content": content}},
        "stopReason": "end_turn",
        "usage": usage or {"inputTokens": 10, "outputTokens": 5},
    }


class FakeRuntime:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def converse(self, **kw):
        self.calls.append(kw)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def throttle():
    from botocore.exceptions import ClientError

    return ClientError(
        {
            "Error": {"Code": "ThrottlingException", "Message": "slow down"},
            "ResponseMetadata": {"HTTPStatusCode": 429},
        },
        "Converse",
    )


# --- auth ---------------------------------------------------------------------------------
def _isolate_aws(monkeypatch, tmp_path):
    """Never touch the real ~/.aws (employer profiles live there)."""
    empty = tmp_path / "aws_empty"
    empty.write_text("")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(empty))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(empty))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    for k in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_DEFAULT_PROFILE",
        "AWS_PROFILE",
        "AWS_BEARER_TOKEN_BEDROCK",
    ):
        monkeypatch.delenv(k, raising=False)


def test_bearer_token_sends_authorization_header(monkeypatch, tmp_path):
    """Real botocore request building; the HTTP send is intercepted (offline)."""
    pytest.importorskip("botocore")
    from botocore.awsrequest import AWSResponse

    _isolate_aws(monkeypatch, tmp_path)
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "bedrock-api-key-test")
    # Would raise ProfileNotFound if it were passed to boto3: proves the key path skips it.
    monkeypatch.setenv("AWS_PROFILE", "employer-profile-must-not-load")
    assert auth_mode() == "api-key"

    rt = bedrock_mod._session_client()
    seen = {}

    class Raw:
        def stream(self, **_):
            yield json.dumps(converse_resp([{"text": "hi"}])).encode()

    def before_send(request, **_):
        seen["url"] = request.url
        seen["headers"] = dict(request.headers)
        return AWSResponse(request.url, 200, {"content-type": "application/json"}, Raw())

    rt.meta.events.register("before-send.bedrock-runtime.Converse", before_send)
    out = BedrockConverseClient(runtime=rt, effort="").complete(
        [{"role": "user", "content": "hello"}], role="draft", max_tokens=16
    )
    assert out == "hi"
    auth = seen["headers"]["Authorization"]
    auth = auth.decode() if isinstance(auth, bytes) else auth
    assert auth == "Bearer bedrock-api-key-test"  # not SigV4
    assert seen["url"].startswith("https://bedrock-runtime.us-east-1.amazonaws.com/model/")
    assert "global.anthropic.claude-haiku-5-5" in seen["url"]


def test_profile_fallback_without_key(monkeypatch, tmp_path):
    pytest.importorskip("boto3")
    import boto3

    _isolate_aws(monkeypatch, tmp_path)
    monkeypatch.setenv("AWS_PROFILE", "personal-bedrock")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    assert auth_mode() == "profile"
    made = {}

    class FakeSession:
        def __init__(self, **kw):
            made["session"] = kw

        def client(self, name, **kw):
            made["client"] = (name, kw)
            return "rt"

    monkeypatch.setattr(boto3, "Session", FakeSession)
    assert bedrock_mod._session_client() == "rt"
    assert made["session"] == {"profile_name": "personal-bedrock", "region_name": "us-west-2"}
    assert made["client"][0] == "bedrock-runtime"
    # empty AWS_PROFILE -> default chain (profile_name=None)
    monkeypatch.setenv("AWS_PROFILE", "")
    bedrock_mod._session_client()
    assert made["session"]["profile_name"] is None
    # with a key, no profile is passed at all
    monkeypatch.setenv("AWS_PROFILE", "employer")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "k")
    bedrock_mod._session_client()
    assert "profile_name" not in made["session"]
    assert made["session"]["region_name"] == "us-west-2"
    assert made["session"]["botocore_session"].get_config_variable("profile") is None


def test_old_botocore_rejected_for_api_keys(monkeypatch, tmp_path):
    botocore = pytest.importorskip("botocore")
    _isolate_aws(monkeypatch, tmp_path)
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "k")
    monkeypatch.setattr(botocore, "__version__", "1.38.9")
    with pytest.raises(ImportError, match="1.39.0"):
        bedrock_mod._session_client()


def test_missing_boto3_is_explained(monkeypatch):
    import builtins

    real = builtins.__import__

    def fake(name, *a, **k):
        if name.startswith(("boto3", "botocore")):
            raise ImportError(name)
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)
    with pytest.raises(ImportError, match="extra bedrock"):
        _ = BedrockConverseClient().runtime


# --- retries / budget -----------------------------------------------------------------------
def test_throttling_is_retried_with_backoff():
    pytest.importorskip("botocore")
    rt = FakeRuntime(throttle(), throttle(), converse_resp([{"text": "ok"}]))
    sleeps = []
    c = BedrockConverseClient(runtime=rt, sleep=sleeps.append)
    assert c.complete([{"role": "user", "content": "x"}], role="polish") == "ok"
    assert len(rt.calls) == 3 and len(sleeps) == 2
    assert all(0 <= s <= 60 for s in sleeps)


def test_throttling_exhausted_defers_and_logs_throttle(tmp_path):
    pytest.importorskip("botocore")
    guard = BudgetGuard(BudgetConfig(), Ledger(tmp_path / "l.sqlite"))
    rt = FakeRuntime(*[throttle()] * 3)
    c = BedrockConverseClient(runtime=rt, budget=guard, sleep=lambda s: None, max_retries=2)
    with pytest.raises(BudgetDeferred) as ei:
        c.complete([{"role": "user", "content": "x"}], role="fact_check")
    assert ei.value.retry_after == 300
    rows = (
        sqlite3.connect(tmp_path / "l.sqlite").execute("SELECT outcome FROM llm_spend").fetchall()
    )
    assert rows == [("throttled",)]


def test_non_retryable_error_raises_immediately():
    pytest.importorskip("botocore")
    from botocore.exceptions import ClientError

    err = ClientError({"Error": {"Code": "ValidationException", "Message": "bad"}}, "Converse")
    rt = FakeRuntime(err)
    with pytest.raises(ClientError):
        BedrockConverseClient(runtime=rt, sleep=lambda s: None).complete(
            [{"role": "user", "content": "x"}], role="draft"
        )
    assert len(rt.calls) == 1


def test_haiku_draft_spend_recorded_in_ledger(tmp_path):
    guard = BudgetGuard(BudgetConfig(), Ledger(tmp_path / "l.sqlite"))
    rt = FakeRuntime(
        converse_resp([{"text": "draft"}], {"inputTokens": 4000, "outputTokens": 1000})
    )
    c = BedrockConverseClient(runtime=rt, budget=guard).bind(channel="c01", job_id="j1")
    c.complete([{"role": "user", "content": "x"}], role="draft", max_tokens=1000)
    assert rt.calls[0]["modelId"] == "global.anthropic.claude-haiku-5-5"
    assert rt.calls[0]["inferenceConfig"] == {"maxTokens": 1000}
    assert guard.ledger.month_to_date()["c01"] == pytest.approx((4000 * 0.10 + 1000 * 0.50) / 1e6)
    row = (
        sqlite3.connect(tmp_path / "l.sqlite")
        .execute("SELECT step, route, model, input_tokens, output_tokens, job_id FROM llm_spend")
        .fetchone()
    )
    assert row == ("draft", "bedrock", "haiku-5-5", 4000, 1000, "j1")


def test_draft_queues_when_over_budget(tmp_path):
    guard = BudgetGuard(BudgetConfig(monthly_cap_usd=1.0), Ledger(tmp_path / "l.sqlite"))
    guard.ledger.record(
        channel="c01", job_id="j", step="polish", route="bedrock", model="sonnet-5-5", usd=1.0
    )
    rt = FakeRuntime()
    with pytest.raises(BudgetDeferred, match="monthly"):
        BedrockConverseClient(runtime=rt, budget=guard).complete(
            [{"role": "user", "content": "x"}], role="draft"
        )
    assert rt.calls == []


# --- structured output ----------------------------------------------------------------------
SCHEMA = {"type": "object", "required": ["t"], "properties": {"t": {"type": "string"}}}


def test_complete_json_via_tool_with_auto_choice_and_cache_point():
    rt = FakeRuntime(converse_resp([{"toolUse": {"toolUseId": "1", "input": {"t": "x"}}}]))
    out = BedrockConverseClient(runtime=rt).complete(
        [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
        role="polish",
        json_schema=SCHEMA,
    )
    assert json.loads(out) == {"t": "x"}
    call = rt.calls[0]
    assert call["toolConfig"]["toolChoice"] == {"auto": {}}  # forced choice 400s on 5.5
    assert call["system"][-1] == {"cachePoint": {"type": "default"}}
    assert call["additionalModelRequestFields"] == {"output_config": {"effort": "low"}}


def test_invalid_tool_input_gets_one_repair_round():
    rt = FakeRuntime(
        converse_resp([{"toolUse": {"toolUseId": "tu1", "name": "submit", "input": {"x": 1}}}]),
        converse_resp([{"toolUse": {"toolUseId": "tu2", "name": "submit", "input": {"t": "ok"}}}]),
    )
    out = BedrockConverseClient(runtime=rt).complete(
        [{"role": "user", "content": "u"}], role="metadata", json_schema=SCHEMA
    )
    assert json.loads(out) == {"t": "ok"}
    msgs = rt.calls[1]["messages"]
    assert msgs[1]["role"] == "assistant"
    assert msgs[2]["content"][0]["toolResult"]["toolUseId"] == "tu1"
    assert msgs[2]["content"][0]["toolResult"]["status"] == "error"


def test_plain_text_json_accepted_and_double_failure_raises():
    rt = FakeRuntime(converse_resp([{"text": '{"t": "y"}'}]))
    out = BedrockConverseClient(runtime=rt).complete(
        [{"role": "user", "content": "u"}], role="draft", json_schema=SCHEMA
    )
    assert json.loads(out) == {"t": "y"}
    rt = FakeRuntime(converse_resp([{"text": "no"}]), converse_resp([{"text": "still no"}]))
    with pytest.raises(SchemaError):
        BedrockConverseClient(runtime=rt).complete(
            [{"role": "user", "content": "u"}], role="draft", json_schema=SCHEMA
        )
    assert len(rt.calls) == 2


# --- fact-check -----------------------------------------------------------------------------
class VerdictRuntime:
    def __init__(self, verdicts_by_model):
        self.verdicts_by_model = verdicts_by_model
        self.calls = []

    def converse(self, **kw):
        self.calls.append(kw)
        v = self.verdicts_by_model[kw["modelId"]]
        claims = [{"claim": "c", "verdict": v, "source_ids": ["s1"], "fix": ""}]
        return converse_resp(
            [{"toolUse": {"name": "submit_factcheck", "input": {"claims": claims}}}],
            {"inputTokens": 100, "outputTokens": 50},
        )


def test_fact_check_escalates_to_opus_on_dispute():
    rt = VerdictRuntime(
        {
            "global.anthropic.claude-sonnet-5-5": "contradicted",
            "global.anthropic.claude-opus-5-5": "supported",
        }
    )
    out = BedrockConverseClient(runtime=rt).fact_check("sys", "script", [{"id": "s1"}])
    assert out["escalated"] is True and out["model"].endswith("opus-5-5")
    assert [c["modelId"] for c in rt.calls] == [
        "global.anthropic.claude-sonnet-5-5",
        "global.anthropic.claude-opus-5-5",
    ]
    assert rt.calls[0]["toolConfig"]["toolChoice"] == {"auto": {}}
    assert rt.calls[0]["inferenceConfig"] == {"maxTokens": 4000}


def test_no_escalation_when_supported():
    rt = VerdictRuntime({"global.anthropic.claude-sonnet-5-5": "supported"})
    out = BedrockConverseClient(runtime=rt).fact_check("sys", "s", [])
    assert out["escalated"] is False and len(rt.calls) == 1


def test_validator_rejects_bad_verdict():
    with pytest.raises(SchemaError):
        validate(
            {"claims": [{"claim": "c", "verdict": "maybe", "source_ids": [], "fix": ""}]},
            VERDICT_SCHEMA,
        )
