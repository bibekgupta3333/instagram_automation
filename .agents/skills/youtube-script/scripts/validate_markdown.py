#!/usr/bin/env python3
"""Deterministic structural checks for YouTube script drafts."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

TIMESTAMP = re.compile(r"\[(\d{1,2}):(\d{2})\]")
LOOP_OPEN = re.compile(r"\[LOOP OPEN\s+([A-Za-z0-9_-]+)", re.IGNORECASE)
LOOP_CLOSE = re.compile(r"\[LOOP CLOSE\s+([A-Za-z0-9_-]+)", re.IGNORECASE)
CTA_HEADING = re.compile(r"^#{2,4}\s+.*\bCTA\b", re.IGNORECASE | re.MULTILINE)
CONFIRM = re.compile(r"\[CREATOR:\s*confirm\b", re.IGNORECASE)
WORD = re.compile(r"\b[\w’'-]+\b", re.UNICODE)
DIRECTION_LINE = re.compile(r"^\s*\[(?:VISUAL|SCREEN|B-ROLL|PAUSE|LOOP|CREATOR):?", re.IGNORECASE)


def spoken_text(text: str) -> str:
    lines = []
    in_fence = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or line.lstrip().startswith("#") or DIRECTION_LINE.match(line):
            continue
        line = TIMESTAMP.sub("", line)
        lines.append(line)
    return "\n".join(lines)


def validate(path: Path, fmt: str, wpm: int) -> tuple[list[str], list[str]]:
    text = path.read_text(encoding="utf-8")
    errors: list[str] = []
    warnings: list[str] = []

    required = ["HOOK"] if fmt == "long-form" else ["OPEN", "MECHANISM", "CLOSE"]
    for name in required:
        if not re.search(rf"^#{{2,4}}\s+.*\b{re.escape(name)}\b", text, re.IGNORECASE | re.MULTILINE):
            errors.append(f"Missing required heading containing: {name}")

    timestamps = [int(m.group(1)) * 60 + int(m.group(2)) for m in TIMESTAMP.finditer(text)]
    if not timestamps:
        errors.append("No [M:SS] timestamps found")
    elif timestamps != sorted(timestamps):
        errors.append("Timestamps are not monotonic")

    opens = LOOP_OPEN.findall(text)
    closes = LOOP_CLOSE.findall(text)
    unclosed = sorted(set(opens) - set(closes))
    orphaned = sorted(set(closes) - set(opens))
    if unclosed:
        errors.append("Unclosed loops: " + ", ".join(unclosed))
    if orphaned:
        errors.append("Loop closes without opens: " + ", ".join(orphaned))

    cta_count = len(CTA_HEADING.findall(text))
    if cta_count > 1:
        errors.append(f"Multiple CTA sections found: {cta_count}")
    if fmt == "long-form" and cta_count == 0:
        warnings.append("No CTA heading found")

    words = WORD.findall(spoken_text(text))
    runtime = len(words) / max(wpm, 1)
    if len(words) < 40:
        warnings.append("Very little spoken text detected")

    confirmations = len(CONFIRM.findall(text))
    if confirmations:
        warnings.append(f"Open creator confirmations: {confirmations}")

    warnings.append(f"Estimated spoken words: {len(words)}")
    warnings.append(f"Estimated runtime at {wpm} WPM: {runtime:.2f} minutes")
    warnings.append(f"Loops opened/closed: {len(opens)}/{len(closes)}")
    warnings.append(f"CTA sections: {cta_count}")
    return errors, warnings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("script", type=Path)
    parser.add_argument("--format", choices=["long-form", "shorts"], default="long-form")
    parser.add_argument("--wpm", type=int, default=150)
    args = parser.parse_args()

    errors, warnings = validate(args.script, args.format, args.wpm)
    for item in warnings:
        print(f"INFO: {item}")
    for item in errors:
        print(f"ERROR: {item}")
    print("PASS" if not errors else "FAIL")
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
