"""Role -> model routing policy (docs 15 §5, local model choice from docs 14).

Every LLM call in the pipeline names a *role* (the pipeline step). The table below maps the
role to a lane (local Ollama vs Amazon Bedrock), a step class (bulk vs quality) and the
LiteLLM model-group alias defined in ``infra/litellm/config.yaml``.

Rules (docs 15 §7.1):
- **bulk** roles run locally. If local is busy/down they MAY overflow to Bedrock Haiku 5.5,
  but only while budget headroom is above 30%.
- **quality** roles run on Bedrock. When Bedrock is throttled or over budget they QUEUE
  (``BudgetDeferred``); they never silently fall back to a local model.

All model identifiers are config values (env vars) so they can be changed without code edits.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Literal

Lane = Literal["local", "bedrock"]
StepClass = Literal["bulk", "quality"]


def _env(name: str, default: str) -> str:
    return os.environ.get(name) or default


# --- Model identifiers (config) ------------------------------------------------------------
# Local workhorse: docs 14 §3 recommends Qwen3.5-9B (Ollama tag `qwen3.5:9b`). Gemma 4 12B
# (`gemma4:12b`) is the A/B alternative. Override with STUDIO_LOCAL_MODEL.
LOCAL_MODEL_DEFAULT = "qwen3.5:9b"

# Bedrock global cross-Region inference profiles as documented in docs 15 §2.
# TODO-verify: confirm exact IDs with `aws bedrock list-inference-profiles` in Phase 0; docs 15
# lists them from the model cards but they have not been called from this account yet.
BEDROCK_HAIKU_DEFAULT = "global.anthropic.claude-haiku-5-5"
BEDROCK_SONNET_DEFAULT = "global.anthropic.claude-sonnet-5-5"
BEDROCK_OPUS_DEFAULT = "global.anthropic.claude-opus-5-5"


@dataclass(frozen=True)
class ModelSpec:
    key: str  # short key used by the budget/price table: "local", "haiku-5-5", ...
    lane: Lane
    gateway_model: str  # LiteLLM model_name (infra/litellm/config.yaml)
    provider_model: str  # Ollama tag or Bedrock model id / inference profile


def models() -> dict[str, ModelSpec]:
    """Model catalogue, resolved from env each call so tests/ops can override."""
    return {
        "local": ModelSpec(
            "local", "local", "local-bulk", _env("STUDIO_LOCAL_MODEL", LOCAL_MODEL_DEFAULT)
        ),
        "haiku-5-5": ModelSpec(
            "haiku-5-5",
            "bedrock",
            "bedrock-haiku",
            _env("BEDROCK_HAIKU_MODEL_ID", BEDROCK_HAIKU_DEFAULT),
        ),
        "sonnet-5-5": ModelSpec(
            "sonnet-5-5",
            "bedrock",
            "bedrock-sonnet",
            _env("BEDROCK_SONNET_MODEL_ID", BEDROCK_SONNET_DEFAULT),
        ),
        "opus-5-5": ModelSpec(
            "opus-5-5",
            "bedrock",
            "bedrock-opus",
            _env("BEDROCK_OPUS_MODEL_ID", BEDROCK_OPUS_DEFAULT),
        ),
    }


@dataclass(frozen=True)
class Route:
    role: str
    step_class: StepClass
    model: ModelSpec
    overflow: ModelSpec | None = None  # bulk only: used when local is busy and budget allows
    escalation: ModelSpec | None = None  # quality only: used on dispute (fact_check -> Opus)
    reason: str = ""

    @property
    def lane(self) -> Lane:
        return self.model.lane


# role -> (class, model key, overflow key, escalation key, reason). docs 15 §5.
_TABLE: dict[str, tuple[StepClass, str, str | None, str | None, str]] = {
    # bulk / local
    "ideation": ("bulk", "local", "haiku-5-5", None, "high volume, low stakes"),
    "outline": ("bulk", "local", "haiku-5-5", None, "templated, structural"),
    "draft": ("bulk", "local", "haiku-5-5", None, "bulk words; quality comes at polish"),
    "metadata": ("bulk", "local", "haiku-5-5", None, "formulaic descriptions/tags/captions"),
    "thumbnail_text": ("bulk", "local", "haiku-5-5", None, "3-5 words"),
    "title_variants": ("bulk", "local", "haiku-5-5", None, "local generates 10 candidates"),
    "translation_draft": ("bulk", "local", "haiku-5-5", None, "optional ES captions"),
    "claim_extraction": ("bulk", "local", "haiku-5-5", None, "feeds the fact-check step"),
    "qa_judge": ("bulk", "local", "haiku-5-5", None, "non-YMYL QA judge"),
    # overflow is a lane, but callers may also request it explicitly
    "overflow": ("bulk", "haiku-5-5", None, None, "explicit bulk overflow to Haiku 5.5"),
    # quality / Bedrock
    "polish": ("quality", "sonnet-5-5", None, "opus-5-5", "long-form polish drives RPM"),
    "polish_short": ("quality", "haiku-5-5", None, None, "Haiku 5.5 is cheap enough"),
    "fact_check": (
        "quality",
        "sonnet-5-5",
        None,
        "opus-5-5",
        "accuracy-critical channels; disputed claims escalate to Opus 5.5",
    ),
    "fact_check_light": ("quality", "haiku-5-5", None, None, "cheap second opinion"),
    "title_rank": ("quality", "sonnet-5-5", None, None, "CTR lever for long-form"),
    "comment_mining": ("quality", "haiku-5-5", None, None, "cluster summaries, <100K prompt"),
    "translation_qa": ("quality", "haiku-5-5", None, None, "low volume QA"),
    "qa_judge_ymyl": ("quality", "sonnet-5-5", None, None, "Bedrock judge for YMYL"),
}

ROLES: frozenset[str] = frozenset(_TABLE)


class UnknownRole(KeyError):
    pass


def resolve(role: str, *, escalate: bool = False) -> Route:
    """Return the route for ``role``. ``escalate=True`` swaps in the escalation model
    (e.g. a disputed fact-check goes to Opus 5.5)."""
    try:
        step_class, model_key, overflow_key, esc_key, reason = _TABLE[role]
    except KeyError:
        raise UnknownRole(f"unknown LLM role {role!r}; known: {sorted(ROLES)}") from None
    cat = models()
    route = Route(
        role=role,
        step_class=step_class,
        model=cat[model_key],
        overflow=cat[overflow_key] if overflow_key else None,
        escalation=cat[esc_key] if esc_key else None,
        reason=reason,
    )
    if escalate:
        if route.escalation is None:
            raise ValueError(f"role {role!r} has no escalation model")
        route = replace(route, model=route.escalation, escalation=None)
    return route


def routing_table() -> dict[str, Route]:
    return {role: resolve(role) for role in sorted(ROLES)}
