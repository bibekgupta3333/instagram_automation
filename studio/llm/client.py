"""LLM client factory (``get_client``), OpenAI-compatible client, and the offline mock.

``get_client()`` picks the backend from ``STUDIO_LLM`` (default ``bedrock``; see its docstring).
The OpenAI-compatible client here talks to either
- the **LiteLLM proxy** (``STUDIO_LLM=gateway``; model = LiteLLM group alias such as
  ``bedrock-haiku`` / ``bedrock-sonnet``, see infra/litellm/config.yaml), or
- **Ollama directly** (``STUDIO_LLM=ollama``, opt-in; bulk roles only).

Retries 429/5xx/529 with exponential backoff + jitter and honours ``Retry-After`` (docs 15 §7.5).
Quality roles never silently fall back to local (docs 15 §7.1): if Bedrock is not reachable,
over budget or throttled they raise ``BudgetDeferred`` / ``LLMUnavailable``.
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from studio.contracts import ChatClient
from studio.llm.budget import BudgetDeferred, BudgetGuard, estimate_tokens
from studio.llm.routing import ModelSpec, Route, resolve

log = logging.getLogger(__name__)

RETRY_STATUS = frozenset({429, 500, 502, 503, 504, 529})
Mode = Literal["gateway", "ollama"]


class LLMError(RuntimeError):
    pass


class LLMUnavailable(LLMError):
    """The endpoint could not serve the request after retries."""


def _retry_after(resp: httpx.Response) -> float | None:
    v = resp.headers.get("retry-after")
    if not v:
        return None
    try:
        return max(0.0, float(v))
    except ValueError:
        return None


def build_payload(
    model: str,
    messages: list[dict[str, str]],
    *,
    json_schema: dict[str, Any] | None,
    max_tokens: int,
    schema_name: str = "response",
    temperature: float | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": max_tokens}
    if temperature is not None:
        payload["temperature"] = temperature
    if json_schema is not None:
        # OpenAI structured-output shape. Ollama >= 0.5 and LiteLLM accept it. For Bedrock
        # Claude 5.5 (no native structured outputs, docs 15 §4) LiteLLM maps it to a tool call;
        # we additionally parse the JSON locally and do one repair retry.
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "schema": json_schema, "strict": True},
        }
    return payload


@dataclass
class CallContext:
    channel: str = "shared"
    job_id: str = "-"
    accuracy_critical: bool = False


class OpenAICompatClient:
    def __init__(
        self,
        base_url: str,
        *,
        mode: Mode = "gateway",
        api_key: str | None = None,
        budget: BudgetGuard | None = None,
        context: CallContext | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 600.0,
        max_retries: int = 4,
        sleep: Callable[[float], None] = time.sleep,
        allow_local_quality: bool = False,
    ):
        self.base_url = base_url.rstrip("/")
        self.mode = mode
        self.budget = budget
        self.context = context or CallContext()
        self.max_retries = max_retries
        self.sleep = sleep
        self.allow_local_quality = allow_local_quality
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._http = httpx.Client(
            base_url=self.base_url, headers=headers, timeout=timeout, transport=transport
        )

    # -- context ----------------------------------------------------------------------------
    def bind(
        self,
        *,
        channel: str | None = None,
        job_id: str | None = None,
        accuracy_critical: bool | None = None,
    ) -> OpenAICompatClient:
        """Return a client sharing the connection pool but with a per-job ledger context."""
        clone = object.__new__(OpenAICompatClient)
        clone.__dict__.update(self.__dict__)
        c = self.context
        clone.context = CallContext(
            channel=channel if channel is not None else c.channel,
            job_id=job_id if job_id is not None else c.job_id,
            accuracy_critical=(
                accuracy_critical if accuracy_critical is not None else c.accuracy_critical
            ),
        )
        return clone

    def close(self) -> None:
        self._http.close()

    # -- HTTP -------------------------------------------------------------------------------
    @property
    def _path(self) -> str:
        # LiteLLM serves both /chat/completions and /v1/chat/completions; Ollama needs /v1.
        return "/v1/chat/completions"

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        last: str = ""
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._http.post(self._path, json=payload)
            except httpx.TransportError as exc:  # connect error, timeout
                last = f"{type(exc).__name__}: {exc}"
                resp = None
            if resp is not None:
                if resp.status_code < 400:
                    return resp.json()
                last = f"HTTP {resp.status_code}: {resp.text[:300]}"
                if resp.status_code not in RETRY_STATUS:
                    raise LLMError(last)
            if attempt == self.max_retries:
                break
            ra = _retry_after(resp) if resp is not None else None
            delay = ra if ra is not None else min(60.0, 2.0**attempt) * random.random()
            log.warning("LLM call failed (%s); retry %d in %.1fs", last, attempt + 1, delay)
            self.sleep(delay)
        raise LLMUnavailable(f"gave up after {self.max_retries + 1} attempts: {last}")

    @staticmethod
    def _text(body: dict[str, Any]) -> str:
        try:
            msg = body["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"unexpected response shape: {str(body)[:300]}") from exc
        content = msg.get("content")
        if not content and msg.get("tool_calls"):  # LiteLLM json-via-tool on Bedrock
            content = msg["tool_calls"][0]["function"]["arguments"]
        return content or ""

    def _model_name(self, spec: ModelSpec) -> str:
        return spec.gateway_model if self.mode == "gateway" else spec.provider_model

    def _call(
        self, spec: ModelSpec, messages, *, role: str, json_schema, max_tokens: int
    ) -> tuple[str, dict, str]:
        payload = build_payload(
            self._model_name(spec),
            messages,
            json_schema=json_schema,
            max_tokens=max_tokens,
            schema_name=role,
        )
        body = self._post(payload)
        text = self._text(body)
        if json_schema is not None:
            try:
                json.loads(text)
            except json.JSONDecodeError:
                log.warning("role %s returned invalid JSON; one repair retry", role)
                repair = [
                    *messages,
                    {"role": "assistant", "content": text},
                    {
                        "role": "user",
                        "content": "That was not valid JSON for the schema. "
                        "Reply with only the corrected JSON object.",
                    },
                ]
                body = self._post({**payload, "messages": repair})
                text = self._text(body)
                json.loads(text)  # raises if still invalid
        return text, body.get("usage") or {}, str(body.get("model") or "")

    # -- ChatClient -------------------------------------------------------------------------
    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        role: str,
        json_schema: dict[str, Any] | None = None,
        max_tokens: int = 2048,
        escalate: bool = False,
    ) -> str:
        route = resolve(role, escalate=escalate)
        if self.mode == "ollama":
            return self._complete_local(route, messages, json_schema, max_tokens)
        return self._complete_bedrock(route, route.model, messages, json_schema, max_tokens)

    def _complete_local(self, route: Route, messages, json_schema, max_tokens) -> str:
        """Opt-in Ollama mode (STUDIO_LLM=ollama): bulk roles only."""
        ctx = self.context
        spec = route.local
        if spec is None:
            if not self.allow_local_quality:
                raise LLMUnavailable(
                    f"role {route.role!r} needs Bedrock ({route.model.provider_model}) but "
                    "STUDIO_LLM=ollama; quality steps never fall back to local."
                )
            # Dev-only escape hatch (STUDIO_LLM_LOCAL_QUALITY=1). Logged loudly.
            log.warning("role %s served LOCALLY: not production quality", route.role)
            spec = resolve("draft").local
            assert spec is not None
        text, usage, _ = self._call(
            spec, messages, role=route.role, json_schema=json_schema, max_tokens=max_tokens
        )
        if self.budget is not None:
            self.budget.record_local(
                role=route.role, channel=ctx.channel, job_id=ctx.job_id, usage=usage
            )
        return text

    def _complete_bedrock(
        self, route: Route, spec: ModelSpec, messages, json_schema, max_tokens
    ) -> str:
        """Gateway mode: every role is a Bedrock model group behind LiteLLM."""
        ctx = self.context
        admission = None
        if self.budget is not None:
            admission = self.budget.admit(
                role=route.role,
                channel=ctx.channel,
                model_key=spec.key,
                est_in=estimate_tokens(messages),
                max_out=max_tokens,
                accuracy_critical=ctx.accuracy_critical,
            )
        try:
            text, usage, _ = self._call(
                spec, messages, role=route.role, json_schema=json_schema, max_tokens=max_tokens
            )
        except LLMUnavailable as exc:
            if self.budget is not None and admission is not None:
                self.budget.settle(
                    admission,
                    {},
                    role=route.role,
                    channel=ctx.channel,
                    job_id=ctx.job_id,
                    outcome="throttled",
                )
            # queue, don't degrade (docs 15 §7.1)
            raise BudgetDeferred(
                f"Bedrock unavailable: {exc}", retry_after=300, role=route.role
            ) from exc
        if self.budget is not None and admission is not None:
            self.budget.settle(
                admission, usage, role=route.role, channel=ctx.channel, job_id=ctx.job_id
            )
        return text


# --- Mock -------------------------------------------------------------------------------------
def _sample(schema: dict[str, Any]) -> Any:
    """Minimal instance of a JSON schema (enough for pipeline tests)."""
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        return schema["enum"][0]
    for key in ("anyOf", "oneOf"):
        if key in schema:
            return _sample(schema[key][0])
    if "allOf" in schema:
        out: dict[str, Any] = {}
        for s in schema["allOf"]:
            v = _sample(s)
            if isinstance(v, dict):
                out.update(v)
        return out
    t = schema.get("type", "object")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), "null")
    if t == "object":
        props = schema.get("properties", {})
        req = schema.get("required", list(props))
        return {k: _sample(props.get(k, {"type": "string"})) for k in req}
    if t == "array":
        n = max(1, schema.get("minItems", 1))
        return [_sample(schema.get("items", {"type": "string"})) for _ in range(n)]
    if t == "string":
        return "mock"
    if t == "integer":
        return int(schema.get("minimum", 1))
    if t == "number":
        return float(schema.get("minimum", 1.0))
    if t == "boolean":
        return True
    return None


class MockChatClient:
    """Offline ChatClient. ``responses`` maps role -> str or callable(messages) -> str."""

    def __init__(self, responses: dict[str, Any] | None = None):
        self.responses = responses or {}
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        role: str,
        json_schema: dict[str, Any] | None = None,
        max_tokens: int = 2048,
    ) -> str:
        resolve(role)  # unknown roles fail the same way as the real client
        self.calls.append(
            {
                "messages": messages,
                "role": role,
                "json_schema": json_schema,
                "max_tokens": max_tokens,
            }
        )
        r = self.responses.get(role)
        if callable(r):
            return r(messages)
        if r is not None:
            return r if isinstance(r, str) else json.dumps(r)
        if json_schema is not None:
            return json.dumps(_sample(json_schema))
        return f"[mock {role}] " + (messages[-1].get("content", "")[:80] if messages else "")


Backend = Literal["bedrock", "claude-code", "gateway", "ollama", "mock"]
BACKENDS: tuple[str, ...] = ("bedrock", "claude-code", "gateway", "ollama", "mock")


def get_client(
    *, budget: BudgetGuard | None = None, transport: httpx.BaseTransport | None = None
) -> ChatClient:
    """Factory from ``STUDIO_LLM`` (default ``bedrock``):

    - ``bedrock``     direct Converse (studio.llm.bedrock); API key in AWS_BEARER_TOKEN_BEDROCK,
                      else AWS_PROFILE / default chain. Budget guard on.
    - ``claude-code`` ``claude -p`` headless (studio.llm.claude_code); uses the user's Claude
                      subscription limits, no $ ledger.
    - ``gateway``     LiteLLM proxy at LITELLM_BASE_URL (default http://localhost:4000), which
                      fronts the same Bedrock models. Budget guard on.
    - ``ollama``      opt-in local Ollama at OLLAMA_BASE_URL, bulk roles only.
    - ``mock``        offline MockChatClient.

    ``STUDIO_BUDGET=off`` disables the budget guard."""
    e = os.environ
    backend = (e.get("STUDIO_LLM") or "bedrock").strip().lower()
    if backend in ("claude_code", "claudecode", "claude"):
        backend = "claude-code"
    if backend == "mock":
        return MockChatClient()

    def _guard() -> BudgetGuard | None:
        if budget is not None:
            return budget
        return BudgetGuard() if e.get("STUDIO_BUDGET", "on").lower() != "off" else None

    if backend == "bedrock":
        from studio.llm.bedrock import BedrockConverseClient

        return BedrockConverseClient(budget=_guard())
    if backend == "claude-code":
        from studio.llm.claude_code import ClaudeCodeClient

        return ClaudeCodeClient()
    if backend == "gateway":
        return OpenAICompatClient(
            e.get("LITELLM_BASE_URL") or "http://localhost:4000",
            mode="gateway",
            api_key=e.get("LITELLM_API_KEY") or None,
            budget=_guard(),
            transport=transport,
        )
    if backend == "ollama":
        return OpenAICompatClient(
            e.get("OLLAMA_BASE_URL") or "http://localhost:11434",
            mode="ollama",
            budget=budget,
            transport=transport,
            allow_local_quality=e.get("STUDIO_LLM_LOCAL_QUALITY") == "1",
        )
    raise ValueError(f"unknown STUDIO_LLM={backend!r}; expected one of {', '.join(BACKENDS)}")
