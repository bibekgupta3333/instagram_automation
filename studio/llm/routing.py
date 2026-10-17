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

Model identifiers are config values (Bedrock is used for TEXT models only). Precedence:

1. per role:  ``STUDIO_BEDROCK_MODEL_<ROLE>`` (e.g. ``STUDIO_BEDROCK_MODEL_DRAFT``), and
   ``STUDIO_BEDROCK_MODEL_ESCALATION`` for the dispute model;
2. per tier:  ``BEDROCK_MODEL_ID_FAST`` (every role routed to the Haiku tier),
   ``BEDROCK_MODEL_ID_QUALITY`` (Sonnet tier) and ``BEDROCK_MODEL_ID_ESCALATION`` (Opus tier).
   The older ``BEDROCK_HAIKU_MODEL_ID`` / ``BEDROCK_SONNET_MODEL_ID`` / ``BEDROCK_OPUS_MODEL_ID``
   still work, below the new names;
3. the built-in ``global.anthropic.claude-*-5-5`` defaults.

An **empty value counts as unset** (the .env ships ``BEDROCK_MODEL_ID_FAST=``). Any Bedrock id
form works: a base model id (``anthropic.claude-...``, ``amazon.nova-lite-v1:0``), a cross-Region
inference profile (``us.`` / ``eu.`` / ``apac.`` / ``global.`` prefix) or a full ARN.

Pricing (``ModelSpec.key``, used by budget.py) is inferred from the id text
(``price_key_for_id``): Claude ids price as their haiku/sonnet/opus tier; known non-Claude
families price at a conservative Claude-tier ceiling; an unknown id prices as the tier its
variable belongs to (FAST -> haiku, QUALITY -> sonnet, ESCALATION -> opus) with a one-time
warning. ``BEDROCK_PRICE_{IN,OUT}_PER_MTOK_<TIER>`` overrides the price of the model a tier
variable resolves to (key ``price-<tier>``, see budget.py).

Claude Code: ``STUDIO_CLAUDE_CODE_MODEL_<ROLE>`` (alias or full model name).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, replace
from typing import Literal

log = logging.getLogger(__name__)

Lane = Literal["local", "bedrock"]
StepClass = Literal["bulk", "quality"]


def _env(name: str, default: str) -> str:
    return os.environ.get(name) or default


def env_id(name: str) -> str:
    """A model-id env var, "" when unset. Empty counts as unset, and so does a value that is
    only an inline comment (``BEDROCK_MODEL_ID_FAST=   # drafts`` can reach the env as
    ``# drafts`` through a dotenv loader); a trailing `` # comment`` is dropped."""
    v = (os.environ.get(name) or "").strip()
    if v.startswith("#"):
        return ""
    return v.split(" #", 1)[0].strip()


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


# Tier variables (the simple knobs in .env). Tier key -> (env var, legacy env var, default).
TIER_ENV: dict[str, tuple[str, str, str]] = {
    "FAST": ("BEDROCK_MODEL_ID_FAST", "BEDROCK_HAIKU_MODEL_ID", BEDROCK_HAIKU_DEFAULT),
    "QUALITY": ("BEDROCK_MODEL_ID_QUALITY", "BEDROCK_SONNET_MODEL_ID", BEDROCK_SONNET_DEFAULT),
    "ESCALATION": ("BEDROCK_MODEL_ID_ESCALATION", "BEDROCK_OPUS_MODEL_ID", BEDROCK_OPUS_DEFAULT),
}
# Claude tier key <-> tier variable.
TIER_OF_KEY = {"haiku-5-5": "FAST", "sonnet-5-5": "QUALITY", "opus-5-5": "ESCALATION"}
KEY_OF_TIER = {t: k for k, t in TIER_OF_KEY.items()}

# Non-Claude families (substring of the lower-cased id) -> the Claude-tier price used as a
# CONSERVATIVE ceiling for the budget guard (est., from public Bedrock list prices; set
# BEDROCK_PRICE_{IN,OUT}_PER_MTOK_<TIER> for exact numbers). First match wins.
_FAMILY_PRICE_CLASS: tuple[tuple[str, str], ...] = (
    ("nova-micro", "haiku-5-5"),
    ("nova-lite", "haiku-5-5"),
    ("nova-2-lite", "haiku-5-5"),
    ("nova-pro", "sonnet-5-5"),
    ("nova-premier", "sonnet-5-5"),
    ("ministral", "haiku-5-5"),
    ("command-r-plus", "opus-5-5"),
    ("command-r", "sonnet-5-5"),
    ("llama", "sonnet-5-5"),
    ("mistral", "sonnet-5-5"),
    ("mixtral", "sonnet-5-5"),
    ("pixtral", "sonnet-5-5"),
    ("magistral", "sonnet-5-5"),
    ("devstral", "sonnet-5-5"),
    ("jamba", "sonnet-5-5"),
    ("deepseek", "sonnet-5-5"),
    ("qwen", "sonnet-5-5"),
    ("gpt-oss", "sonnet-5-5"),
    ("gemma", "sonnet-5-5"),
    ("nemotron", "sonnet-5-5"),
    ("titan-text", "sonnet-5-5"),
)

_warned: set[str] = set()


def infer_price_key(model_id: str) -> str | None:
    """Price key implied by the id text, or None when the family is unknown."""
    low = model_id.lower()
    for tier in ("opus", "sonnet", "haiku"):
        if tier in low:
            return f"{tier}-5-5"
    for needle, key in _FAMILY_PRICE_CLASS:
        if needle in low:
            return key
    return None


def price_key_for_id(model_id: str, tier: str) -> str:
    """Infer the price key; unknown ids fall back to ``tier``'s Claude key (one warning)."""
    key = infer_price_key(model_id)
    if key is not None:
        return key
    fallback = KEY_OF_TIER[tier]
    if model_id not in _warned:
        _warned.add(model_id)
        log.warning(
            "Bedrock model id %r: unknown family, budgeting it at %s prices (tier %s). Set "
            "BEDROCK_PRICE_IN_PER_MTOK_%s / BEDROCK_PRICE_OUT_PER_MTOK_%s for exact prices.",
            model_id,
            fallback,
            tier,
            tier,
            tier,
        )
    return fallback


def tier_price_override(tier: str) -> bool:
    """True when BEDROCK_PRICE_IN_PER_MTOK_<TIER> or ..._OUT_... is set (non-empty)."""
    return any(env_id(f"BEDROCK_PRICE_{d}_PER_MTOK_{tier}") for d in ("IN", "OUT"))


def _gateway_for(key: str, tier: str) -> str:
    base = key if key in TIER_OF_KEY else KEY_OF_TIER[tier]
    return f"bedrock-{base.split('-')[0]}"


def tier_model_id(tier: str) -> str:
    env, legacy, default = TIER_ENV[tier]
    return env_id(env) or env_id(legacy) or default


@dataclass(frozen=True)
class ModelSpec:
    key: str  # price/budget key: "haiku-5-5", "sonnet-5-5", "opus-5-5", "price-fast", "local"
    lane: Lane
    gateway_model: str  # LiteLLM model_name (infra/litellm/config.yaml)
    provider_model: str  # Bedrock inference profile / model id / ARN, or Ollama tag
    claude_code_model: str = ""  # `claude --model` value (alias or full name)
    tier: str = ""  # "FAST" / "QUALITY" / "ESCALATION" (the tier variable group)


def _tier_spec(tier: str, cc_alias: str) -> ModelSpec:
    model_id = tier_model_id(tier)
    if tier_price_override(tier):
        key = f"price-{tier.lower()}"
    else:
        key = price_key_for_id(model_id, tier)
    return ModelSpec(key, "bedrock", _gateway_for(key, tier), model_id, cc_alias, tier)


def models() -> dict[str, ModelSpec]:
    """Model catalogue, resolved from env each call so tests/ops can override."""
    return {
        "haiku-5-5": _tier_spec("FAST", "haiku"),
        "sonnet-5-5": _tier_spec("QUALITY", "sonnet"),
        "opus-5-5": _tier_spec("ESCALATION", "opus"),
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


def _override(spec: ModelSpec, env_suffix: str) -> ModelSpec:
    """Apply STUDIO_BEDROCK_MODEL_<X> / STUDIO_CLAUDE_CODE_MODEL_<X> overrides (empty = unset).
    A per-role id is priced from its own text (the tier's price override does not apply)."""
    bedrock_id = env_id(f"STUDIO_BEDROCK_MODEL_{env_suffix}")
    cc = env_id(f"STUDIO_CLAUDE_CODE_MODEL_{env_suffix}")
    if bedrock_id:
        key = price_key_for_id(bedrock_id, spec.tier or "FAST")
        spec = replace(
            spec,
            key=key,
            provider_model=bedrock_id,
            gateway_model=_gateway_for(key, spec.tier or "FAST"),
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
