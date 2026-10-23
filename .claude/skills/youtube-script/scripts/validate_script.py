#!/usr/bin/env python3
"""Deterministic checks for a studio `Script` JSON written with the youtube-script skill.

Adapted from chaseraw/youtube-script-skill (MIT, see ../LICENSE and ../NOTICE.md). Upstream
checks a Markdown draft; this version checks the JSON the pipeline consumes
(`studio.contracts.Script`): title, hook, beats[{text, visual_prompt, shot, overlay,
stock_query}], cta, sources, description, hashtags.

Usage (from the repo root):
    uv run python .claude/skills/youtube-script/scripts/validate_script.py script.json \
        --channel wing-and-turbine [--format short|long] [--json]

Exit 0 = PASS, 1 = FAIL (errors found), 2 = unreadable input or unknown channel.

The checks are a backstop, not a gate: the pipeline's own lint (studio/stages/policy.py)
and the human approval step stay authoritative. When `studio` is importable (run with
`uv run` from the repo), the validator also runs the pipeline policy lint (credential words,
income claims, the brand's red_lines and banned_templates) and validates against the real
pydantic `Script` model. Otherwise those checks are skipped with a note.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
if (REPO_ROOT / "studio").is_dir() and str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

WPM = 150  # matches studio/stages/script.py and tts.py
HOOK_SECONDS = 4.8  # whole hook <= ~12 words; matches studio/stages/script_quality.py.
# The claim/number/question itself must land in the first ~5 words (~2 s, doc 06 §2).

# Mirrors studio/stages/script.py FORMAT_SPECS; kept local so the file also runs without uv.
FORMATS = {
    "short": {"seconds": (35, 50), "beats": (8, 12)},
    "long": {"seconds": (480, 600), "beats": (40, 70)},
}
SHOTS = ("wide", "medium", "close-up", "macro detail", "aerial", "cutaway", "diagram")
NO_STOCK_SHOTS = {"diagram", "cutaway"}
OVERLAY_MAX_WORDS = 5
TITLE_MAX = 70

BEAT_KEYS = {"text", "visual_prompt", "shot", "overlay", "stock_query"}
SCRIPT_KEYS = {"title", "hook", "beats", "cta", "sources", "description", "hashtags"}

# Words that ask an image model to draw text. Negated mentions ("no text, no logos") are
# stripped first, because they are instructions, not content.
_TEXT_NOUNS = (
    r"text|words?|letters?|lettering|labels?|labell?ed|logos?|captions?|signage|signs?|"
    r"titles?|watermarks?|typography|brand(?:ed|ing)?|numbers?|numerals?|digits?|"
    r"writing|written|inscriptions?|headlines?|subtitles?|insignia|livery"
)
_NEGATED_TEXT = re.compile(
    rf"\b(?:no|without|omit(?:ting)?|free of|devoid of)\s+(?:visible |any |on-image )?"
    rf"(?:{_TEXT_NOUNS}|people|faces|figures)"
    rf"(?:\s*(?:,|/|or|and|nor)\s*(?:no\s+)?(?:{_TEXT_NOUNS}|people|faces|figures))*",
    re.IGNORECASE,
)
_TEXT_IN_IMAGE = re.compile(rf"\b(?:{_TEXT_NOUNS})\b", re.IGNORECASE)
_TEXT_PHRASES = re.compile(
    r"\b(?:that says|saying|reads|spelled|spelling out|with the words?|titled)\b"
    r"|[\"“][^\"”]{2,}[\"”]",
    re.IGNORECASE,
)
_ALLOWED_DIGITS = re.compile(r"\b\d+\s?mm\b|\b\d(?:\.\d)?D\b|#[0-9A-Fa-f]{6}\b|\bf/\d+(?:\.\d)?")

# Generic AI-voice tells (upstream quality gate 3) plus the pipeline's banned filler.
CLICHES = (
    "basically", "actually", "incredible", "mind-blowing", "mind blowing", "marvel of engineering",
    "game-changer", "game changer", "let that sink in", "but here's the thing",
    "here's the thing", "you won't believe", "buckle up", "delve", "dive into", "deep dive",
    "in today's fast-paced world", "in a world where", "unlock the secret", "the secret to",
    "revolutionize", "revolutionary", "a testament to", "tapestry", "ever-evolving",
    "stay tuned", "the answer will shock you", "shocking truth", "nobody is talking about this",
    "it's important to note", "at the end of the day", "without further ado",
)
_CLICHE_RX = re.compile(
    r"(?<![\w-])(?:" + "|".join(re.escape(c) for c in CLICHES) + r")(?![\w-])", re.IGNORECASE
)

# Always-on fallback for the pipeline's credential/human rules (studio/stages/policy.py).
_CRED = (
    r"pilot|captain|electrician|contractor|plumber|attorney|lawyer|cpa|cfp|financial advis[eo]r"
    r"|advis[eo]r|doctor|nurse|expert|guru|hacker|agent|realtor|broker|lender|engineer"
)
CREDENTIAL_PATTERNS = (
    re.compile(r"\bDr\.?\s+[A-Z][a-z]+"),
    re.compile(r"[A-Z][a-z]+,\s*(?:MD|CFP|CPA|PhD)\b"),
    re.compile(
        rf"\b(?:I'?m|I am|we'?re|we are|as (?:a|an|your))\s+(?:a |an |your )?"
        rf"(?:licensed |certified |registered |professional |former |real )?(?:{_CRED})s?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\byour (?:trusted |friendly |go-to )?(?:financial |tech |seo |security |home |car )?"
        r"(?:advis[eo]r|expert|guru)\b",
        re.IGNORECASE,
    ),
)
HUMAN_CLAIM = re.compile(
    r"\b(?:I'?m|I am)\s+(?:a\s+)?(?:real|human)\b|\bnot an? (?:AI|robot)\b"
    r"|\bas a human\b|\b(?:when I|I once|I used to|in my experience)\b",
    re.IGNORECASE,
)
ADVICE = re.compile(
    r"\byou should (?:buy|sell|invest|refinance|take out|lease|finance|sue|take)\b"
    r"|\bwe recommend (?:buying|selling|investing|refinancing)\b"
    r"|\b(?:my|our) (?:financial|legal|medical|tax|investment) advice\b"
    r"|\bvote (?:for|against)\b",
    re.IGNORECASE,
)
_BAD_OPENERS = re.compile(
    r"^\s*(?:in this video|today|welcome|hey|hi\b|did you know|have you ever wondered|"
    r"ever wondered|let'?s|so,|imagine)",
    re.IGNORECASE,
)
_OUTRO = re.compile(
    r"\b(?:subscribe|follow for|thanks for|see you|like this video|hit the bell)\b", re.IGNORECASE
)
_AFFILIATE_SIGNAL = re.compile(
    r"\b(?:affiliate|commission|promo code|use code|discount code|sponsored|sponsor|"
    r"link in (?:bio|description)|links? below|amzn\.to|#ad\b|paid partnership)",
    re.IGNORECASE,
)
_AFFILIATE_DISCLOSURE = re.compile(r"\b(?:affiliate|commission|paid (?:for|partnership)|sponsor)",
                                   re.IGNORECASE)
_NUMBER = re.compile(r"\d|\b(?:percent|half|double|triple|twice|million|billion|thousand)\b",
                     re.IGNORECASE)
_WORD = re.compile(r"\b[\w’'-]+\b", re.UNICODE)


def words(text: str) -> int:
    return len(_WORD.findall(text or ""))


def seconds(n_words: int, speed: float = 1.0) -> float:
    return n_words / WPM * 60 / max(speed, 0.1)


# ----------------------------------------------------------------- optional studio imports


def _studio_brand(channel: str) -> tuple[Any | None, str | None]:
    try:
        from studio.config import BrandPackError, load_brand_file
    except Exception as exc:  # noqa: BLE001 - any import problem means "skip with a note"
        return None, f"studio not importable ({type(exc).__name__}); brand checks skipped"
    path = REPO_ROOT / "brand_packs" / f"{channel}.yaml"
    if not path.exists():
        raise SystemExit(f"unknown channel {channel!r}: {path} not found")
    try:
        return load_brand_file(path), None
    except BrandPackError as exc:
        raise SystemExit(f"bad brand pack {channel!r}: {exc}") from exc


def _studio_lint(data: dict, brand: Any) -> tuple[list[str], str | None]:
    try:
        from studio.contracts import Script
    except Exception as exc:  # noqa: BLE001
        return [], f"studio.contracts not importable ({type(exc).__name__}); lint skipped"
    try:
        script = Script.model_validate(data)
    except Exception as exc:  # noqa: BLE001 - pydantic ValidationError
        return [f"pydantic Script validation: {str(exc).splitlines()[0]}"], None
    if brand is None:
        return [], None
    try:
        from studio.stages.script import lint_all  # policy lint + overlays and stock queries
    except Exception:  # noqa: BLE001
        try:
            from studio.stages.policy import lint_script as lint_all
        except Exception as exc:  # noqa: BLE001
            return [], f"studio.stages.policy not importable ({type(exc).__name__}); lint skipped"
    return [f"policy {v}" for v in lint_all(script, brand)], None


# ----------------------------------------------------------------- checks


def check_schema(data: Any) -> list[str]:
    if not isinstance(data, dict):
        return ["top level must be a JSON object matching studio.contracts.Script"]
    errors = []
    for key in ("title", "hook", "beats"):
        if key not in data:
            errors.append(f"missing required field '{key}'")
    for key in ("title", "hook", "cta", "description"):
        if key in data and not isinstance(data[key], str):
            errors.append(f"'{key}' must be a string")
    for key in ("sources", "hashtags"):
        if key in data and not (
            isinstance(data[key], list) and all(isinstance(x, str) for x in data[key])
        ):
            errors.append(f"'{key}' must be a list of strings")
    extra = set(data) - SCRIPT_KEYS
    if extra:
        errors.append(f"unknown top-level keys {sorted(extra)} (the pipeline would drop them)")
    beats = data.get("beats")
    if "beats" in data and not isinstance(beats, list):
        errors.append("'beats' must be a list")
        return errors
    for i, beat in enumerate(beats or [], 1):
        if not isinstance(beat, dict):
            errors.append(f"beat {i} must be an object")
            continue
        for key in ("text", "visual_prompt"):
            if not isinstance(beat.get(key), str) or not beat.get(key, "").strip():
                errors.append(f"beat {i} needs a non-empty string '{key}'")
        for key in ("shot", "overlay", "stock_query"):
            if key in beat and not isinstance(beat[key], str):
                errors.append(f"beat {i} '{key}' must be a string")
        extra = set(beat) - BEAT_KEYS
        if extra:
            errors.append(f"beat {i} has unknown keys {sorted(extra)}")
    return errors


def _fields(data: dict) -> list[tuple[str, str]]:
    out = [("title", data.get("title", "")), ("hook", data.get("hook", ""))]
    for i, b in enumerate(data.get("beats", []), 1):
        out += [(f"beat {i} text", b.get("text", "")), (f"beat {i} overlay", b.get("overlay", ""))]
    out += [("cta", data.get("cta", "")), ("description", data.get("description", ""))]
    return [(k, v) for k, v in out if v]


def narration(data: dict) -> str:
    parts = [data.get("hook", ""), *(b.get("text", "") for b in data.get("beats", [])),
             data.get("cta", "")]
    return " ".join(p for p in parts if p)


def check_script(data: dict, fmt: str, speed: float, hook_seconds: float
                 ) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    info: list[str] = []
    spec = FORMATS[fmt]
    beats = data.get("beats", [])

    # timing
    hook_w = words(data.get("hook", ""))
    hook_s = seconds(hook_w, speed)
    if hook_s > hook_seconds + 1e-9:
        errors.append(f"hook is {hook_w} words (~{hook_s:.1f} s at {WPM} wpm x{speed:g}); it must "
                      f"land within {hook_seconds:g} s, so cut it to "
                      f"{int(hook_seconds * WPM / 60 * speed)} words or fewer")
    if _BAD_OPENERS.search(data.get("hook", "")):
        errors.append("hook opens with setup or a stock opener; start on the claim itself")
    lo, hi = spec["beats"]
    if not lo <= len(beats) <= hi:
        errors.append(f"{len(beats)} beats; a {fmt} needs {lo}-{hi}")
    total_w = words(narration(data))
    total_s = seconds(total_w, speed)
    s_lo, s_hi = spec["seconds"]
    if not s_lo <= total_s <= s_hi:
        errors.append(f"spoken length ~{total_s:.0f} s ({total_w} words); a {fmt} needs "
                      f"{s_lo}-{s_hi} s at {WPM} wpm")
    info.append(f"spoken words {total_w}, ~{total_s:.1f} s at {WPM} wpm x{speed:g}; "
                f"hook {hook_w} words (~{hook_s:.1f} s); beats {len(beats)}")

    # loop ending
    cta = data.get("cta", "").strip()
    if not cta:
        errors.append("cta is empty; end on a loop line that flows back into the hook")
    elif _OUTRO.search(cta):
        errors.append("cta is an outro or subscribe ask; make it a loop line into the hook")

    # beats
    shots: list[str] = []
    for i, b in enumerate(beats, 1):
        vp = b.get("visual_prompt", "")
        stripped = _NEGATED_TEXT.sub(" ", vp)
        m = _TEXT_IN_IMAGE.search(stripped) or _TEXT_PHRASES.search(stripped)
        if m:
            errors.append(f"beat {i} visual_prompt asks for text in the image ({m.group(0)!r}); "
                          "AI images carry no text, put the label in 'overlay'")
        if re.search(r"\d", _ALLOWED_DIGITS.sub("", vp)):
            errors.append(f"beat {i} visual_prompt contains numbers; image models draw them as "
                          "garbled text")
        if words(vp) < 18:
            info.append(f"warning: beat {i} visual_prompt is vague ({words(vp)} words); "
                        "aim for 25-45 with subject, setting, viewpoint and lighting")
        ov = b.get("overlay", "")
        if words(ov) > OVERLAY_MAX_WORDS:
            errors.append(f"beat {i} overlay is {words(ov)} words; max {OVERLAY_MAX_WORDS}")
        if ov and ov.strip().lower() == b.get("text", "").strip().lower():
            errors.append(f"beat {i} overlay copies the narration")
        shot = b.get("shot", "").strip().lower()
        shots.append(shot)
        if shot and shot not in SHOTS:
            info.append(f"warning: beat {i} shot {shot!r} is not one of {', '.join(SHOTS)} "
                        "(the pipeline normalises common aliases)")
        q = words(b.get("stock_query", ""))
        if shot in NO_STOCK_SHOTS and q:
            info.append(f"warning: beat {i} is a {shot}; leave stock_query empty")
        elif q and not 2 <= q <= 4:
            info.append(f"warning: beat {i} stock_query has {q} words; use 2-4")
    for i in range(1, len(shots)):
        if shots[i] and shots[i] == shots[i - 1]:
            info.append(f"warning: beats {i} and {i + 1} repeat the shot {shots[i]!r}")
    if fmt == "short" and len({s for s in shots if s}) < 4:
        info.append("warning: fewer than 4 distinct shot types")

    # language and policy
    for name, text in _fields(data):
        for m in _CLICHE_RX.finditer(text):
            errors.append(f"{name}: AI cliche {m.group(0)!r}")
        for rx in CREDENTIAL_PATTERNS:
            m = rx.search(text)
            if m:
                errors.append(f"{name}: credential claim {m.group(0)!r} (faceless narrator)")
        m = HUMAN_CLAIM.search(text)
        if m:
            errors.append(f"{name}: narrator claims to be human or have personal experience "
                          f"({m.group(0)!r}); the voice is AI-generated")
        m = ADVICE.search(text)
        if m:
            errors.append(f"{name}: advice or politics ({m.group(0)!r}); write explainers only")

    # evidence and disclosure
    spoken = narration(data)
    if _NUMBER.search(spoken) and not data.get("sources"):
        errors.append("narration has a quantity but 'sources' is empty; cite supplied evidence "
                      "or delete the number")
    everything = " ".join(t for _, t in _fields(data))
    if _AFFILIATE_SIGNAL.search(everything):
        if not _AFFILIATE_DISCLOSURE.search(spoken):
            errors.append("affiliate or sponsor mention without a spoken disclosure "
                          "(e.g. 'we earn a commission if you use our links')")
        if not _AFFILIATE_DISCLOSURE.search(data.get("description", "")):
            errors.append("affiliate or sponsor mention without a disclosure in the description")

    # packaging
    if len(data.get("title", "")) > TITLE_MAX:
        errors.append(f"title is {len(data['title'])} characters; keep it under {TITLE_MAX}")
    tags = data.get("hashtags", [])
    bad = [t for t in tags if not t.startswith("#") or " " in t]
    if bad:
        errors.append(f"hashtags must start with # and have no spaces: {bad}")
    if tags and not 3 <= len(tags) <= 6:
        info.append(f"warning: {len(tags)} hashtags; use 3-6")
    if not data.get("description", "").strip():
        info.append("warning: description is empty (it becomes the platform caption)")
    return errors, info


def validate(data: Any, channel: str | None, fmt: str, hook_seconds: float = HOOK_SECONDS,
             use_studio: bool = True) -> dict:
    errors = check_schema(data)
    info: list[str] = []
    if errors:
        return {"ok": False, "errors": errors, "info": info}
    brand, note = (None, None)
    if channel and use_studio:
        brand, note = _studio_brand(channel)
    elif channel:
        note = "--no-studio: brand red_lines and banned_templates not checked"
    else:
        note = "no --channel: brand red_lines and banned_templates not checked"
    if note:
        info.append(f"note: {note}")
    speed = float(getattr(getattr(brand, "voice", None), "speed", 1.0) or 1.0)
    e, i = check_script(data, fmt, speed, hook_seconds)
    errors += e
    info += i
    if use_studio:
        lint, note = _studio_lint(data, brand)
        errors += lint
        if note:
            info.append(f"note: {note}")
        elif brand is not None:
            info.append(f"studio policy lint ran for {brand.slug}")
    # de-duplicate, keep order
    errors = list(dict.fromkeys(errors))
    return {"ok": not errors, "errors": errors, "info": info}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("script", type=Path, help="Script JSON file")
    parser.add_argument("--channel", help="Brand pack slug (brand_packs/<slug>.yaml)")
    parser.add_argument("--format", choices=sorted(FORMATS), default="short")
    parser.add_argument("--hook-seconds", type=float, default=HOOK_SECONDS,
                        help=f"Max spoken hook length (default {HOOK_SECONDS:g} s)")
    parser.add_argument("--no-studio", action="store_true",
                        help="Skip the studio imports (pydantic model and policy lint)")
    parser.add_argument("--json", action="store_true", help="Print the result as JSON")
    args = parser.parse_args(argv)

    try:
        data = json.loads(args.script.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: cannot read {args.script}: {exc}")
        return 2
    try:
        result = validate(data, args.channel, args.format, args.hook_seconds,
                          use_studio=not args.no_studio)
    except SystemExit as exc:
        print(f"ERROR: {exc}")
        return 2
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        for item in result["info"]:
            print(f"INFO: {item}")
        for item in result["errors"]:
            print(f"ERROR: {item}")
        print("PASS" if result["ok"] else "FAIL")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
