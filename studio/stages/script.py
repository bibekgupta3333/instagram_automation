"""Stage 1: script. Brand-aware prompt → ChatClient(role="draft") → validated `Script` → policy lint.

Retention rules baked into the prompt come from doc 06 §2–3: the hook lands in 1–3 s,
something changes every 2–4 s, and the last line loops back into the first.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import ValidationError

from studio.contracts import BrandPack, Presenter, Script, VideoJob
from studio.stages.base import StageContext, StageError, hash_inputs, is_cached, log, write_cache
from studio.stages.policy import lint_script

STAGE = "script"
VERSION = 1

FORMAT_SPECS = {
    "short": {"words": "110-150", "beats": "5-8", "seconds": "45-60", "aspect": "9:16 vertical"},
    "long": {"words": "1200-1500", "beats": "40-70", "seconds": "480-600", "aspect": "16:9"},
}

RETENTION_RULES = """\
Retention rules (non-negotiable):
1. The hook is spoken in the first 1-3 seconds: at most 12 words, no setup, no greeting, no
   channel name. The same promise must work as on-screen text in under 7 words.
2. Something changes every 2-4 seconds: each beat is 1-2 short sentences (about 8-25 words)
   with its own distinct visual. Never two beats with the same visual.
3. Open a loop early (tease the payoff) and pay it off at the end.
4. Loop ending: the final line (cta) must flow grammatically back into the hook so the video
   replays seamlessly. No outro, no "subscribe" end card, no "thanks for watching".
5. Cut the air: no filler, no rhetorical throat-clearing."""


def build_messages(job: VideoJob, brand: BrandPack, error: str | None = None) -> list[dict[str, str]]:
    spec = FORMAT_SPECS[job.format]
    presenter = (
        f"AI avatar presenter named {brand.avatar_name} (disclosed as AI; never claims to be human)"
        if brand.presenter == Presenter.AVATAR
        else "faceless narrator with no name and no credentials"
    )
    schema = json.dumps(Script.model_json_schema(), indent=None)
    system = "\n".join(
        [
            f"You write scripts for the US channel '{brand.name}' ({brand.handle}).",
            f"Niche (stay strictly inside it): {brand.niche}.",
            f"Presenter: {presenter}.",
            (f"Format: {job.format}, {spec['aspect']}, {spec['seconds']} s, "
            f"{spec['words']} spoken words, {spec['beats']} beats."),
            "",
            "Hook styles to choose from (rotate; pick the one that fits the topic best):",
            *(f"- {h}" for h in brand.hook_styles),
            "",
            RETENTION_RULES,
            "",
            "Red lines (a violation fails the video):",
            *(f"- {r}" for r in brand.red_lines),
            "",
            "Banned phrases and templates (never use them, in any field):",
            *(f"- {b}" for b in brand.banned_templates),
            "",
            ("Each beat's visual_prompt describes one still image (subject, composition, camera)"
            " with no on-image text; the brand style is appended automatically."),
            "Put every factual source (URL or publication + year) in `sources`.",
            ("`description` is the platform caption (2-3 sentences, includes any required"
            " disclaimer). `hashtags` are 3-6 tags without spaces."),
            "",
            "Reply with ONE JSON object only, no markdown, matching this JSON schema:",
            schema,
        ]
    )
    user = f"Topic: {job.topic}"
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    if error:
        messages.append(
            {
                "role": "user",
                "content": "Your previous reply was not valid for the schema:\n"
                f"{error}\nReply again with only the corrected JSON object.",
            }
        )
    return messages


def parse_script(raw: str) -> Script:
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object found in reply")
    script = Script.model_validate_json(text[start : end + 1])
    if not script.beats:
        raise ValueError("script has no beats")
    return script


def generate_script(job: VideoJob, brand: BrandPack, ctx: StageContext) -> Script:
    schema = Script.model_json_schema()
    error: str | None = None
    for attempt in range(2):  # one retry with the validation error fed back
        raw = ctx.chat.complete(
            build_messages(job, brand, error), role="draft", json_schema=schema,
            max_tokens=2048 if job.format == "short" else 8192,
        )
        try:
            return parse_script(raw)
        except (ValidationError, ValueError) as exc:
            error = str(exc)[:1500]
            log(job, f"script: attempt {attempt + 1} invalid: {error.splitlines()[0]}")
    raise StageError(f"script: LLM reply failed validation twice: {error}")


def run(job: VideoJob, brand: BrandPack, ctx: StageContext) -> VideoJob:
    workdir = Path(job.workdir)
    out = workdir / "script.json"
    key = hash_inputs(STAGE, VERSION, job.topic, job.format, brand)
    if is_cached(job, STAGE, key, [out], ctx):
        if job.script is None:
            job.script = Script.model_validate_json(out.read_text())
        log(job, "script: cached, skipped")
        return job

    script = generate_script(job, brand, ctx)
    violations = lint_script(script, brand)
    if violations:
        (workdir / "script.rejected.json").write_text(script.model_dump_json(indent=2))
        detail = "; ".join(str(v) for v in violations)
        raise StageError(f"policy lint failed ({len(violations)}): {detail}")

    out.write_text(script.model_dump_json(indent=2))
    job.script = script
    write_cache(job, STAGE, key, [out])
    log(job, f"script: {len(script.beats)} beats, {len(script.narration().split())} words")
    return job
