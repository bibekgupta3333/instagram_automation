import json
import subprocess

import pytest

from studio.contracts import ChatClient
from studio.llm.client import get_client
from studio.llm.opencode import DEFAULT_MODEL, OpenCodeClient, OpenCodeError

SCHEMA = {
    "type": "object",
    "required": ["ok"],
    "properties": {"ok": {"type": "boolean"}},
    "additionalProperties": False,
}
MSG = [{"role": "system", "content": "be terse"}, {"role": "user", "content": "say ok"}]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("STUDIO_OPENCODE_MODEL", "STUDIO_OPENCODE_TIMEOUT", "STUDIO_OPENCODE_BIN"):
        monkeypatch.delenv(k, raising=False)


def fake_run(monkeypatch, *stdouts, returncode=0, stderr="", exc=None):
    calls = []
    outs = list(stdouts)

    def run(argv, **kw):
        calls.append((argv, kw))
        if exc:
            raise exc
        return subprocess.CompletedProcess(argv, returncode, outs.pop(0) if outs else "", stderr)

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def client(**kw):
    return OpenCodeClient(binary="/bin/opencode", **kw)


def test_success_argv_and_isolation(monkeypatch):
    calls = fake_run(monkeypatch, "OK\n")
    c: ChatClient = client()
    assert c.complete(MSG, role="draft") == "OK"
    argv, kw = calls[0]
    assert argv[:6] == ["/bin/opencode", "run", "-m", DEFAULT_MODEL, "--format", "default"]
    assert "be terse" in argv[-1] and "say ok" in argv[-1]
    assert "shell" not in kw and kw["timeout"] == 300.0
    assert kw["cwd"] not in ("", ".") and kw["stdin"] == subprocess.DEVNULL


def test_nonzero_exit(monkeypatch):
    fake_run(monkeypatch, "", returncode=1, stderr="boom")
    with pytest.raises(OpenCodeError, match="exited 1: boom"):
        client().complete(MSG, role="draft")


def test_empty_output(monkeypatch):
    fake_run(monkeypatch, "  \n")
    with pytest.raises(OpenCodeError, match="empty"):
        client().complete(MSG, role="draft")


def test_timeout(monkeypatch):
    fake_run(monkeypatch, exc=subprocess.TimeoutExpired("opencode", 5))
    with pytest.raises(OpenCodeError, match="timed out"):
        client(timeout=5).complete(MSG, role="draft")


def test_missing_binary(monkeypatch):
    fake_run(monkeypatch, exc=FileNotFoundError())
    with pytest.raises(OpenCodeError, match="not found"):
        client().complete(MSG, role="draft")


def test_ansi_stripped(monkeypatch):
    fake_run(monkeypatch, "\x1b[0m\x1b[1;32mHello\x1b[0m world\r\n")
    assert client().complete(MSG, role="draft") == "Hello world"


def test_env_model_override(monkeypatch):
    monkeypatch.setenv("STUDIO_OPENCODE_MODEL", "opencode/other-free")
    calls = fake_run(monkeypatch, "OK")
    OpenCodeClient(binary="/bin/opencode").complete(MSG, role="draft")
    assert calls[0][0][3] == "opencode/other-free"


def test_json_fenced_and_schema_in_prompt(monkeypatch):
    calls = fake_run(monkeypatch, 'Sure:\n```json\n{"ok": true}\n```')
    out = client().complete(MSG, role="draft", json_schema=SCHEMA)
    assert json.loads(out) == {"ok": True}
    assert '"required":["ok"]' in calls[0][0][-1]


def test_json_retry_then_success(monkeypatch):
    calls = fake_run(monkeypatch, "not json at all", '{"ok": false}')
    out = client().complete(MSG, role="draft", json_schema=SCHEMA)
    assert json.loads(out) == {"ok": False} and len(calls) == 2


def test_get_client_opencode(monkeypatch):
    monkeypatch.setenv("STUDIO_LLM", "opencode")
    assert isinstance(get_client(), OpenCodeClient)
