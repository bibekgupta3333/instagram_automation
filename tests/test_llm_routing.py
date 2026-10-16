from pathlib import Path

import pytest
import yaml

from studio.llm.routing import ROLES, UnknownRole, resolve, routing_table


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in (
        "STUDIO_LOCAL_MODEL",
        "BEDROCK_HAIKU_MODEL_ID",
        "BEDROCK_SONNET_MODEL_ID",
        "BEDROCK_OPUS_MODEL_ID",
    ):
        monkeypatch.delenv(k, raising=False)


@pytest.mark.parametrize("role", ["draft", "metadata", "ideation", "outline"])
def test_bulk_roles_are_local_qwen(role):
    r = resolve(role)
    assert r.lane == "local" and r.step_class == "bulk"
    assert r.model.provider_model == "qwen3.5:9b"
    assert r.model.gateway_model == "local-bulk"
    assert r.overflow is not None and r.overflow.key == "haiku-5-5"


def test_quality_roles():
    polish = resolve("polish")
    assert polish.step_class == "quality"
    assert polish.model.provider_model == "global.anthropic.claude-sonnet-5-5"
    assert resolve("overflow").model.provider_model == "global.anthropic.claude-haiku-5-5"
    fc = resolve("fact_check")
    assert fc.model.key == "sonnet-5-5" and fc.overflow is None
    assert fc.escalation.provider_model == "global.anthropic.claude-opus-5-5"
    esc = resolve("fact_check", escalate=True)
    assert esc.model.key == "opus-5-5" and esc.lane == "bedrock"


def test_quality_roles_never_overflow_to_local():
    for r in routing_table().values():
        if r.step_class == "quality":
            assert r.lane == "bedrock" and r.overflow is None


def test_model_ids_are_configurable(monkeypatch):
    monkeypatch.setenv("STUDIO_LOCAL_MODEL", "gemma4:12b")
    monkeypatch.setenv("BEDROCK_SONNET_MODEL_ID", "us.anthropic.claude-sonnet-5-5")
    assert resolve("draft").model.provider_model == "gemma4:12b"
    assert resolve("polish").model.provider_model == "us.anthropic.claude-sonnet-5-5"


def test_unknown_role():
    with pytest.raises(UnknownRole):
        resolve("nope")


def test_gateway_aliases_exist_in_litellm_config():
    cfg = yaml.safe_load((Path(__file__).parents[1] / "infra/litellm/config.yaml").read_text())
    names = {m["model_name"] for m in cfg["model_list"]}
    params = {m["model_name"]: m["litellm_params"] for m in cfg["model_list"]}
    for role in ROLES:
        r = resolve(role)
        assert r.model.gateway_model in names
        if r.lane == "bedrock":
            assert params[r.model.gateway_model]["model"].endswith(r.model.provider_model)
            assert params[r.model.gateway_model]["aws_profile_name"] == "os.environ/AWS_PROFILE"
    assert params["local-bulk"]["model"].endswith(resolve("draft").model.provider_model)
