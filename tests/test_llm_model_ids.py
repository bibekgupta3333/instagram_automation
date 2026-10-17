"""User-supplied Bedrock text model ids: precedence, empty = unset, pricing, non-Claude calls."""

import json
import logging
import os

import pytest

from studio.llm import bedrock as bedrock_mod
from studio.llm import routing
from studio.llm.bedrock import BedrockConverseClient, SchemaError, supports_tool_use
from studio.llm.budget import BudgetConfig, BudgetGuard, Ledger, cost_usd, load_prices
from studio.llm.routing import infer_price_key, price_key_for_id, resolve

HAIKU = "global.anthropic.claude-haiku-5-5"
SONNET = "global.anthropic.claude-sonnet-5-5"
OPUS = "global.anthropic.claude-opus-5-5"
SCHEMA = {"type": "object", "required": ["t"], "properties": {"t": {"type": "string"}}}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith(("BEDROCK_", "STUDIO_BEDROCK_", "STUDIO_CLAUDE_CODE_MODEL_")):
            monkeypatch.delenv(k)
    monkeypatch.setattr(routing, "_warned", set())
    monkeypatch.setattr(bedrock_mod, "_learned_no_tool_use", set())


def resp(content, usage=None):
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


# --- precedence / empty values --------------------------------------------------------------
def test_defaults_when_unset():
    assert resolve("draft").model.provider_model == HAIKU
    assert resolve("polish").model.provider_model == SONNET
    assert resolve("polish", escalate=True).model.provider_model == OPUS


def test_tier_vars_route_their_roles(monkeypatch):
    monkeypatch.setenv("BEDROCK_MODEL_ID_FAST", "us.anthropic.claude-haiku-5-5")
    monkeypatch.setenv("BEDROCK_MODEL_ID_QUALITY", "anthropic.claude-sonnet-5-5")
    arn = (
        "arn:aws:bedrock:us-east-1:123456789012:inference-profile/global.anthropic.claude-opus-5-5"
    )
    monkeypatch.setenv("BEDROCK_MODEL_ID_ESCALATION", arn)
    for role in ("draft", "metadata", "ideation", "polish_short"):
        assert resolve(role).model.provider_model == "us.anthropic.claude-haiku-5-5"
    assert resolve("fact_check").model.provider_model == "anthropic.claude-sonnet-5-5"
    esc = resolve("fact_check", escalate=True).model
    assert esc.provider_model == arn and esc.key == "opus-5-5" and esc.tier == "ESCALATION"


def test_precedence_role_over_tier_over_legacy_over_default(monkeypatch):
    monkeypatch.setenv("BEDROCK_HAIKU_MODEL_ID", "legacy.anthropic.claude-haiku-5-5")
    assert resolve("draft").model.provider_model == "legacy.anthropic.claude-haiku-5-5"
    monkeypatch.setenv("BEDROCK_MODEL_ID_FAST", "us.anthropic.claude-haiku-5-5")
    assert resolve("draft").model.provider_model == "us.anthropic.claude-haiku-5-5"
    monkeypatch.setenv("STUDIO_BEDROCK_MODEL_DRAFT", "apac.anthropic.claude-haiku-5-5")
    assert resolve("draft").model.provider_model == "apac.anthropic.claude-haiku-5-5"
    assert resolve("metadata").model.provider_model == "us.anthropic.claude-haiku-5-5"
    monkeypatch.setenv("BEDROCK_MODEL_ID_ESCALATION", "us.anthropic.claude-opus-5-5")
    monkeypatch.setenv("STUDIO_BEDROCK_MODEL_ESCALATION", "global.anthropic.claude-opus-5-5-x")
    assert resolve("polish", escalate=True).model.provider_model.endswith("opus-5-5-x")


@pytest.mark.parametrize("empty", ["", "   ", "# drafts, metadata, ideation (e.g. Haiku-class)"])
def test_empty_or_comment_only_counts_as_unset(monkeypatch, empty):
    for var in (
        "BEDROCK_MODEL_ID_FAST",
        "BEDROCK_MODEL_ID_QUALITY",
        "BEDROCK_MODEL_ID_ESCALATION",
        "STUDIO_BEDROCK_MODEL_DRAFT",
        "STUDIO_BEDROCK_MODEL_ESCALATION",
        "STUDIO_CLAUDE_CODE_MODEL_DRAFT",
        "BEDROCK_PRICE_IN_PER_MTOK_FAST",
    ):
        monkeypatch.setenv(var, empty)
    d = resolve("draft").model
    assert d.provider_model == HAIKU and d.key == "haiku-5-5" and d.claude_code_model == "haiku"
    assert resolve("fact_check").model.provider_model == SONNET
    assert resolve("fact_check", escalate=True).model.provider_model == OPUS


def test_trailing_inline_comment_is_dropped(monkeypatch):
    monkeypatch.setenv("BEDROCK_MODEL_ID_FAST", "amazon.nova-lite-v1:0   # cheap drafts")
    assert resolve("draft").model.provider_model == "amazon.nova-lite-v1:0"


# --- tier inference / pricing -----------------------------------------------------------------
@pytest.mark.parametrize(
    ("model_id", "key"),
    [
        ("anthropic.claude-haiku-5-5", "haiku-5-5"),
        ("us.anthropic.claude-sonnet-5-5", "sonnet-5-5"),
        (
            "arn:aws:bedrock:us-east-1:1:inference-profile/global.anthropic.claude-opus-5-5",
            "opus-5-5",
        ),
        ("us.amazon.nova-micro-v1:0", "haiku-5-5"),
        ("amazon.nova-lite-v1:0", "haiku-5-5"),
        ("us.amazon.nova-pro-v1:0", "sonnet-5-5"),
        ("us.meta.llama3-3-70b-instruct-v1:0", "sonnet-5-5"),
        ("mistral.ministral-3-8b-instruct", "haiku-5-5"),
        ("mistral.mistral-large-3", "sonnet-5-5"),
        ("cohere.command-r-plus-v1:0", "opus-5-5"),
        ("vendor.mystery-model-v1", None),
    ],
)
def test_infer_price_key(model_id, key):
    assert infer_price_key(model_id) == key


def test_custom_tier_ids_are_priced_by_family(monkeypatch):
    monkeypatch.setenv("BEDROCK_MODEL_ID_FAST", "us.amazon.nova-lite-v1:0")
    monkeypatch.setenv("BEDROCK_MODEL_ID_QUALITY", "us.meta.llama3-3-70b-instruct-v1:0")
    d, p = resolve("draft").model, resolve("polish").model
    assert (d.key, d.gateway_model, d.tier) == ("haiku-5-5", "bedrock-haiku", "FAST")
    assert (p.key, p.gateway_model, p.tier) == ("sonnet-5-5", "bedrock-sonnet", "QUALITY")


@pytest.mark.parametrize(
    ("var", "role", "escalate", "key"),
    [
        ("BEDROCK_MODEL_ID_FAST", "draft", False, "haiku-5-5"),
        ("BEDROCK_MODEL_ID_QUALITY", "polish", False, "sonnet-5-5"),
        ("BEDROCK_MODEL_ID_ESCALATION", "fact_check", True, "opus-5-5"),
    ],
)
def test_unknown_id_falls_back_to_variable_tier_with_one_warning(
    monkeypatch, caplog, var, role, escalate, key
):
    monkeypatch.setenv(var, "vendor.mystery-model-v1")
    with caplog.at_level(logging.WARNING, logger="studio.llm.routing"):
        for _ in range(3):
            spec = resolve(role, escalate=escalate).model
    assert spec.key == key and spec.provider_model == "vendor.mystery-model-v1"
    warnings = [r for r in caplog.records if "mystery-model" in r.getMessage()]
    assert len(warnings) == 1


def test_per_role_unknown_id_falls_back_to_role_tier():
    assert price_key_for_id("vendor.x", "QUALITY") == "sonnet-5-5"


def test_tier_price_override(monkeypatch, tmp_path):
    monkeypatch.setenv("BEDROCK_MODEL_ID_FAST", "vendor.mystery-model-v1")
    monkeypatch.setenv("BEDROCK_PRICE_IN_PER_MTOK_FAST", "0.06")
    monkeypatch.setenv("BEDROCK_PRICE_OUT_PER_MTOK_FAST", "0.24")
    spec = resolve("draft").model
    assert spec.key == "price-fast" and spec.gateway_model == "bedrock-haiku"
    prices = load_prices()
    assert prices["price-fast"][:2] == (0.06, 0.24)
    usage = {"inputTokens": 1_000_000, "outputTokens": 1_000_000}
    assert cost_usd("price-fast", usage, prices) == pytest.approx(0.30)
    # the per-role override is priced from its own id, not the FAST override
    monkeypatch.setenv("STUDIO_BEDROCK_MODEL_DRAFT", "us.anthropic.claude-sonnet-5-5")
    assert resolve("draft").model.key == "sonnet-5-5"
    assert resolve("metadata").model.key == "price-fast"


def test_half_price_override_keeps_tier_default(monkeypatch):
    monkeypatch.setenv("BEDROCK_PRICE_OUT_PER_MTOK_QUALITY", "1.5")
    assert load_prices()["price-quality"][:2] == (2.00, 1.5)
    assert resolve("polish").model.key == "price-quality"


def test_guard_bills_price_override_set_after_guard_creation(monkeypatch, tmp_path):
    guard = BudgetGuard(BudgetConfig(), Ledger(tmp_path / "l.sqlite"))
    monkeypatch.setenv("BEDROCK_MODEL_ID_FAST", "amazon.nova-micro-v1:0")
    monkeypatch.setenv("BEDROCK_PRICE_IN_PER_MTOK_FAST", "0.035")
    monkeypatch.setenv("BEDROCK_PRICE_OUT_PER_MTOK_FAST", "0.14")
    rt = FakeRuntime(resp([{"text": "d"}], {"inputTokens": 1_000_000, "outputTokens": 1_000_000}))
    BedrockConverseClient(runtime=rt, budget=guard).bind(channel="c1").complete(
        [{"role": "user", "content": "x"}], role="draft", max_tokens=10
    )
    assert guard.ledger.month_to_date()["c1"] == pytest.approx(0.175)


def test_empty_budget_numbers_use_defaults(monkeypatch):
    monkeypatch.setenv("STUDIO_BEDROCK_MONTHLY_USD", "")
    monkeypatch.setenv("STUDIO_BEDROCK_CHANNEL_USD", "")
    assert BudgetConfig.from_env().monthly_cap_usd == 30.0


# --- non-Anthropic requests -------------------------------------------------------------------
def test_non_anthropic_id_gets_no_anthropic_only_fields(monkeypatch):
    monkeypatch.setenv("BEDROCK_MODEL_ID_FAST", "us.meta.llama3-3-70b-instruct-v1:0")
    rt = FakeRuntime(resp([{"toolUse": {"toolUseId": "1", "input": {"t": "x"}}}]))
    out = BedrockConverseClient(runtime=rt, effort="low").complete(
        [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
        role="draft",
        json_schema=SCHEMA,
    )
    assert json.loads(out) == {"t": "x"}
    call = rt.calls[0]
    assert call["modelId"] == "us.meta.llama3-3-70b-instruct-v1:0"
    assert "additionalModelRequestFields" not in call  # no output_config.effort
    assert call["system"] == [{"text": "s"}]  # no cachePoint for a non-caching family
    assert call["toolConfig"]["toolChoice"] == {"auto": {}}


def test_anthropic_arn_keeps_effort_and_cache_point(monkeypatch):
    arn = "arn:aws:bedrock:us-east-1:1:inference-profile/us.anthropic.claude-haiku-5-5"
    monkeypatch.setenv("BEDROCK_MODEL_ID_FAST", arn)
    rt = FakeRuntime(resp([{"text": "ok"}]))
    BedrockConverseClient(runtime=rt, effort="low").complete(
        [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}], role="draft"
    )
    call = rt.calls[0]
    assert call["modelId"] == arn
    assert call["additionalModelRequestFields"] == {"output_config": {"effort": "low"}}
    assert call["system"][-1] == {"cachePoint": {"type": "default"}}


def test_nova_keeps_cache_point_but_not_effort(monkeypatch):
    monkeypatch.setenv("BEDROCK_MODEL_ID_FAST", "us.amazon.nova-lite-v1:0")
    rt = FakeRuntime(resp([{"text": "ok"}]))
    BedrockConverseClient(runtime=rt, effort="low").complete(
        [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}], role="draft"
    )
    assert "additionalModelRequestFields" not in rt.calls[0]
    assert rt.calls[0]["system"][-1] == {"cachePoint": {"type": "default"}}


# --- JSON without tool use ----------------------------------------------------------------------
def test_tool_use_support_table(monkeypatch):
    assert supports_tool_use("us.anthropic.claude-haiku-5-5")
    assert supports_tool_use("us.meta.llama3-1-70b-instruct-v1:0")
    assert not supports_tool_use("mistral.mistral-7b-instruct-v0:2")
    assert not supports_tool_use("meta.llama3-8b-instruct-v1:0")
    monkeypatch.setenv("STUDIO_BEDROCK_TOOL_USE", "off")
    assert not supports_tool_use("us.anthropic.claude-haiku-5-5")
    monkeypatch.setenv("STUDIO_BEDROCK_TOOL_USE", "on")
    assert supports_tool_use("mistral.mistral-7b-instruct-v0:2")


def test_no_tool_model_gets_prompted_json_with_local_validation(monkeypatch):
    monkeypatch.setenv("BEDROCK_MODEL_ID_FAST", "mistral.mistral-7b-instruct-v0:2")
    rt = FakeRuntime(resp([{"text": 'Sure! {"t": "hello"} Hope that helps.'}]))
    out = BedrockConverseClient(runtime=rt).complete(
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "u"}],
        role="metadata",
        json_schema=SCHEMA,
    )
    assert json.loads(out) == {"t": "hello"}
    call = rt.calls[0]
    assert "toolConfig" not in call
    assert "system" not in call  # Mistral 7B: system text folded into the user turn
    first = call["messages"][0]
    assert first["role"] == "user"
    assert first["content"][0]["text"].startswith("sys")
    assert '"required": ["t"]' in first["content"][0]["text"]  # schema is in the prompt
    assert first["content"][-1] == {"text": "u"}


def test_prompted_json_one_retry_then_raises(monkeypatch):
    monkeypatch.setenv("STUDIO_BEDROCK_TOOL_USE", "off")
    rt = FakeRuntime(resp([{"text": '{"x": 1}'}]), resp([{"text": '{"t": "fixed"}'}]))
    out = BedrockConverseClient(runtime=rt).complete(
        [{"role": "user", "content": "u"}], role="draft", json_schema=SCHEMA
    )
    assert json.loads(out) == {"t": "fixed"}
    repair = rt.calls[1]["messages"]
    assert repair[-2] == {"role": "assistant", "content": [{"text": '{"x": 1}'}]}
    assert "not valid" in repair[-1]["content"][0]["text"]
    rt = FakeRuntime(resp([{"text": "nope"}]), resp([{"text": "still nope"}]))
    with pytest.raises(SchemaError):
        BedrockConverseClient(runtime=rt).complete(
            [{"role": "user", "content": "u"}], role="draft", json_schema=SCHEMA
        )
    assert len(rt.calls) == 2


def test_runtime_no_tool_use_error_switches_to_prompted_json(monkeypatch):
    pytest.importorskip("botocore")
    from botocore.exceptions import ClientError

    model = "vendor.new-model-v1"
    monkeypatch.setenv("BEDROCK_MODEL_ID_FAST", model)
    err = ClientError(
        {
            "Error": {
                "Code": "ValidationException",
                "Message": "This model doesn't support tool use.",
            }
        },
        "Converse",
    )
    rt = FakeRuntime(err, resp([{"text": '{"t": "ok"}'}]), resp([{"text": '{"t": "again"}'}]))
    c = BedrockConverseClient(runtime=rt)
    out = c.complete([{"role": "user", "content": "u"}], role="draft", json_schema=SCHEMA)
    assert json.loads(out) == {"t": "ok"}
    assert "toolConfig" in rt.calls[0] and "toolConfig" not in rt.calls[1]
    # remembered: the next call goes straight to prompted JSON
    c.complete([{"role": "user", "content": "u"}], role="draft", json_schema=SCHEMA)
    assert len(rt.calls) == 3 and "toolConfig" not in rt.calls[2]


def test_fact_check_without_tool_use(monkeypatch):
    monkeypatch.setenv("BEDROCK_MODEL_ID_QUALITY", "mistral.mixtral-8x7b-instruct-v0:1")
    claims = [{"claim": "c", "verdict": "supported", "source_ids": ["s1"], "fix": ""}]
    rt = FakeRuntime(resp([{"text": json.dumps({"claims": claims})}]))
    out = BedrockConverseClient(runtime=rt).fact_check("sys", "script", [{"id": "s1"}])
    assert out["claims"] == claims and out["escalated"] is False
    assert out["model"] == "mistral.mixtral-8x7b-instruct-v0:1"
    assert "toolConfig" not in rt.calls[0]
