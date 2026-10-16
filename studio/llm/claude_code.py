"""Claude Code headless backend (``STUDIO_LLM=claude-code``): shells out to ``claude -p``.

**Billing:** this runs on the user's own Claude subscription (Pro/Max) usage limits through
the logged-in Claude Code CLI, not on Bedrock dollars, so it bypasses the ``BudgetGuard``
ledger. Heavy batch use will eat the same 5-hour/weekly allowance the user codes with. If
``ANTHROPIC_API_KEY`` is set in the environment, Claude Code bills that API key instead.
Hitting a usage limit raises ``BudgetDeferred`` (queue, don't degrade).

Flags (checked with ``claude --help`` on Claude Code 2.1.296, and with one real call)::

    claude -p "<prompt>" --model haiku --output-format json --tools "" \\
        --json-schema '<schema>' --max-turns 3 --permission-mode dontAsk \\
        --no-session-persistence --safe-mode --strict-mcp-config --system-prompt "<sys>"

- ``--tools ""`` turns off every built-in tool (no Bash/Edit/Write), and ``--permission-mode
  dontAsk`` denies anything that would prompt, so it can never edit files.
- ``--safe-mode`` drops CLAUDE.md, hooks, skills, plugins and MCP servers, and
  ``--strict-mcp-config`` (with no ``--mcp-config``) loads no MCP servers. The process also
  runs in a throwaway temp dir.
- ``--json-schema`` makes Claude Code return ``structured_output`` (validated) in the JSON
  result. That costs one internal tool turn, so ``--max-turns`` is 3 (the real call used 2).
- The prompt goes right after ``-p``. ``--tools <tools...>`` is variadic, so a trailing
  positional prompt could be swallowed by it.

Result JSON fields used: ``is_error``, ``subtype`` ("success"), ``result`` (text),
``structured_output``, ``api_error_status``, ``usage``.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from typing import Any

from studio.llm.bedrock import SchemaError, _loads_lenient, validate
from studio.llm.budget import BudgetDeferred
from studio.llm.routing import resolve

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 300.0
DEFAULT_MAX_TURNS = 3
RATE_LIMIT_HINTS = ("rate limit", "usage limit", "rate_limit", "overloaded", "limit reached")


class ClaudeCodeError(RuntimeError):
    pass


def _render_prompt(messages: list[dict[str, str]]) -> tuple[str, str]:
    """(system_prompt, prompt). Multi-turn history is flattened into a labelled transcript."""
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    turns = [m for m in messages if m["role"] != "system"]
    if len(turns) == 1 and turns[0]["role"] == "user":
        prompt = turns[0]["content"]
    else:
        parts = [f"[{m['role'].upper()}]\n{m['content']}" for m in turns]
        prompt = (
            "Conversation so far (reply as the assistant to the last USER turn):\n\n"
            + "\n\n".join(parts)
        )
    # Never let the prompt start with '-' (it would be parsed as a CLI option).
    if prompt.startswith("-"):
        prompt = " " + prompt
    return system, prompt


class ClaudeCodeClient:
    """``ChatClient`` backed by the local Claude Code CLI. Uses the user's Claude
    subscription limits (see module docstring). Model per role: haiku for bulk roles, sonnet
    for quality roles, opus on escalation; override with ``STUDIO_CLAUDE_CODE_MODEL_<ROLE>``."""

    def __init__(
        self,
        *,
        binary: str | None = None,
        timeout: float | None = None,
        max_turns: int = DEFAULT_MAX_TURNS,
        effort: str | None = None,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ):
        e = os.environ
        self.binary = binary or e.get("STUDIO_CLAUDE_BIN") or shutil.which("claude") or "claude"
        self.timeout = float(timeout or e.get("STUDIO_CLAUDE_CODE_TIMEOUT") or DEFAULT_TIMEOUT)
        self.max_turns = max_turns
        self.effort = effort if effort is not None else (e.get("STUDIO_CLAUDE_CODE_EFFORT") or None)
        self._run = runner

    def bind(self, **_: Any) -> ClaudeCodeClient:
        return self  # no ledger context: subscription usage is not metered in $ here

    def build_argv(
        self, prompt: str, *, model: str, system: str, json_schema: dict[str, Any] | None
    ) -> list[str]:
        argv = [
            self.binary,
            "-p",
            prompt,
            "--model",
            model,
            "--output-format",
            "json",
            "--tools",
            "",
        ]
        if json_schema is not None:
            argv += ["--json-schema", json.dumps(json_schema, separators=(",", ":"))]
        argv += [
            "--max-turns",
            str(self.max_turns),
            "--permission-mode",
            "dontAsk",
            "--no-session-persistence",
            "--safe-mode",
            "--strict-mcp-config",
        ]
        if self.effort:
            argv += ["--effort", self.effort]
        if system:
            argv += ["--system-prompt", system]
        return argv

    def _invoke(self, argv: list[str], role: str) -> dict[str, Any]:
        try:
            with tempfile.TemporaryDirectory(prefix="studio-cc-") as cwd:
                proc = self._run(
                    argv,
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                    stdin=subprocess.DEVNULL,
                )
        except subprocess.TimeoutExpired as exc:
            raise BudgetDeferred(
                f"claude -p timed out after {self.timeout:.0f}s", retry_after=60, role=role
            ) from exc
        except FileNotFoundError as exc:
            raise ClaudeCodeError(
                f"Claude Code CLI not found at {self.binary!r}; set STUDIO_CLAUDE_BIN"
            ) from exc
        out = (proc.stdout or "").strip()
        try:
            body = json.loads(out.splitlines()[-1] if out else "")
        except (json.JSONDecodeError, IndexError):
            body = None
        if not isinstance(body, dict):
            err = (proc.stderr or out or "").strip()[:400]
            if any(h in err.lower() for h in RATE_LIMIT_HINTS):
                raise BudgetDeferred(f"Claude usage limit: {err}", retry_after=900, role=role)
            raise ClaudeCodeError(f"claude -p exited {proc.returncode}: {err}")
        if body.get("is_error") or body.get("subtype") not in (None, "success"):
            msg = str(body.get("result") or body.get("subtype") or "error")[:400]
            if body.get("api_error_status") in (429, 529) or any(
                h in msg.lower() for h in RATE_LIMIT_HINTS
            ):
                raise BudgetDeferred(f"Claude usage limit: {msg}", retry_after=900, role=role)
            raise ClaudeCodeError(f"claude -p failed ({body.get('subtype')}): {msg}")
        return body

    @staticmethod
    def _parse(body: dict[str, Any], json_schema: dict[str, Any]) -> Any:
        data = body.get("structured_output")
        if data is None:
            try:
                data = _loads_lenient(str(body.get("result") or ""))
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
        route = resolve(role, escalate=escalate)
        model = route.model.claude_code_model or "sonnet"
        system, prompt = _render_prompt(messages)
        body = self._invoke(
            self.build_argv(prompt, model=model, system=system, json_schema=json_schema), role
        )
        if json_schema is None:
            return str(body.get("result") or "")
        try:
            return json.dumps(self._parse(body, json_schema))
        except SchemaError as err:
            log.warning("role %s: claude -p returned invalid JSON (%s); one retry", role, err)
            retry = (
                f"{prompt}\n\nYour previous reply was not valid JSON for the required schema "
                f"({err}). Reply with only the corrected JSON object.\nPrevious reply:\n"
                f"{str(body.get('result') or '')[:4000]}"
            )
            body = self._invoke(
                self.build_argv(retry, model=model, system=system, json_schema=json_schema),
                role,
            )
            return json.dumps(self._parse(body, json_schema))
