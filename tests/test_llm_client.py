import json

import httpx
import pytest

from studio.contracts import ChatClient
from studio.llm.budget import BudgetConfig, BudgetDeferred, BudgetGuard, Ledger
from studio.llm.client import (
    LLMError,
    LLMUnavailable,
    MockChatClient,
    OpenAICompatClient,
    build_payload,
    get_client,
)


def ok(content="hello", model="x", usage=None):
    return httpx.Response(
        200,
        json={
            "model": model,
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5},
        },
    )


def make(handler, **kw):
    sleeps: list[float] = []
    c = OpenAICompatClient(
        "http://gw.test", transport=httpx.MockTransport(handler), sleep=sleeps.append, **kw
    )
    return c, sleeps


def test_retries_on_429_then_succeeds():
    calls = []

    def handler(req):
        calls.append(req)
        if len(calls) < 3:
            return httpx.Response(429, headers={"Retry-After": "2"}, json={"error": "slow"})
        return ok("done")

    c, sleeps = make(handler)
    assert c.complete([{"role": "user", "content": "hi"}], role="draft") == "done"
    assert len(calls) == 3
    assert sleeps == [2.0, 2.0]  # honours Retry-After
    assert calls[0].url.path == "/v1/chat/completions"


def test_retries_5xx_with_backoff_and_gives_up():
    c, sleeps = make(lambda req: httpx.Response(503), max_retries=3, mode="ollama")
    with pytest.raises(LLMUnavailable):
        c.complete([{"role": "user", "content": "hi"}], role="draft")
    assert len(sleeps) == 3 and all(0 <= s <= 60 for s in sleeps)


def test_4xx_is_not_retried():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(400, json={"error": "bad"})

    c, _ = make(handler)
    with pytest.raises(LLMError):
        c.complete([{"role": "user", "content": "hi"}], role="draft")
    assert len(calls) == 1


def test_json_schema_payload_shape():
    schema = {"type": "object", "required": ["title"], "properties": {"title": {"type": "string"}}}
    seen = {}

    def handler(req):
        seen.update(json.loads(req.content))
        return ok(json.dumps({"title": "T"}))

    c, _ = make(handler)
    out = c.complete(
        [{"role": "user", "content": "x"}], role="metadata", json_schema=schema, max_tokens=300
    )
    assert json.loads(out) == {"title": "T"}
    assert seen["model"] == "bedrock-haiku"  # gateway alias: metadata runs on Haiku 5.5
    assert seen["max_tokens"] == 300
    assert seen["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "metadata", "schema": schema, "strict": True},
    }
    assert build_payload("m", [], json_schema=None, max_tokens=1).get("response_format") is None


def test_ollama_mode_uses_provider_tag_and_refuses_quality(monkeypatch):
    monkeypatch.delenv("STUDIO_LOCAL_MODEL", raising=False)
    seen = {}

    def handler(req):
        seen.update(json.loads(req.content))
        return ok()

    c, _ = make(handler, mode="ollama")
    c.complete([{"role": "user", "content": "x"}], role="draft")
    assert seen["model"] == "qwen3.5:9b"  # opt-in Ollama mode only
    with pytest.raises(LLMUnavailable, match="never fall back to local"):
        c.complete([{"role": "user", "content": "x"}], role="polish")


def test_invalid_json_gets_one_repair_retry():
    replies = iter(["not json", json.dumps({"a": 1})])
    c, _ = make(lambda req: ok(next(replies)))
    out = c.complete(
        [{"role": "user", "content": "x"}], role="draft", json_schema={"type": "object"}
    )
    assert json.loads(out) == {"a": 1}


def test_quality_role_records_spend_in_ledger(tmp_path):
    guard = BudgetGuard(BudgetConfig(), Ledger(tmp_path / "l.sqlite"))
    c, _ = make(
        lambda req: ok(usage={"prompt_tokens": 1000, "completion_tokens": 1000}), budget=guard
    )
    c.bind(channel="c01", job_id="j1").complete(
        [{"role": "user", "content": "x"}], role="polish", max_tokens=1000
    )
    mtd = guard.ledger.month_to_date()
    assert mtd["c01"] == pytest.approx((1000 * 2.0 + 1000 * 10.0) / 1e6)


def test_bedrock_outage_defers_quality_role(tmp_path):
    guard = BudgetGuard(BudgetConfig(), Ledger(tmp_path / "l.sqlite"))
    c, _ = make(lambda req: httpx.Response(529), budget=guard, max_retries=1)
    with pytest.raises(BudgetDeferred):
        c.complete([{"role": "user", "content": "x"}], role="fact_check")


@pytest.fixture
def no_llm_env(monkeypatch, tmp_path):
    for k in ("STUDIO_LLM", "LITELLM_BASE_URL", "OLLAMA_BASE_URL", "STUDIO_BUDGET"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("STUDIO_LEDGER_PATH", str(tmp_path / "ledger.sqlite"))
    return monkeypatch


def test_get_client_defaults_to_bedrock_with_budget(no_llm_env):
    from studio.llm.bedrock import BedrockConverseClient

    c = get_client()
    assert isinstance(c, BedrockConverseClient)
    assert isinstance(c.budget, BudgetGuard)  # guard on by default
    assert c._rt is None  # boto3 client is created lazily: no AWS access at construction
    no_llm_env.setenv("STUDIO_BUDGET", "off")
    assert get_client().budget is None


@pytest.mark.parametrize("value", ["claude-code", "CLAUDE-CODE", "claude_code"])
def test_get_client_claude_code(no_llm_env, value):
    from studio.llm.claude_code import ClaudeCodeClient

    no_llm_env.setenv("STUDIO_LLM", value)
    assert isinstance(get_client(), ClaudeCodeClient)


def test_get_client_gateway_ollama_mock_and_unknown(no_llm_env):
    no_llm_env.setenv("STUDIO_LLM", "mock")
    assert isinstance(get_client(), MockChatClient)
    no_llm_env.setenv("STUDIO_LLM", "gateway")
    no_llm_env.setenv("STUDIO_BUDGET", "off")
    c = get_client()
    assert isinstance(c, OpenAICompatClient) and c.mode == "gateway"
    assert c.base_url == "http://localhost:4000"
    no_llm_env.setenv("LITELLM_BASE_URL", "http://gw.test:4000")
    assert get_client().base_url == "http://gw.test:4000"
    # LITELLM_BASE_URL alone no longer switches the backend: bedrock stays the default
    no_llm_env.delenv("STUDIO_LLM")
    assert not isinstance(get_client(), OpenAICompatClient)
    no_llm_env.setenv("STUDIO_LLM", "ollama")
    no_llm_env.setenv("OLLAMA_BASE_URL", "http://ollama.test:11434")
    c = get_client()
    assert c.mode == "ollama" and c.base_url == "http://ollama.test:11434"
    no_llm_env.setenv("STUDIO_LLM", "local-llama")
    with pytest.raises(ValueError, match="unknown STUDIO_LLM"):
        get_client()


def test_mock_client_satisfies_protocol_and_schema():
    m: ChatClient = MockChatClient()
    schema = {
        "type": "object",
        "required": ["title", "beats"],
        "properties": {
            "title": {"type": "string"},
            "beats": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["text"],
                    "properties": {"text": {"type": "string"}},
                },
            },
        },
    }
    out = json.loads(
        m.complete([{"role": "user", "content": "x"}], role="draft", json_schema=schema)
    )
    assert out == {"title": "mock", "beats": [{"text": "mock"}]}
