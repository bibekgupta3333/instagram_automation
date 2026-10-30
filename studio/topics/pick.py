"""Picker: proposed topics -> day plan YAML in the existing {channel, topic, format} shape."""

from __future__ import annotations

import math
import os
from datetime import date
from pathlib import Path

import yaml

from studio.contracts import BrandPack
from studio.topics.triage import HARD_EXCLUDED_CHANNELS, jaccard, tokens

DAILY_CAP_DEFAULT = 20
DAILY_CAP_CEILING = 1000
CLUSTER_JACCARD = 0.4


def daily_cap(arg: int | None = None) -> int:
    v = arg
    if v is None:
        try:
            v = int(os.environ.get("STUDIO_DAILY_CAP", DAILY_CAP_DEFAULT))
        except ValueError:
            v = DAILY_CAP_DEFAULT
    return max(0, min(int(v), DAILY_CAP_CEILING))


def plan_path(day: date, plan_dir: Path | str | None = None) -> Path:
    return Path(plan_dir or os.environ.get("STUDIO_PLAN_DIR", "output/plans")) / f"{day.isoformat()}.yaml"


def pick_topics(rows: list[dict], brands: dict[str, BrandPack], *, per_channel: int = 1,
                cap: int = DAILY_CAP_DEFAULT, existing: list[dict] | None = None) -> list[dict]:
    """rows: dicts with id, channel, topic_text, format, volume (sorted best-first by caller).
    Round-robin across channels so the daily cap is shared fairly. Per channel: at most
    min(per_channel, ceil(shorts_per_day)) topics, and at most one per angle cluster."""
    cap = min(cap, DAILY_CAP_CEILING)
    plan = list(existing or [])
    taken: dict[str, list[frozenset]] = {}
    count: dict[str, int] = {}
    for e in plan:
        taken.setdefault(e["channel"], []).append(tokens(e["topic"]))
        count[e["channel"]] = count.get(e["channel"], 0) + 1
    queues: dict[str, list[dict]] = {}
    for r in rows:
        ch = r["channel"]
        b = brands.get(ch)
        if not b or ch in HARD_EXCLUDED_CHANNELS or b.made_for_kids:
            continue
        queues.setdefault(ch, []).append(r)
    limit = {ch: min(per_channel, max(1, math.ceil(brands[ch].shorts_per_day))) for ch in queues}
    picked: list[dict] = []
    progress = True
    while progress and len(plan) < cap:
        progress = False
        for ch in sorted(queues):
            if len(plan) >= cap:
                break
            if count.get(ch, 0) >= limit[ch]:
                continue
            q = queues[ch]
            while q:
                r = q.pop(0)
                t = tokens(r["topic_text"])
                if any(jaccard(t, o) >= CLUSTER_JACCARD for o in taken.get(ch, [])):
                    continue  # same angle cluster already used today
                taken.setdefault(ch, []).append(t)
                count[ch] = count.get(ch, 0) + 1
                entry = {"channel": ch, "topic": r["topic_text"], "format": r.get("format") or "short"}
                plan.append(entry)
                picked.append({**entry, "id": r["id"]})
                progress = True
                break
    return picked


def read_plan(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [e for e in (yaml.safe_load(path.read_text()) or []) if isinstance(e, dict)]


def write_plan(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(
        [{"channel": e["channel"], "topic": e["topic"], "format": e["format"]} for e in entries],
        sort_keys=False, allow_unicode=True))
