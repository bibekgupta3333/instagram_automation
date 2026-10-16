"""Role -> model routing policy (docs 15 §5, revised 2026-10-16: no local LLM by default).

Every LLM call in the pipeline names a *role* (the pipeline step). The table below maps each
role to a step class (bulk vs quality) and a Claude 5.5 tier on **Amazon Bedrock**:

- bulk roles (ideation, outline, draft, metadata, ...) -> **Haiku 5.5**
- quality roles (polish, fact_check, title_rank, ...)  -> **Sonnet 5.5**
- dispute escalation (``escalate=True``)               -> **Opus 5.5**

The same tiers are used by the Claude Code backend (``STUDIO_LLM=claude-code``) through the
``haiku`` / ``sonnet`` / ``opus`` aliases (``ModelSpec.claude_code_model``).

Local Ollama is an explicit opt-in only (``STUDIO_LLM=ollama``). Even then, only bulk roles
may run locally (``Route.local``); quality roles never fall back to a local model
(docs 15 §7.1). Over budget, every Bedrock role **queues** (``BudgetDeferred``).

Model identifiers are config values:
- per tier:  ``BEDROCK_HAIKU_MODEL_ID`` / ``BEDROCK_SONNET_MODEL_ID`` / ``BEDROCK_OPUS_MODEL_ID``
- per role:  ``STUDIO_BEDROCK_MODEL_<ROLE>`` (e.g. ``STUDIO_BEDROCK_MODEL_DRAFT``), and
  ``STUDIO_BEDROCK_MODEL_ESCALATION`` for the dispute model. A per-role override keeps the
  price key of the tier its id names (``haiku``/``sonnet``/``opus``), else the role's tier.
- Claude Code: ``STUDIO_CLAUDE_CODE_MODEL_<ROLE>`` (alias or full model name).
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
# Global cross-Region inference profiles. Verified 2026-10-16 against the Bedrock model cards
# ("Programmatic Access" table): e.g.
# https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-haiku-5-5.html
# Not yet called from the user's account (no key configured at the time of writing).
BEDROCK_HAIKU_DEFAULT = "global.anthropic.claude-haiku-5-5"
BEDROCK_SONNET_DEFAULT = "global.anthropic.claude-sonnet-5-5"
BEDROCK_OPUS_DEFAULT = "global.anthropic.claude-opus-5-5"

# Optional local workhorse (STUDIO_LLM=ollama only). docs 14 §3.
LOCAL_MODEL_DEFAULT = "qwen3.5:9b"


@dataclass(frozen=True)
class ModelSpec:
    key: str  # price/budget key: "haiku-5-5", "sonnet-5-5", "opus-5-5", "local"
    lane: Lane
    gateway_model: str  # LiteLLM model_name (infra/litellm/config.yaml)
    provider_model: str  # Bedrock inference profile / model id, or Ollama tag
    claude_code_model: str = ""  # `claude --model` value (alias or full name)


def models() -> dict[str, ModelSpec]:
    """Model catalogue, resolved from env each call so tests/ops can override."""
    return {
        "haiku-5-5": ModelSpec(
            "haiku-5-5",
            "bedrock",
            "bedrock-haiku",
            _env("BEDROCK_HAIKU_MODEL_ID", BEDROCK_HAIKU_DEFAULT),
            "haiku",
        ),
        "sonnet-5-5": ModelSpec(
            "sonnet-5-5",
            "bedrock",
            "bedrock-sonnet",
            _env("BEDROCK_SONNET_MODEL_ID", BEDROCK_SONNET_DEFAULT),
            "sonnet",
        ),
        "opus-5-5": ModelSpec(
            "opus-5-5",
            "bedrock",
            "bedrock-opus",
            _env("BEDROCK_OPUS_MODEL_ID", BEDROCK_OPUS_DEFAULT),
            "opus",
        ),
        "local": ModelSpec(
            "local", "local", "local-bulk", _env("STUDIO_LOCAL_MODEL", LOCAL_MODEL_DEFAULT)
        ),
    }


@dataclass(frozen=True)
class Route:
    role: str
    step_class: StepClass
    model: ModelSpec  # always a Bedrock tier
    escalation: ModelSpec | None = None  # dispute model (polish/fact_check -> Opus 5.5)
    local: ModelSpec | None = None  # bulk only, used solely with STUDIO_LLM=ollama
    reason: str = ""

    @property
    def lane(self) -> Lane:
        return self.model.lane


# role -> (class, model key, escalation key, reason). docs 15 §5 (all text on Bedrock).
_TABLE: dict[str, tuple[StepClass, str, str | None, str]] = {
    # bulk -> Haiku 5.5
    "ideation": ("bulk", "haiku-5-5", None, "high volume, low stakes"),
    "outline": ("bulk", "haiku-5-5", None, "templated, structural"),
    "draft": ("bulk", "haiku-5-5", None, "bulk words; quality comes at polish"),
    "metadata": ("bulk", "haiku-5-5", None, "formulaic descriptions/tags/captions"),
    "thumbnail_text": ("bulk", "haiku-5-5", None, "3-5 words"),
    "title_variants": ("bulk", "haiku-5-5", None, "generates 10 candidates"),
    "translation_draft": ("bulk", "haiku-5-5", None, "optional ES captions"),
    "claim_extraction": ("bulk", "haiku-5-5", None, "feeds the fact-check step"),
    "qa_judge": ("bulk", "haiku-5-5", None, "non-YMYL QA judge"),
    "overflow": ("bulk", "haiku-5-5", None, "explicit cheap lane (Haiku 5.5)"),
    # quality -> Sonnet 5.5 (Haiku where docs 15 says it is enough)
    "polish": ("quality", "sonnet-5-5", "opus-5-5", "long-form polish drives RPM"),
    "polish_short": ("quality", "haiku-5-5", None, "Haiku 5.5 is cheap enough"),
    "fact_check": (
        "quality",
        "sonnet-5-5",
        "opus-5-5",
        "accuracy-critical channels; disputed claims escalate to Opus 5.5",
    ),
    "fact_check_light": ("quality", "haiku-5-5", None, "cheap second opinion"),
    "title_rank": ("quality", "sonnet-5-5", None, "CTR lever for long-form"),
    "comment_mining": ("quality", "haiku-5-5", None, "cluster summaries, <100K prompt"),
    "translation_qa": ("quality", "haiku-5-5", None, "low volume QA"),
    "qa_judge_ymyl": ("quality", "sonnet-5-5", None, "Bedrock judge for YMYL"),
}

ROLES: frozenset[str] = frozenset(_TABLE)


class UnknownRole(KeyError):
    pass


def _key_for_id(model_id: str, default: str) -> str:
    low = model_id.lower()
    for tier in ("opus", "sonnet", "haiku"):
        if tier in low:
            return f"{tier}-5-5"
    return default


def _override(spec: ModelSpec, env_suffix: str) -> ModelSpec:
    """Apply STUDIO_BEDROCK_MODEL_<X> / STUDIO_CLAUDE_CODE_MODEL_<X> overrides."""
    bedrock_id = os.environ.get(f"STUDIO_BEDROCK_MODEL_{env_suffix}")
    cc = os.environ.get(f"STUDIO_CLAUDE_CODE_MODEL_{env_suffix}")
    if bedrock_id:
        key = _key_for_id(bedrock_id, spec.key)
        spec = replace(
            spec,
            key=key,
            provider_model=bedrock_id,
            gateway_model=f"bedrock-{key.split('-')[0]}",
        )
    if cc:
        spec = replace(spec, claude_code_model=cc)
    return spec


def resolve(role: str, *, escalate: bool = False) -> Route:
    """Return the route for ``role``. ``escalate=True`` swaps in the escalation model
    (e.g. a disputed fact-check goes to Opus 5.5)."""
    try:
        step_class, model_key, esc_key, reason = _TABLE[role]
    except KeyError:
        raise UnknownRole(f"unknown LLM role {role!r}; known: {sorted(ROLES)}") from None
    cat = models()
    route = Route(
        role=role,
        step_class=step_class,
        model=_override(cat[model_key], role.upper()),
        escalation=_override(cat[esc_key], "ESCALATION") if esc_key else None,
        local=cat["local"] if step_class == "bulk" else None,
        reason=reason,
    )
    if escalate:
        if route.escalation is None:
            raise ValueError(f"role {role!r} has no escalation model")
        route = replace(route, model=route.escalation, escalation=None, local=None)
    return route


def routing_table() -> dict[str, Route]:
    return {role: resolve(role) for role in sorted(ROLES)}
