from pathlib import Path

import pytest
import yaml

from studio.llm.routing import ROLES, UnknownRole, resolve, routing_table

HAIKU = "global.anthropic.claude-haiku-5-5"
SONNET = "global.anthropic.claude-sonnet-5-5"
OPUS = "global.anthropic.claude-opus-5-5"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    import os

    for k in list(os.environ):
        if k.startswith(("STUDIO_BEDROCK_MODEL_", "STUDIO_CLAUDE_CODE_MODEL_")):
            monkeypatch.delenv(k)
    for k in (
        "STUDIO_LOCAL_MODEL",
        "BEDROCK_HAIKU_MODEL_ID",
        "BEDROCK_SONNET_MODEL_ID",
        "BEDROCK_OPUS_MODEL_ID",
    ):
        monkeypatch.delenv(k, raising=False)


@pytest.mark.parametrize("role", ["draft", "metadata", "ideation", "outline", "overflow"])
def test_bulk_roles_are_bedrock_haiku(role):
    r = resolve(role)
    assert r.lane == "bedrock" and r.step_class == "bulk"
    assert r.model.key == "haiku-5-5"
    assert r.model.provider_model == HAIKU
    assert r.model.gateway_model == "bedrock-haiku"
    assert r.model.claude_code_model == "haiku"
    # Ollama is an explicit opt-in: the local spec is only consulted with STUDIO_LLM=ollama
    assert r.local is not None and r.local.provider_model == "qwen3.5:9b"


@pytest.mark.parametrize("role", ["polish", "fact_check"])
def test_quality_roles_sonnet_with_opus_escalation(role):
    r = resolve(role)
    assert r.step_class == "quality" and r.lane == "bedrock" and r.local is None
    assert r.model.provider_model == SONNET and r.model.claude_code_model == "sonnet"
    assert r.escalation.provider_model == OPUS
    esc = resolve(role, escalate=True)
    assert esc.model.key == "opus-5-5" and esc.model.claude_code_model == "opus"
    assert esc.escalation is None


def test_every_role_routes_to_bedrock_and_quality_never_local():
    for r in routing_table().values():
        assert r.lane == "bedrock"
        assert r.model.key in {"haiku-5-5", "sonnet-5-5", "opus-5-5"}
        if r.step_class == "quality":
            assert r.local is None


def test_tier_ids_are_configurable(monkeypatch):
    monkeypatch.setenv("BEDROCK_SONNET_MODEL_ID", "us.anthropic.claude-sonnet-5-5")
    assert resolve("polish").model.provider_model == "us.anthropic.claude-sonnet-5-5"


def test_per_role_override(monkeypatch):
    monkeypatch.setenv("STUDIO_BEDROCK_MODEL_DRAFT", "us.anthropic.claude-sonnet-5-5")
    monkeypatch.setenv("STUDIO_CLAUDE_CODE_MODEL_DRAFT", "claude-sonnet-5-5")
    monkeypatch.setenv("STUDIO_BEDROCK_MODEL_ESCALATION", "us.anthropic.claude-opus-5-5")
    d = resolve("draft")
    assert d.model.provider_model == "us.anthropic.claude-sonnet-5-5"
    assert d.model.key == "sonnet-5-5"  # priced as the tier its id names
    assert d.model.gateway_model == "bedrock-sonnet"
    assert d.model.claude_code_model == "claude-sonnet-5-5"
    assert resolve("metadata").model.provider_model == HAIKU  # other roles untouched
    assert resolve("fact_check", escalate=True).model.provider_model == (
        "us.anthropic.claude-opus-5-5"
    )


def test_unknown_role():
    with pytest.raises(UnknownRole):
        resolve("nope")
    with pytest.raises(ValueError):
        resolve("draft", escalate=True)


def test_gateway_aliases_exist_in_litellm_config():
    cfg = yaml.safe_load((Path(__file__).parents[1] / "infra/litellm/config.yaml").read_text())
    params = {m["model_name"]: m["litellm_params"] for m in cfg["model_list"]}
    assert "local-bulk" not in params  # no Ollama default any more
    for role in ROLES:
        for r in (resolve(role),) + (
            (resolve(role, escalate=True),) if resolve(role).escalation else ()
        ):
            p = params[r.model.gateway_model]
            assert p["model"] == f"bedrock/converse/{r.model.provider_model}"
            assert p["api_key"] == "os.environ/AWS_BEARER_TOKEN_BEDROCK"
            assert p["aws_region_name"] == "os.environ/AWS_REGION"
