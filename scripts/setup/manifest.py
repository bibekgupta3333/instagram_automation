"""Tiny reader for models.manifest.yaml, used by the setup shell scripts.

Usage:
  python manifest.py total [--include-optional]            -> required GB (float)
  python manifest.py buffer                                -> disk buffer GB
  python manifest.py list [--stage S] [--include-optional] -> TSV rows:
      name  stage  runtime  source  size_gb  optional  include(comma-sep)  verified
  python manifest.py stages                                -> TSV: stage  required_gb  optional_gb

--name NAME is repeatable and exact; optional names still need --include-optional.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

MANIFEST = Path(__file__).with_name("models.manifest.yaml")
STAGES = ("llm", "tts", "asr", "image", "video", "music")


def load() -> dict:
    return yaml.safe_load(MANIFEST.read_text())


def selected(
    models: list[dict], stage: str, include_optional: bool, names: list[str] | None = None
) -> list[dict]:
    requested = set(names or [])
    known = {m["name"]: m for m in models}
    unknown = requested - known.keys()
    if unknown:
        raise ValueError(f"unknown model name(s): {', '.join(sorted(unknown))}")
    blocked = [name for name in requested if known[name].get("optional")]
    if blocked and not include_optional:
        raise ValueError(f"optional model(s) require --include-optional: {', '.join(sorted(blocked))}")
    wrong_stage = [name for name in requested if stage != "all" and known[name]["stage"] != stage]
    if wrong_stage:
        raise ValueError(f"model(s) outside --stage {stage}: {', '.join(sorted(wrong_stage))}")
    out = []
    for m in models:
        if requested and m["name"] not in requested:
            continue
        if stage != "all" and m["stage"] != stage:
            continue
        if m.get("optional") and not include_optional:
            continue
        out.append(m)
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["total", "buffer", "list", "stages"])
    p.add_argument("--stage", default="all", choices=["all", *STAGES])
    p.add_argument("--include-optional", action="store_true")
    p.add_argument("--name", action="append", help="exact model name (repeatable)")
    a = p.parse_args()
    data = load()
    models = data["models"]
    try:
        chosen = selected(models, a.stage, a.include_optional, a.name)
    except ValueError as exc:
        p.error(str(exc))

    if a.cmd == "buffer":
        print(data.get("disk_buffer_gb", 10))
    elif a.cmd == "total":
        print(round(sum(m["size_gb"] for m in chosen), 3))
    elif a.cmd == "stages":
        if a.name:
            models = chosen
        for s in STAGES:
            req = sum(m["size_gb"] for m in models if m["stage"] == s and not m.get("optional"))
            opt = sum(m["size_gb"] for m in models if m["stage"] == s and m.get("optional"))
            print(f"{s}\t{req:.1f}\t{opt:.1f}")
    else:
        for m in chosen:
            row = [
                m["name"],
                m["stage"],
                m["runtime"],
                m["source"],
                str(m["size_gb"]),
                "yes" if m.get("optional") else "no",
                ",".join(m.get("include") or []) or "-",
                "yes" if m.get("verified") else "no",
            ]
            print("\t".join(row))
    return 0


if __name__ == "__main__":
    sys.exit(main())
