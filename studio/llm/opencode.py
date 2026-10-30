"""opencode CLI backend (``STUDIO_LLM=opencode``): shells out to ``opencode run``.

Defaults to the free hosted model ``opencode/deepseek-v4.1-flash-free`` (override with
``STUDIO_OPENCODE_MODEL``, format ``provider/model``). The prompt text is sent to that hosted
model. No $ ledger and no ``BudgetDeferred``: failures raise ``OpenCodeError``.

Flags (checked with ``opencode run --help`` on opencode 1.19.4)::

    opencode run -m provider/model --format default "<message>"

The process runs in a throwaway empty temp dir (``cwd``) so it cannot read or edit the repo,
with no shell. opencode has no system-prompt flag, so the system text is folded into the
message. JSON output is requested in the prompt and parsed leniently, with one repair retry,
like ``claude_code.py``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from typing import Any

from studio.llm.bedrock import SchemaError, _loads_lenient, validate

log = logging.getLogger(__name__)

DEFAULT_MODEL = "opencode/deepseek-v4.1-flash-free"
DEFAULT_TIMEOUT = 300.0
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


class OpenCodeError(RuntimeError):
    pass


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text).replace("\r", "")


def _render(messages: list[dict[str, str]], json_schema: dict[str, Any] | None) -> str:
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    turns = [m for m in messages if m["role"] != "system"]
    if len(turns) == 1 and turns[0]["role"] == "user":
        body = turns[0]["content"]
    else:
        parts = [f"[{m['role'].upper()}]\n{m['content']}" for m in turns]
        body = (
            "Conversation so far (reply as the assistant to the last USER turn):\n\n"
            + "\n\n".join(parts)
        )
    out = []
    if system:
        out.append(f"[SYSTEM INSTRUCTIONS]\n{system}\n[END SYSTEM INSTRUCTIONS]")
    out.append(body)
    if json_schema is not None:
        out.append(
            "Reply with ONLY one JSON object (no prose, no code fences) that validates against "
            "this JSON Schema:\n" + json.dumps(json_schema, separators=(",", ":"))
        )
    prompt = "\n\n".join(out)
    # Never let the message start with '-' (it would be parsed as a CLI option).
    return " " + prompt if prompt.startswith("-") else prompt


class OpenCodeClient:
    """``ChatClient`` backed by the local ``opencode`` CLI (free hosted model)."""

    def __init__(
        self,
        *,
        binary: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
        runner: Callable[..., subprocess.CompletedProcess] | None = None,
    ):
        e = os.environ
        self.binary = (
            binary or e.get("STUDIO_OPENCODE_BIN") or shutil.which("opencode") or "opencode"
        )
        self.model = model or e.get("STUDIO_OPENCODE_MODEL") or DEFAULT_MODEL
        self.timeout = float(timeout or e.get("STUDIO_OPENCODE_TIMEOUT") or DEFAULT_TIMEOUT)
        self._runner = runner

    def bind(self, **_: Any) -> OpenCodeClient:
        return self

    def build_argv(self, prompt: str) -> list[str]:
        return [self.binary, "run", "-m", self.model, "--format", "default", prompt]

    def _invoke(self, prompt: str) -> str:
        run = self._runner or subprocess.run
        try:
            with tempfile.TemporaryDirectory(prefix="studio-oc-") as cwd:
                proc = run(
                    self.build_argv(prompt),
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                    stdin=subprocess.DEVNULL,
                )
        except subprocess.TimeoutExpired as exc:
            raise OpenCodeError(f"opencode run timed out after {self.timeout:.0f}s") from exc
        except FileNotFoundError as exc:
            raise OpenCodeError(
                f"opencode CLI not found at {self.binary!r}; set STUDIO_OPENCODE_BIN"
            ) from exc
        out = strip_ansi(proc.stdout or "").strip()
        if proc.returncode != 0:
            err = strip_ansi(proc.stderr or out or "").strip()[:400]
            raise OpenCodeError(f"opencode run exited {proc.returncode}: {err}")
        if not out:
            err = strip_ansi(proc.stderr or "").strip()[:400]
            raise OpenCodeError(f"opencode run returned empty output {err}".strip())
        return out

    @staticmethod
    def _parse(text: str, json_schema: dict[str, Any]) -> Any:
        try:
            data = _loads_lenient(text)
        except (json.JSONDecodeError, ValueError) as exc:
            raise SchemaError(f"not JSON: {exc}") from None
        validate(data, json_schema)
        return data

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        role: str,
        json_schema: dict[str, Any] | None = None,
        max_tokens: int = 2048,  # not settable via the CLI; kept for the protocol
        escalate: bool = False,
    ) -> str:
        prompt = _render(messages, json_schema)
        text = self._invoke(prompt)
        if json_schema is None:
            return text
        try:
            return json.dumps(self._parse(text, json_schema))
        except SchemaError as err:
            log.warning("role %s: opencode returned invalid JSON (%s); one retry", role, err)
            retry = (
                f"{prompt}\n\nYour previous reply was not valid JSON for the required schema "
                f"({err}). Reply with only the corrected JSON object.\nPrevious reply:\n"
                f"{text[:4000]}"
            )
            return json.dumps(self._parse(self._invoke(retry), json_schema))
