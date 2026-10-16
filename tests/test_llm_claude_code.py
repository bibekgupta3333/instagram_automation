import json
import subprocess
from pathlib import Path

import pytest

from studio.contracts import ChatClient
from studio.llm.bedrock import SchemaError
from studio.llm.budget import BudgetDeferred
from studio.llm.claude_code import ClaudeCodeClient, ClaudeCodeError

SCHEMA = {
    "type": "object",
    "required": ["ok"],
    "properties": {"ok": {"type": "boolean"}},
    "additionalProperties": False,
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in (
        "STUDIO_CLAUDE_CODE_MODEL_DRAFT",
        "STUDIO_CLAUDE_CODE_MODEL_POLISH",
        "STUDIO_CLAUDE_CODE_EFFORT",
        "STUDIO_CLAUDE_CODE_TIMEOUT",
    ):
        monkeypatch.delenv(k, raising=False)


def result(**kw):
    body = {"type": "result", "subtype": "success", "is_error": False, "result": "", **kw}
    return json.dumps(body)


class FakeRunner:
    def __init__(self, *stdouts, returncode=0, exc=None):
        self.stdouts = list(stdouts)
        self.calls: list[tuple[list[str], dict]] = []
        self.returncode = returncode
        self.exc = exc

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(argv, self.returncode, self.stdouts.pop(0), "")


def client(runner, **kw):
    return ClaudeCodeClient(binary="/opt/homebrew/bin/claude", runner=runner, **kw)


def test_argv_matches_verified_command():
    run = FakeRunner(result(result='{"ok":true}', structured_output={"ok": True}))
    c: ChatClient = client(run)
    out = c.complete(
        [{"role": "system", "content": "be terse"}, {"role": "user", "content": "say ok"}],
        role="draft",
        json_schema=SCHEMA,
    )
    assert json.loads(out) == {"ok": True}
    argv, kw = run.calls[0]
    assert argv == [
        "/opt/homebrew/bin/claude",
        "-p",
        "say ok",  # prompt right after -p (--tools is variadic)
        "--model",
        "haiku",
        "--output-format",
        "json",
        "--tools",
        "",
        "--json-schema",
        json.dumps(SCHEMA, separators=(",", ":")),
        "--max-turns",
        "3",
        "--permission-mode",
        "dontAsk",
        "--no-session-persistence",
        "--safe-mode",
        "--strict-mcp-config",
        "--system-prompt",
        "be terse",
    ]
    assert kw["timeout"] == 300.0 and kw["capture_output"] and kw["stdin"] == subprocess.DEVNULL
    assert Path(kw["cwd"]).name.startswith("studio-cc-")  # throwaway dir, not the repo


def test_models_per_role_and_escalation(monkeypatch):
    run = FakeRunner(*[result(result="x")] * 4)
    c = client(run)
    c.complete([{"role": "user", "content": "p"}], role="polish")
    c.complete([{"role": "user", "content": "p"}], role="fact_check", escalate=True)
    monkeypatch.setenv("STUDIO_CLAUDE_CODE_MODEL_DRAFT", "claude-haiku-5-5")
    c.complete([{"role": "user", "content": "p"}], role="draft")
    models = [a[a.index("--model") + 1] for a, _ in run.calls]
    assert models == ["sonnet", "opus", "claude-haiku-5-5"]
    assert "--json-schema" not in run.calls[0][0]


def test_text_result_and_fenced_json_fallback():
    run = FakeRunner(result(result="plain text"), result(result='```json\n{"ok": false}\n```'))
    c = client(run)
    assert c.complete([{"role": "user", "content": "p"}], role="draft") == "plain text"
    out = c.complete([{"role": "user", "content": "p"}], role="draft", json_schema=SCHEMA)
    assert json.loads(out) == {"ok": False}


def test_invalid_json_gets_one_retry():
    run = FakeRunner(result(result="sure! ok"), result(result='{"ok": true}'))
    out = client(run).complete(
        [{"role": "user", "content": "p"}], role="metadata", json_schema=SCHEMA
    )
    assert json.loads(out) == {"ok": True}
    assert len(run.calls) == 2
    retry_prompt = run.calls[1][0][2]
    assert "not valid JSON" in retry_prompt and "sure! ok" in retry_prompt


def test_invalid_json_twice_raises():
    run = FakeRunner(result(result="nope"), result(result='{"ok": "yes"}'))
    with pytest.raises(SchemaError):
        client(run).complete([{"role": "user", "content": "p"}], role="draft", json_schema=SCHEMA)
    assert len(run.calls) == 2


def test_multi_turn_is_flattened_and_dash_prompt_is_safe():
    run = FakeRunner(result(result="a"), result(result="b"))
    c = client(run)
    c.complete(
        [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"},
        ],
        role="draft",
    )
    p = run.calls[0][0][2]
    assert "[USER]\nq1" in p and "[ASSISTANT]\na1" in p and p.rstrip().endswith("q2")
    c.complete([{"role": "user", "content": "--help"}], role="draft")
    assert not run.calls[1][0][2].startswith("-")


def test_timeout_defers():
    run = FakeRunner(exc=subprocess.TimeoutExpired("claude", 5))
    with pytest.raises(BudgetDeferred, match="timed out"):
        client(run, timeout=5).complete([{"role": "user", "content": "p"}], role="draft")


def test_usage_limit_defers_and_other_errors_raise():
    limited = result(is_error=True, subtype="error_during_execution", result="Usage limit reached")
    with pytest.raises(BudgetDeferred):
        client(FakeRunner(limited)).complete([{"role": "user", "content": "p"}], role="polish")
    bad = result(is_error=True, subtype="error_max_turns", result="")
    with pytest.raises(ClaudeCodeError, match="error_max_turns"):
        client(FakeRunner(bad)).complete([{"role": "user", "content": "p"}], role="polish")
    with pytest.raises(ClaudeCodeError, match="exited 1"):
        client(FakeRunner("garbage", returncode=1)).complete(
            [{"role": "user", "content": "p"}], role="polish"
        )
