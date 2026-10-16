import json

import pytest

from studio.llm.bedrock import VERDICT_SCHEMA, BedrockConverseClient, SchemaError, validate


class FakeRuntime:
    def __init__(self, verdicts_by_model):
        self.verdicts_by_model = verdicts_by_model
        self.calls = []

    def converse(self, **kw):
        self.calls.append(kw)
        v = self.verdicts_by_model[kw["modelId"]]
        claims = [{"claim": "c", "verdict": v, "source_ids": ["s1"], "fix": ""}]
        return {
            "output": {
                "message": {
                    "content": [
                        {"toolUse": {"name": "submit_factcheck", "input": {"claims": claims}}}
                    ]
                }
            },
            "usage": {"inputTokens": 100, "outputTokens": 50},
        }


def test_fact_check_escalates_to_opus_on_dispute(monkeypatch):
    for k in ("BEDROCK_SONNET_MODEL_ID", "BEDROCK_OPUS_MODEL_ID"):
        monkeypatch.delenv(k, raising=False)
    rt = FakeRuntime(
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
    rt = FakeRuntime({"global.anthropic.claude-sonnet-5-5": "supported"})
    out = BedrockConverseClient(runtime=rt).fact_check("sys", "s", [])
    assert out["escalated"] is False and len(rt.calls) == 1


def test_validator_rejects_bad_verdict():
    with pytest.raises(SchemaError):
        validate(
            {"claims": [{"claim": "c", "verdict": "maybe", "source_ids": [], "fix": ""}]},
            VERDICT_SCHEMA,
        )


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


def test_complete_json_via_tool():
    class RT:
        def converse(self, **kw):
            assert kw["toolConfig"]["toolChoice"] == {"auto": {}}
            return {
                "output": {"message": {"content": [{"toolUse": {"input": {"t": "x"}}}]}},
                "usage": {},
            }

    out = BedrockConverseClient(runtime=RT()).complete(
        [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
        role="polish",
        json_schema={"type": "object", "required": ["t"], "properties": {"t": {"type": "string"}}},
    )
    assert json.loads(out) == {"t": "x"}
