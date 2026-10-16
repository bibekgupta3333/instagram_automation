"""Stage 2: fact-check. Accuracy-critical channels (doc 15 §5 note) go to role="fact_check",
which the LLM router sends to Bedrock; every other channel records a skip."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from studio.contracts import BrandPack, FactCheck, VideoJob
from studio.stages.base import StageContext, StageError, hash_inputs, is_cached, log, write_cache

STAGE = "fact_check"
VERSION = 1

SYSTEM = """You are a strict fact-checker for a US educational video channel ({name}, niche: {niche}).
Check every factual claim in the script (numbers, dates, mechanisms, named facts). Flag anything
false, unverifiable, outdated, missing a year on a cost figure, or crossing a red line:
{red_lines}
Reply with ONE JSON object only matching this schema: {schema}
Set passed=false if any issue would mislead a viewer; list each issue as one short sentence."""


def _parse(raw: str) -> FactCheck:
    text = raw.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in fact-check reply")
    return FactCheck.model_validate_json(text[start : end + 1])


def run(job: VideoJob, brand: BrandPack, ctx: StageContext) -> VideoJob:
    if job.script is None:
        raise StageError("fact_check: no script")
    out = Path(job.workdir) / "fact_check.json"
    key = hash_inputs(STAGE, VERSION, brand.accuracy_critical, job.script, brand.red_lines)
    if is_cached(job, STAGE, key, [out], ctx):
        job.fact_check = FactCheck.model_validate_json(out.read_text())
        log(job, "fact_check: cached, skipped")
        return _gate(job)

    if not brand.accuracy_critical:
        result = FactCheck(passed=True, issues=[], model="skipped")
        log(job, "fact_check: skipped (channel is not accuracy-critical)")
    else:
        schema = FactCheck.model_json_schema()
        messages = [
            {
                "role": "system",
                "content": SYSTEM.format(
                    name=brand.name, niche=brand.niche,
                    red_lines="\n".join(f"- {r}" for r in brand.red_lines),
                    schema=json.dumps(schema),
                ),
            },
            {"role": "user", "content": job.script.model_dump_json(indent=2)},
        ]
        error = None
        result = None
        for _ in range(2):
            msgs = messages if error is None else [
                *messages, {"role": "user", "content": f"Invalid reply ({error}). JSON only."}
            ]
            raw = ctx.chat.complete(msgs, role="fact_check", json_schema=schema, max_tokens=1500)
            try:
                result = _parse(raw)
                break
            except (ValidationError, ValueError) as exc:
                error = str(exc)[:500]
        if result is None:
            raise StageError(f"fact_check: invalid reply twice: {error}")
        log(job, f"fact_check: passed={result.passed} issues={len(result.issues)} ({result.model})")

    out.write_text(result.model_dump_json(indent=2))
    job.fact_check = result
    write_cache(job, STAGE, key, [out])
    return _gate(job)


def _gate(job: VideoJob) -> VideoJob:
    fc = job.fact_check
    if fc is not None and not fc.passed:
        raise StageError("fact_check failed: " + "; ".join(fc.issues or ["no detail"]))
    return job
