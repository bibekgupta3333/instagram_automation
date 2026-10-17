"""Bedrock budget guard, token buckets and the per-channel spend ledger (docs 15 §6, §7.5, §7.6).

Since 2026-10-16 every text role runs on Bedrock (draft/metadata/ideation on Haiku 5.5), so
bulk steps cost money too. Expected spend at full scale (300 Shorts + 80 long-form a month):
about **$2.56 bulk (Haiku) + $16.22 quality = $18.78, ~$23.50 with 25% retries** (*est.*;
worked out in infra/LLM_COSTS.md). The default cap is therefore **$30/month**
($2.50 per channel x 10 + $5 shared pool) instead of the old $25 tier.

- Prices come from docs 15 §2 (global profiles, USD per 1M tokens) and are config: override
  them with a YAML file at ``STUDIO_PRICES_FILE`` (same shape as ``DEFAULT_PRICES``).
- User-supplied model ids (``BEDROCK_MODEL_ID_FAST`` / ``_QUALITY`` / ``_ESCALATION``) are
  priced by the key routing.py infers from the id text. For exact prices on a non-Claude
  model set ``BEDROCK_PRICE_IN_PER_MTOK_<TIER>`` / ``BEDROCK_PRICE_OUT_PER_MTOK_<TIER>``
  (USD per 1M tokens, TIER = FAST | QUALITY | ESCALATION): the tier's model is then billed under
  the key ``price-<tier>``. A missing half takes the tier's Claude default; cache read/write
  are priced at 0.1x / 1.25x the input price.
- Haiku 5.5 prompts over 100K input tokens are billed at 5x (model card "Long Context" row).
- The ledger is SQLite at ``output/ledger.sqlite`` (``STUDIO_LEDGER_PATH``); docs 15 uses
  Postgres, the table shape is the same.
- Quality roles that do not fit (monthly cap, channel cap or TPM bucket) raise
  ``BudgetDeferred``: the caller re-enqueues. They never drop to local silently.
- Bulk roles queue the same way (they are on Bedrock now). ``may_overflow`` only matters for
  the opt-in ``STUDIO_LLM=ollama`` mode: local busy -> Haiku only above 30% headroom.
- The Claude Code backend (``STUDIO_LLM=claude-code``) runs on the user's Claude
  subscription limits, not Bedrock dollars, so it does not go through this guard.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from studio.llm.routing import env_id

# (input, output, cache_read, cache_write_5min) USD per 1M tokens. docs 15 §2, 2026-10-16.
DEFAULT_PRICES: dict[str, tuple[float, float, float, float]] = {
    "haiku-5-5": (0.10, 0.50, 0.01, 0.125),
    "sonnet-5-5": (2.00, 10.00, 0.10, 2.50),
    "opus-5-5": (4.00, 20.00, 0.20, 5.00),
    "local": (0.0, 0.0, 0.0, 0.0),
}
# Haiku 5.5 tiered long-context pricing: prompts > 100K tokens cost 5x (docs 15 §2).
LONG_CONTEXT = {"haiku-5-5": (100_000, 5.0)}
# Output-token quota burndown (docs 15 §3). Sonnet 5.5 is unpublished: assume 10x (verify).
DEFAULT_BURNDOWN: dict[str, int] = {"haiku-5-5": 10, "sonnet-5-5": 10, "opus-5-5": 10, "local": 1}
# Default bucket size = ~80% of the TPM given to LiteLLM in docs 15 §7.4 (set to 80% of the
# APPLIED quota once known).
DEFAULT_TPM: dict[str, int] = {"haiku-5-5": 200_000, "sonnet-5-5": 200_000, "opus-5-5": 100_000}

DEFAULT_LEDGER_PATH = Path("output/ledger.sqlite")


class BudgetDeferred(RuntimeError):
    """The call must be re-enqueued later (quality step over budget/quota, or bulk step with
    local busy and no overflow headroom). ``retry_after`` is seconds, or None = next budget
    period / human decision."""

    def __init__(self, reason: str, *, retry_after: float | None = None, role: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.retry_after = retry_after
        self.role = role


# Tier variable -> the Claude key whose prices fill a half-set override.
TIER_DEFAULT_KEY = {"FAST": "haiku-5-5", "QUALITY": "sonnet-5-5", "ESCALATION": "opus-5-5"}


def _env_float(name: str) -> float | None:
    v = env_id(name)  # empty / comment-only = unset
    return float(v) if v else None


def tier_price_overrides() -> dict[str, tuple[float, float, float, float]]:
    """``price-<tier>`` entries from BEDROCK_PRICE_{IN,OUT}_PER_MTOK_<TIER> (USD per 1M)."""
    out: dict[str, tuple[float, float, float, float]] = {}
    for tier, base in TIER_DEFAULT_KEY.items():
        i = _env_float(f"BEDROCK_PRICE_IN_PER_MTOK_{tier}")
        o = _env_float(f"BEDROCK_PRICE_OUT_PER_MTOK_{tier}")
        if i is None and o is None:
            continue
        bi, bo, _, _ = DEFAULT_PRICES[base]
        i = bi if i is None else i
        o = bo if o is None else o
        out[f"price-{tier.lower()}"] = (i, o, i * 0.1, i * 1.25)
    return out


def load_prices() -> dict[str, tuple[float, float, float, float]]:
    prices = dict(DEFAULT_PRICES)
    path = os.environ.get("STUDIO_PRICES_FILE")
    if path:
        import yaml

        raw = yaml.safe_load(Path(path).read_text()) or {}
        for k, v in raw.items():
            prices[k] = tuple(float(x) for x in v)  # type: ignore[assignment]
    prices.update(tier_price_overrides())
    return prices


def cost_usd(model_key: str, usage: dict[str, Any], prices: dict | None = None) -> float:
    """Cost from an OpenAI-style or Converse-style usage dict."""
    table = prices or DEFAULT_PRICES
    if model_key not in table and model_key.startswith("price-"):
        table = {**table, **tier_price_overrides()}  # env set after the guard was built
    i, o, cr, cw = table.get(model_key, (0.0, 0.0, 0.0, 0.0))
    inp = usage.get("inputTokens", usage.get("prompt_tokens", 0)) or 0
    out = usage.get("outputTokens", usage.get("completion_tokens", 0)) or 0
    c_read = usage.get("cacheReadInputTokens", usage.get("cache_read_input_tokens", 0)) or 0
    c_write = usage.get("cacheWriteInputTokens", usage.get("cache_creation_input_tokens", 0)) or 0
    mult = 1.0
    if model_key in LONG_CONTEXT:
        threshold, factor = LONG_CONTEXT[model_key]
        if inp + c_read + c_write > threshold:
            mult = factor
    return mult * (inp * i + out * o + c_read * cr + c_write * cw) / 1e6


class TokenBucket:
    """Per-model TPM bucket (docs 15 §7.5). Bedrock reserves input + max_tokens up front."""

    def __init__(self, tpm: int, clock: Callable[[], float] = time.monotonic):
        self.cap = tpm
        self.tokens = float(tpm)
        self.clock = clock
        self.t = clock()
        self.lock = threading.Lock()

    def _refill(self) -> None:
        now = self.clock()
        self.tokens = min(self.cap, self.tokens + (now - self.t) * self.cap / 60)
        self.t = now

    def try_take(self, n: int) -> float:
        """0.0 = admitted, otherwise seconds to wait."""
        with self.lock:
            self._refill()
            if n <= self.tokens:
                self.tokens -= n
                return 0.0
            return (n - self.tokens) * 60 / self.cap

    def settle(self, reserved: int, actual: int) -> None:
        with self.lock:
            self.tokens = min(self.cap, self.tokens + reserved - actual)


class Ledger:
    """SQLite spend ledger, one row per LLM call (local calls at usd=0)."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path or os.environ.get("STUDIO_LEDGER_PATH") or DEFAULT_LEDGER_PATH)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS llm_spend (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    channel TEXT NOT NULL, job_id TEXT NOT NULL, step TEXT NOT NULL,
                    route TEXT NOT NULL CHECK (route IN ('local','bedrock')),
                    model TEXT NOT NULL,
                    input_tokens INTEGER, output_tokens INTEGER,
                    cache_read INTEGER, cache_write INTEGER,
                    usd REAL NOT NULL DEFAULT 0,
                    outcome TEXT)"""
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS llm_spend_channel_ts ON llm_spend (channel, ts)"
            )

    def record(
        self,
        *,
        channel: str,
        job_id: str,
        step: str,
        route: str,
        model: str,
        usage: dict[str, Any] | None = None,
        usd: float = 0.0,
        outcome: str = "ok",
        ts: datetime | None = None,
    ) -> None:
        u = usage or {}
        row = (
            (ts or datetime.now(UTC)).isoformat(),
            channel,
            job_id,
            step,
            route,
            model,
            u.get("inputTokens", u.get("prompt_tokens")),
            u.get("outputTokens", u.get("completion_tokens")),
            u.get("cacheReadInputTokens"),
            u.get("cacheWriteInputTokens"),
            usd,
            outcome,
        )
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO llm_spend (ts, channel, job_id, step, route, model, input_tokens,"
                " output_tokens, cache_read, cache_write, usd, outcome)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                row,
            )

    @staticmethod
    def _month_start(now: datetime | None = None) -> str:
        n = now or datetime.now(UTC)
        return n.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()

    def month_to_date(self, now: datetime | None = None) -> dict[str, float]:
        """{channel: usd} for the current calendar month (UTC)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT channel, COALESCE(SUM(usd),0) FROM llm_spend WHERE ts >= ?"
                " GROUP BY channel",
                (self._month_start(now),),
            ).fetchall()
        return {c: float(u) for c, u in rows}

    def close(self) -> None:
        self._conn.close()


@dataclass
class BudgetConfig:
    """Defaults: $30/month = $2.50/channel x 10 + $5 shared pool, YMYL weight 1.5x.
    (docs 15 §6 $25 tier plus ~$2.60 of Haiku bulk work; see infra/LLM_COSTS.md.)"""

    monthly_cap_usd: float = 30.0
    per_channel_usd: float = 2.5
    shared_pool_usd: float = 5.0
    accuracy_critical_weight: float = 1.5
    accuracy_critical_channels: frozenset[str] = frozenset()
    overflow_min_headroom: float = 0.30
    tpm: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_TPM))

    @classmethod
    def from_env(cls) -> BudgetConfig:
        e = os.environ
        crit = {c.strip() for c in e.get("STUDIO_ACCURACY_CRITICAL_CHANNELS", "").split(",")}
        return cls(
            monthly_cap_usd=float(e.get("STUDIO_BEDROCK_MONTHLY_USD") or 30.0),
            per_channel_usd=float(e.get("STUDIO_BEDROCK_CHANNEL_USD") or 2.5),
            shared_pool_usd=float(e.get("STUDIO_BEDROCK_SHARED_POOL_USD") or 5.0),
            accuracy_critical_channels=frozenset(c for c in crit if c),
        )


@dataclass
class Admission:
    reserved_tokens: int
    est_usd: float
    model_key: str


class BudgetGuard:
    def __init__(
        self,
        config: BudgetConfig | None = None,
        ledger: Ledger | None = None,
        *,
        prices: dict | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.config = config or BudgetConfig.from_env()
        self.ledger = ledger or Ledger()
        self.prices = prices or load_prices()
        self._clock = clock
        self._buckets: dict[str, TokenBucket] = {}

    def bucket(self, model_key: str) -> TokenBucket:
        if model_key not in self._buckets:
            self._buckets[model_key] = TokenBucket(
                self.config.tpm.get(model_key, 100_000), clock=self._clock
            )
        return self._buckets[model_key]

    def channel_cap(self, channel: str, accuracy_critical: bool = False) -> float:
        crit = accuracy_critical or channel in self.config.accuracy_critical_channels
        w = self.config.accuracy_critical_weight if crit else 1.0
        return self.config.per_channel_usd * w

    def estimate_usd(self, model_key: str, est_in: int, max_out: int) -> float:
        # worst case: full max_tokens (and the long-context multiplier if it applies)
        return cost_usd(model_key, {"inputTokens": est_in, "outputTokens": max_out}, self.prices)

    def _fits_money(self, channel: str, est_usd: float, accuracy_critical: bool) -> str | None:
        mtd = self.ledger.month_to_date()
        spent_month = sum(mtd.values())
        if spent_month + est_usd > self.config.monthly_cap_usd:
            return f"monthly Bedrock cap ${self.config.monthly_cap_usd:.2f} reached"
        cap = self.channel_cap(channel, accuracy_critical)
        spent_chan = mtd.get(channel, 0.0)
        if spent_chan + est_usd <= cap:
            return None
        # over its own cap: draw on the shared pool
        pool_used = sum(max(0.0, s - self.channel_cap(c)) for c, s in mtd.items() if c != channel)
        own_over = max(0.0, spent_chan + est_usd - cap)
        if pool_used + own_over <= self.config.shared_pool_usd:
            return None
        return f"channel {channel!r} cap ${cap:.2f} and shared pool exhausted"

    def headroom(self) -> float:
        spent = sum(self.ledger.month_to_date().values())
        cap = self.config.monthly_cap_usd
        return max(0.0, (cap - spent) / cap) if cap > 0 else 0.0

    def admit(
        self,
        *,
        role: str,
        channel: str,
        model_key: str,
        est_in: int,
        max_out: int,
        accuracy_critical: bool = False,
    ) -> Admission:
        """Admit a Bedrock call or raise BudgetDeferred (queue, never degrade silently)."""
        est = self.estimate_usd(model_key, est_in, max_out)
        why = self._fits_money(channel, est, accuracy_critical)
        if why:
            raise BudgetDeferred(why, retry_after=None, role=role)
        n = est_in + max_out
        wait = self.bucket(model_key).try_take(n)
        if wait:
            raise BudgetDeferred(
                f"{model_key} token bucket full; retry in {wait:.0f}s", retry_after=wait, role=role
            )
        return Admission(reserved_tokens=n, est_usd=est, model_key=model_key)

    def may_overflow(self, *, channel: str, model_key: str, est_in: int, max_out: int) -> bool:
        """Bulk overflow to Bedrock is allowed only with >30% monthly headroom (docs 15 §5)."""
        if self.headroom() <= self.config.overflow_min_headroom:
            return False
        est = self.estimate_usd(model_key, est_in, max_out)
        return self._fits_money(channel, est, False) is None

    def settle(
        self,
        admission: Admission,
        usage: dict[str, Any],
        *,
        role: str,
        channel: str,
        job_id: str,
        outcome: str = "ok",
    ) -> float:
        mk = admission.model_key
        out = usage.get("outputTokens", usage.get("completion_tokens", 0)) or 0
        inp = usage.get("inputTokens", usage.get("prompt_tokens", 0)) or 0
        cw = usage.get("cacheWriteInputTokens", 0) or 0
        actual = inp + cw + out * DEFAULT_BURNDOWN.get(mk, 1)
        self.bucket(mk).settle(admission.reserved_tokens, actual)
        usd = cost_usd(mk, usage, self.prices)
        self.ledger.record(
            channel=channel,
            job_id=job_id,
            step=role,
            route="bedrock",
            model=mk,
            usage=usage,
            usd=usd,
            outcome=outcome,
        )
        return usd

    def record_local(self, *, role: str, channel: str, job_id: str, usage: dict | None) -> None:
        self.ledger.record(
            channel=channel, job_id=job_id, step=role, route="local", model="local", usage=usage
        )


def estimate_tokens(messages: list[dict[str, str]]) -> int:
    """Rough estimate: ~4 characters per token, plus per-message overhead."""
    return sum(len(m.get("content") or "") // 4 + 4 for m in messages)
