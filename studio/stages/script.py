"""Stage 1: script. An angle brief, two passes, the quality gate, then the policy lint.

0. **Angle** (role="draft", optional, `STUDIO_SCRIPT_ANGLE=0` disables): one short call picks
   the most surprising true fact, the target viewer and the promise; it feeds the draft prompt.
   An invalid reply is logged and skipped (doc: chaseraw/youtube-script-skill brief-and-angle,
   MIT). It is internal planning text, never narrated or treated as evidence.
1. **Draft** (role="draft"): a brand-aware writer prompt -> JSON validated as `Script`.
2. **Polish** (role="polish"): an editor prompt gets the draft plus a deterministic QA report
   (beat count, word count/timing at ~150 wpm, hook clarity, shot variety, overlay length,
   visual specificity) and returns the tightened script, again validated as `Script`.
3. `normalize` canonicalises shot names, overlays and stock queries.
4. `script_quality.gate` (deterministic): on ERROR issues one repair pass via role="polish"
   with the issue list, then re-gate; remaining errors fail the job, warnings go to the log.
5. `lint_script` (policy.py) is the hard compliance gate.

Each LLM call gets one retry with the validation error fed back. The fact-check stage reuses
`polish_script` for its one rewrite pass. Retention rules come from doc 06 §2-3: the hook
lands in the first 1-2 s, the picture changes every 2.5-4 s, and the last line loops into
the hook.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from studio.contracts import Beat, BrandPack, Presenter, Script, VideoJob
from studio.stages import script_quality
from studio.stages.base import StageContext, StageError, hash_inputs, is_cached, log, write_cache
from studio.stages.policy import Violation, lint_script

STAGE = "script"
VERSION = 5  # 5: angle brief before drafting; deterministic quality gate with one repair pass

WPM = 150  # matches studio/stages/tts.py

FORMAT_SPECS = {
    "short": {
        "aspect": "9:16 vertical", "seconds": (35, 50), "words": (90, 125), "beats": (8, 12),
        "beat_words": (6, 14), "hook_words": (5, 12), "beat_seconds": "2.5-4",
        "max_tokens": 8000,
    },
    "long": {
        "aspect": "16:9 horizontal", "seconds": (480, 600), "words": (1200, 1500),
        "beats": (40, 70), "beat_words": (12, 32), "hook_words": (6, 18), "beat_seconds": "5-12",
        "max_tokens": 24000,
    },
}

SHOTS = ("wide", "medium", "close-up", "macro detail", "aerial", "cutaway", "diagram")
_SHOT_ALIASES = {
    "close up": "close-up", "closeup": "close-up", "extreme close-up": "macro detail",
    "macro": "macro detail", "detail": "macro detail", "wide establishing": "wide",
    "establishing": "wide", "wide shot": "wide", "drone": "aerial", "bird's-eye": "aerial",
    "overhead": "aerial", "cross-section": "cutaway", "cross section": "cutaway",
    "exploded view": "cutaway", "schematic": "diagram", "infographic": "diagram",
    "medium shot": "medium",
}
NO_STOCK_SHOTS = {"diagram", "cutaway"}

_BAD_OPENERS = re.compile(
    r"^\s*(?:in this video|today|welcome|hey|hi\b|did you know|have you ever wondered|"
    r"ever wondered|let'?s|so,|imagine)", re.IGNORECASE,
)
_ON_IMAGE_TEXT = re.compile(
    r"\b(?:text|words?|letters?|labels?|labell?ed|logos?|captions?|signage|signs?|titles?|"
    r"watermarks?|numbers? on|typography|brand(?:ed|ing)?)\b", re.IGNORECASE,
)
_ALLOWED_DIGITS = re.compile(r"\b\d+\s?mm\b|\b\d(?:\.\d)?D\b|#[0-9A-Fa-f]{6}\b|\bf/\d+(?:\.\d)?")
_NOUNS = r"(?:visible |any |on-image |overlaid )?(?:text|labels?|letters|lettering|logos?|people|persons|" \
    r"humans|figures|watermarks?|captions?|signage|signs|numbers|words|typography|branding)"
_NEGATED = re.compile(
    rf"[,;.]?\s*\b(?:no|without|omitting|omit|free of|devoid of|absent)\s+{_NOUNS}"
    rf"(?:\s*(?:,|/|or|and|nor)\s*(?:no\s+)?{_NOUNS})*", re.IGNORECASE,
)
_HEX = re.compile(r"\s*#[0-9A-Fa-f]{6}\b")
_OUTRO = re.compile(r"\b(?:subscribe|follow for|thanks for|see you|like this video)\b", re.IGNORECASE)


# ---------------------------------------------------------------- prompts


def style_hint(brand: BrandPack) -> str:
    """The brand style without hex codes (models copy them into prompts as text)."""
    return _HEX.sub("", brand.style_prompt).strip()


def strip_style(visual_prompt: str, brand: BrandPack | None) -> str:
    """Drop comma-separated chunks of the brand style the model pasted into a visual_prompt
    (images.py appends the style itself)."""
    if brand is None or not brand.style_prompt:
        return visual_prompt
    chunks = {c.strip().lower() for c in re.split(r"[,;]", brand.style_prompt + "," +
                                                  style_hint(brand)) if c.strip()}
    parts = [p for p in re.split(r",\s*", visual_prompt) if p.strip().lower() not in chunks]
    out = ", ".join(p.strip() for p in parts if p.strip())
    return re.sub(r",?\s*(?:in )?(?:the )?brand (?:style|look)\b\.?", "", out).strip(" ,") or visual_prompt


def _presenter(brand: BrandPack) -> str:
    if brand.presenter == Presenter.AVATAR:
        return (f"AI avatar presenter named {brand.avatar_name} (disclosed as AI; never claims "
                "to be human)")
    return ("faceless narrator with no name, no credentials and no personal anecdotes "
            "(never 'I worked on', 'as a ...')")


def _spec_line(fmt: str, brand: BrandPack) -> str:
    s = FORMAT_SPECS[fmt]
    lo, hi = s["words"]
    return (f"Format: {fmt}, {s['aspect']}, {s['seconds'][0]}-{s['seconds'][1]} s spoken at about "
            f"{WPM} words per minute, so {lo}-{hi} spoken words in total (hook + beats + cta), "
            f"{s['beats'][0]}-{s['beats'][1]} beats of {s['beat_words'][0]}-{s['beat_words'][1]} "
            f"words each (one new picture every {s['beat_seconds']} s).")


def _structure(fmt: str) -> str:
    if fmt == "short":
        body = """\
STRUCTURE (Shorts)
- hook (spoken in the first 1-2 s, 5-12 words): ONE complete sentence, a concrete, surprising
  claim or causal question. A qualitative, concrete hook is welcome; no number is required.
  It must make sense with zero context and promise an answer. Never start with "In this video",
  "Did you know", "Have you ever wondered", "Imagine", a greeting or the channel name.
- beats 1-2: the stakes. Explain what changes and why, without inventing quantities.
- beats 3-9: 3-5 ESCALATING facts or mechanism steps. Each one answers "how?" or "and then?"
  for the previous beat and is more surprising than the last. Use cause words: because, so,
  which means.
- the second-to-last or last beat: the PAYOFF that directly answers the hook.
- cta (the loop line, 4-10 words): ends mid-thought so it flows straight back into the hook
  when the video replays, e.g. cta "Which explains why" -> hook "Highway speed makes a car push harder against the air." No subscribe ask, no outro, no thanks."""
    else:
        body = """\
STRUCTURE (long-form)
- hook (first 5 s, 6-18 words): a concrete, surprising claim or causal question. A qualitative hook is welcome.
  Never "In this video", a greeting or the channel name.
- beats 1-4: the stakes and a promise of what the viewer will understand by the end.
- 4-6 chapters of 6-12 beats each. Each chapter is one escalating fact or mechanism step and
  ends on a mini cliff-hanger that pulls into the next chapter.
- the payoff: the hook's question answered completely.
- cta (the loop line): calls back to the hook's mechanism so the ending lands on the opening."""
    return body


def _rules(job: VideoJob, brand: BrandPack) -> str:
    s = FORMAT_SPECS[job.format]
    return "\n".join([
        f"Channel: '{brand.name}' ({brand.handle}), faceless explainer for US viewers.",
        f"Niche (stay strictly inside it): {brand.niche}.",
        f"Presenter: {_presenter(brand)}.",
        _spec_line(job.format, brand),
        "",
        "Brand hook styles (adapt the pattern to this topic; evidence constraints override any",
        "numeric example below; never copy examples or add a number just to fit a style):",
        *(f"- {h}" for h in brand.hook_styles),
        "",
        _structure(job.format),
        "",
        "SUBSTANCE",
        "- The video must actually ANSWER the topic. First decide the 3-5 real mechanisms or",
        "  steps that explain it (e.g. for a dam: arch shape, sheer mass, a base far thicker than",
        "  the crest, rock abutments, spillways). The beats walk through them in order, each one",
        "  building on the last. A pile of unrelated statistics is a failure.",
        "EVIDENCE CONSTRAINTS (override style examples and retention targets)",
        "- Do not invent numbers, statistics, percentages, costs, ranges, ratios or multipliers.",
        "- Quantitative claims require supplied source evidence tied to that exact claim, with",
        "  its assumptions, scope, date and units. Model memory or confidence is not evidence.",
        "- This workflow supplies a topic only, with no verified source evidence. Use qualitative",
        "  causal explanations; omit quantitative factual claims from ALL output fields.",
        "- A topic mentioning a number is not evidence. A citation generated by you is not",
        "  supplied evidence. Do not invent sources, titles, publication years, URLs or quotes.",
        "- Do not disguise unsupported statistics using 'about', 'up to', spelled-out numbers,",
        "  'double', 'half', or a comparison. Delete the quantity instead of hedging it.",
        "- Keep mechanisms accurate and conditional where needed. Do not generalize a result",
        "  across vehicles, weather, loads or operating conditions. If uncertain, omit the claim.",
        "- Never infer a universal fuel-use or trip-cost change from aerodynamic drag alone.",
        "- Production word counts and shot counts below are formatting targets, not factual",
        "  claims to narrate. No numerical hook, statistic quota or numeric overlay is required.",
        "",
        "LANGUAGE",
        "- Conversational US English, like explaining to a smart friend. Short, grammatical",
        "  sentences, 14 words max, mostly under 10. Present tense. Contractions are fine.",
        "- US customary units only (feet, pounds, gallons, degrees F, psi, mph); no kPa or MPa.",
        "- Plain verbs, concrete nouns. No unit soup ('cubic-foot-seconds each second').",
        "- Cut filler and cliches: no 'basically', 'actually', 'incredible', 'mind-blowing',",
        "  'marvel of engineering', 'game-changer', 'let that sink in', 'but here's the thing'.",
        "- Each beat's text must follow naturally from the previous one when read aloud.",
        "",
        "HOOK EXAMPLES",
        "- weak: 'Hoover Dam holds a lot of water.' (vague, no causal question)",
        "- weak: 'In this video we explain jet engines.' (setup, banned)",
        "- strong: 'Why is a dam thicker at its base?'",
        "- strong: 'The fire inside a jet engine is hotter than the metal's melting point.'",
        "",
        "BEATS: each beat is one idea and one picture, with these fields:",
        "- text: the narration for that beat.",
        f"- shot: exactly one of: {', '.join(SHOTS)}. Never the same shot twice in a row; use at",
        "  least 4 different shots, including one macro detail and one diagram or cutaway.",
        "- visual_prompt: 25-45 words for ONE art-directable still image of exactly what that",
        "  beat's narration is saying, written in this order: subject (the specific object or",
        "  part, its material and state), setting or context, viewpoint and composition (e.g.",
        "  'isometric three-quarter view from above, subject centered, lots of negative space',",
        "  'side elevation', 'worm's-eye view, 24mm wide'), lighting (e.g. 'soft studio key light",
        "  from upper left, orange rim glow'), and one short style note in the brand look below.",
        f"  Brand look (appended automatically, do not paste it): \"{style_hint(brand)}\".",
        "  Every visual_prompt must live inside that look: same medium, palette and lighting",
        "  family on every beat, so the video feels like one piece. Do not mix photo-real",
        "  sunsets with diagrams.",
        "  NEVER put in a visual_prompt: numbers, measurements, units, dates, text, letters,",
        "  labels, captions, signs, logos, brand, airline or company names, or people. The words",
        "  'no people' and 'labeled' are not needed. Describe only what is seen.",
        "- beat 1's visual_prompt is the frame on screen while the hook is spoken: make it the most",
        "  striking image in the video, the subject at full scale or mid-action.",
        "- overlay: 1-5 words of on-screen text using a qualitative keyword or causal phrase.",
        "  Do not add unsupported figures. Leave it empty on about a",
        "  third of beats. Never copy the narration sentence.",
        "- stock_query: 2-4 plain lowercase keywords a stock photo site would match for the",
        "  real-world subject ('hoover dam aerial', 'jet engine fan blades'). Empty string for",
        "  diagram and cutaway beats, or when no real photo could show it.",
        "",
        "Red lines (a violation fails the video):",
        *(f"- {r}" for r in brand.red_lines),
        "",
        "Never mention a red-line topic even to deny it (no 'this is not a DIY guide', no",
        "'not financial advice' unless a red line requires that disclaimer).",
        "",
        "Banned phrases and templates (never use them, in any field):",
        *(f"- {b}" for b in brand.banned_templates),
        "",
        "OTHER FIELDS",
        "- title: under 70 characters, search-friendly, same promise as the hook, no clickbait lie.",
        "- sources: only supplied source evidence actually used. With topic-only input, return [].",
        "  Do not fabricate references or imply that suggested reading was checked.",
        "- description: the platform caption, 2-3 sentences, includes any required disclaimer.",
        "- hashtags: 3-6 tags without spaces, each starting with #.",
        "",
        ("BEFORE YOU ANSWER, CHECK: the hook is a concrete, accurate sentence; "
        f"{s['beats'][0]}-{s['beats'][1]} beats; no unsupported quantitative claims; every"),
        "visual_prompt has 25+ words with subject, setting, viewpoint and lighting and no numbers;",
        "no two adjacent beats share a shot; the cta flows into the hook.",
    ])


_SKELETON = {
    "title": "...", "hook": "...",
    "beats": [{"text": "...", "shot": "wide", "visual_prompt": "...", "overlay": "...",
               "stock_query": "..."}],
    "cta": "...", "sources": ["..."], "description": "...", "hashtags": ["#..."],
}


def _json_tail() -> str:
    # A filled-in skeleton, not the JSON Schema: small models echo a schema back verbatim.
    # The schema itself still goes to the client as `json_schema` (tool / structured output).
    return ("Reply with ONE JSON object only (no markdown, no schema, no commentary), shaped "
            "exactly like this, with every beat filled in:\n" + json.dumps(_SKELETON))


def _with_error(messages: list[dict[str, str]], error: str | None) -> list[dict[str, str]]:
    if error:
        messages.append({
            "role": "user",
            "content": "Your previous reply was not valid for the schema:\n"
            f"{error}\nReply again with only the corrected JSON object.",
        })
    return messages


def build_messages(job: VideoJob, brand: BrandPack, error: str | None = None,
                   angle: AngleBrief | None = None) -> list[dict[str, str]]:
    """Draft-pass prompt (with the optional angle brief in the user turn)."""
    system = "\n".join([
        "You are the head writer of a top faceless YouTube Shorts / Reels studio. Your scripts",
        "hold 80%+ of viewers to the end because every line earns the next one.",
        "",
        _rules(job, brand),
        "",
        _json_tail(),
    ])
    user = f"Topic: {job.topic}"
    if angle is not None:
        user += "\n\n" + angle.as_prompt()
    return _with_error([{"role": "system", "content": system}, {"role": "user", "content": user}],
                       error)


# ---------------------------------------------------------------- angle brief (optional)


class AngleBrief(BaseModel):
    """Internal planning note for the writer; never narrated, never evidence."""

    surprising_fact: str = Field(min_length=3, description="the most surprising TRUE qualitative "
                                 "fact or mechanism behind the topic, no numbers")
    viewer: str = Field(min_length=3, description="who this is for and what they believe now")
    promise: str = Field(min_length=3, description="the one thing the viewer can explain after")
    angle: str = Field(default="", description="how this beats common coverage: the mechanism, "
                       "counterexample or constraint others skip")

    def as_prompt(self) -> str:
        lines = [
            "ANGLE BRIEF (internal planning from an editor; build the script around it, but do",
            "not narrate these labels; every evidence constraint still applies, so drop any part",
            "of it you cannot state qualitatively and accurately):",
            f"- Most surprising true fact: {self.surprising_fact}",
            f"- Target viewer: {self.viewer}",
            f"- Promise (the hook makes it, the payoff keeps it): {self.promise}",
        ]
        if self.angle:
            lines.append(f"- Angle: {self.angle}")
        return "\n".join(lines)


def angle_enabled() -> bool:
    return os.environ.get("STUDIO_SCRIPT_ANGLE", "1").strip().lower() not in {"0", "false", "off", "no"}


def build_angle_messages(job: VideoJob, brand: BrandPack) -> list[dict[str, str]]:
    system = "\n".join([
        "You are the angle strategist of a faceless explainer studio. Before anyone writes,",
        "you pick the single angle for one video. One video moves the viewer from what they",
        "believe now to one thing they can explain afterwards; never two unrelated takeaways.",
        f"Channel: '{brand.name}', niche: {brand.niche}. Format: {job.format}. US viewers.",
        "Choose the angle with the most specific promise, the clearest mechanism you can",
        "explain accurately, and the biggest difference from common coverage. A contrarian",
        "take without a real mechanism is decoration; do not pick it.",
        "- surprising_fact: the most surprising TRUE fact or mechanism behind the topic, as one",
        "  qualitative sentence. No numbers, statistics, dates, names of studies or sources.",
        "  If you are not sure it is true, pick a safer fact.",
        "- viewer: who watches and the belief or confusion they arrive with (one sentence).",
        "- promise: the one thing they can explain after watching (one sentence).",
        "- angle: one sentence, 'improves on common coverage by ... using ...'.",
        "EVIDENCE: no verified sources are supplied. Do not invent numbers, statistics or",
        "sources. Promise only what the mechanism itself explains: no causal leaps to money,",
        "fuel use, health, safety or performance outcomes the topic does not establish, and",
        "no generalizing a result across all conditions. A narrower true promise beats a",
        "bigger shaky one.",
        "Stay inside the niche and these red lines:",
        *(f"- {r}" for r in brand.red_lines),
        "Reply with ONE JSON object only: " + json.dumps(
            {"surprising_fact": "...", "viewer": "...", "promise": "...", "angle": "..."}),
    ])
    return [{"role": "system", "content": system},
            {"role": "user", "content": f"Topic: {job.topic}"}]


def pick_angle(job: VideoJob, brand: BrandPack, ctx: StageContext) -> AngleBrief | None:
    """One role="draft" call. Optional: an invalid reply is logged and the draft runs without it."""
    try:  # SchemaError (structured-output failure) is a ValueError; budget/transport errors raise
        raw = ctx.chat.complete(build_angle_messages(job, brand), role="draft",
                                json_schema=AngleBrief.model_json_schema(), max_tokens=800)
        text = raw.strip()
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1:
            raise ValueError("no JSON object in angle reply")
        angle = AngleBrief.model_validate_json(text[start : end + 1])
    except (ValidationError, ValueError) as exc:
        log(job, f"script: angle brief skipped (invalid reply: {str(exc).splitlines()[0][:200]})")
        return None
    log(job, f"script: angle: {angle.promise[:160]}")
    return angle


def build_polish_messages(
    job: VideoJob, brand: BrandPack, draft: Script, qa: list[str],
    fixes: list[str] | None = None, error: str | None = None,
) -> list[dict[str, str]]:
    """Polish-pass prompt: the editor gets the writer rules, the draft and a QA report."""
    s = FORMAT_SPECS[job.format]
    system = "\n".join([
        "You are the senior editor of a top faceless YouTube Shorts / Reels studio. You get a",
        "writer's draft and return the final, improved script. Keep what works; fix what doesn't.",
        "",
        "EDIT CHECKLIST (apply all of it):",
        "1. Hook: one complete sentence (subject + verb), concrete, causal and surprising.",
        "   A qualitative hook is welcome. No number is required. Remove generic setup.",
        (f"2. Timing: {s['words'][0]}-{s['words'][1]} spoken words in total at {WPM} wpm "
        f"({s['seconds'][0]}-{s['seconds'][1]} s) and {s['beats'][0]}-{s['beats'][1]} beats of "
        f"{s['beat_words'][0]}-{s['beat_words'][1]} words. Split long beats, merge thin ones."),
        "3. Escalation: each beat must be more surprising than the last, with a clear payoff that",
        "   answers the hook. Cut any beat that only repeats an earlier one.",
        "4. Wording: shorter sentences, concrete objects and causes, no filler or cliches.",
        "5. Evidence: do not invent numbers or statistics. Remove every unsupported quantity",
        "   from narration, title, overlays, description and hashtags, even if the draft or a",
        "   model reviewer calls it correct. No verified source evidence is supplied here.",
        "   Do not round, hedge or replace one unsupported figure with another. Explain the",
        "   mechanism qualitatively, preserve uncertainty, and never manufacture citations.",
        "6. Visuals: make every visual_prompt specific (subject, setting, camera angle and lens,",
        "   lighting, style note), consistent across beats, and matching its beat's narration.",
        "   Remove numbers, units, dates, text, labels, logos, brand names and people from every",
        "   visual_prompt. Never shorten a visual_prompt below 25 words. Vary the shots.",
        "7. overlay 1-5 words or empty; stock_query 2-4 keywords, empty for diagram and cutaway.",
        "8. cta must flow grammatically into the hook so the replay loops.",
        "9. Keep title, description and hashtags useful. Sources stay empty without supplied evidence.",
        "",
        "THE WRITER RULES THE SCRIPT MUST FOLLOW:",
        _rules(job, brand),
        "",
        _json_tail(),
    ])
    parts = [f"Topic: {job.topic}", "", "DRAFT SCRIPT (JSON):", draft.model_dump_json(indent=1)]
    parts += ["", "AUTOMATED QA REPORT:"]
    parts += [f"- {q}" for q in qa] or ["- no automated issues found; still apply the checklist"]
    if fixes:
        parts += ["", ("REQUIRED CORRECTIONS (you MUST apply every one; rewrite or drop the "
                  "claim or phrase if no fix is given):"), *(f"- {f}" for f in fixes)]
    parts += ["", "Return the full final script as one JSON object."]
    messages = [{"role": "system", "content": system}, {"role": "user", "content": "\n".join(parts)}]
    return _with_error(messages, error)


# ---------------------------------------------------------------- parsing + QA


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


def canonical_shot(shot: str) -> str:
    s = re.sub(r"\s+", " ", shot.strip().lower().replace("_", " "))
    if s in SHOTS:
        return s
    if s in _SHOT_ALIASES:
        return _SHOT_ALIASES[s]
    for name in SHOTS:  # "low-angle wide" -> wide
        if name in s:
            return name
    for alias, name in _SHOT_ALIASES.items():
        if alias in s:
            return name
    return s


def normalize(script: Script, brand: BrandPack | None = None) -> Script:
    """Deterministic clean-up after the LLM: canonical shots, overlay <= 5 words, stock queries
    as 2-4 plain keywords (empty for diagram/cutaway)."""
    beats = []
    for b in script.beats:
        shot = canonical_shot(b.shot)
        overlay = " ".join(b.overlay.strip().strip('"').split()[:5])
        words = re.findall(r"[a-z0-9][a-z0-9'-]*", b.stock_query.lower())[:4]
        query = "" if shot in NO_STOCK_SHOTS or len(words) < 2 else " ".join(words)
        beats.append(b.model_copy(update={
            "text": b.text.strip(),
            "visual_prompt": strip_style(_NEGATED.sub("", b.visual_prompt).strip(" ,;"), brand),
            "shot": shot,
            "overlay": overlay, "stock_query": query,
        }))
    tags = [t if t.startswith("#") else "#" + t for t in (h.strip().replace(" ", "")
                                                          for h in script.hashtags) if t]
    return script.model_copy(update={"beats": beats, "hashtags": tags})


def spoken_words(script: Script) -> int:
    return len(script.narration().split())


def estimated_seconds(script: Script, speed: float = 1.0) -> float:
    return spoken_words(script) / WPM * 60 / max(speed, 0.1)


def _same(a: str, b: str) -> bool:
    def norm(x: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", x.lower()).strip()

    return bool(norm(a)) and norm(a) == norm(b)


def _quoted(styles: list[str]) -> list[str]:
    return [m for st in styles for m in re.findall(r"'([^']{12,})'", st)]


def qa_issues(script: Script, fmt: str, brand: BrandPack) -> list[str]:
    """Deterministic quality report fed to the polish pass (not a gate)."""
    s = FORMAT_SPECS[fmt]
    issues: list[str] = []
    n = len(script.beats)
    if not s["beats"][0] <= n <= s["beats"][1]:
        issues.append(f"{n} beats; need {s['beats'][0]}-{s['beats'][1]}.")
    words = spoken_words(script)
    secs = estimated_seconds(script, brand.voice.speed)
    if not s["words"][0] <= words <= s["words"][1]:
        issues.append(f"{words} spoken words (~{secs:.0f} s); need {s['words'][0]}-"
                      f"{s['words'][1]} words ({s['seconds'][0]}-{s['seconds'][1]} s).")
    hook_words = len(script.hook.split())
    if not s["hook_words"][0] <= hook_words <= s["hook_words"][1]:
        issues.append(f"hook has {hook_words} words; need {s['hook_words'][0]}-"
                      f"{s['hook_words'][1]}.")
    if _BAD_OPENERS.search(script.hook):
        issues.append("hook opens with setup or a stock opener; start on the claim itself.")
    if script.beats and _same(script.beats[0].text, script.hook):
        issues.append("beat 1 repeats the hook; beat 1 must move the story forward.")
    if any(_same(script.hook, ex) for ex in _quoted(brand.hook_styles)):
        issues.append("hook copies a brand example; write a new hook for this topic.")
    if not script.cta.strip():
        issues.append("cta (loop line) is empty.")
    elif _OUTRO.search(script.cta):
        issues.append("cta is an outro or subscribe ask; make it a loop line into the hook.")
    if len(script.title) > 70:
        issues.append(f"title is {len(script.title)} characters; keep it under 70.")
    lo, hi = s["beat_words"]
    seen_prompts: set[str] = set()
    shots = []
    for i, b in enumerate(script.beats, 1):
        bw = len(b.text.split())
        if bw < lo or bw > hi + 4:
            issues.append(f"beat {i} has {bw} words; aim for {lo}-{hi}.")
        vp = len(b.visual_prompt.split())
        if vp < 18:
            issues.append(f"beat {i} visual_prompt is vague ({vp} words); add subject, setting, "
                          "camera angle and lens, lighting.")
        if _ON_IMAGE_TEXT.search(b.visual_prompt):
            m = _ON_IMAGE_TEXT.search(b.visual_prompt)
            issues.append(f"beat {i} visual_prompt mentions '{m.group(0)}'; describe objects "
                          "only (no text, labels, logos or brands).")
        if re.search(r"\d", _ALLOWED_DIGITS.sub("", b.visual_prompt)):
            issues.append(f"beat {i} visual_prompt contains numbers; image models render them as "
                          "garbled text, so describe the scene without numbers or units.")
        key = b.visual_prompt.strip().lower()
        if key in seen_prompts:
            issues.append(f"beat {i} repeats an earlier visual_prompt.")
        seen_prompts.add(key)
        shot = canonical_shot(b.shot)
        shots.append(shot)
        if shot not in SHOTS:
            issues.append(f"beat {i} shot '{b.shot}' is not one of: {', '.join(SHOTS)}.")
        if len(b.overlay.split()) > 5:
            issues.append(f"beat {i} overlay is {len(b.overlay.split())} words; max 5.")
        if b.overlay and b.overlay.strip().lower() == b.text.strip().lower():
            issues.append(f"beat {i} overlay copies the narration.")
        q = len(b.stock_query.split())
        if shot not in NO_STOCK_SHOTS and b.stock_query and not 2 <= q <= 4:
            issues.append(f"beat {i} stock_query has {q} words; use 2-4.")
    for i in range(1, len(shots)):
        if shots[i] == shots[i - 1]:
            issues.append(f"beats {i} and {i + 1} use the same shot '{shots[i]}'.")
    if fmt == "short" and len(set(shots)) < 4:
        issues.append(f"only {len(set(shots))} distinct shot types; use at least 4.")
    issues += [f"policy: {v}" for v in lint_all(script, brand)]
    return issues


def lint_all(script: Script, brand: BrandPack) -> list[Violation]:
    """Policy lint over the script, plus overlays and stock queries (on-screen / search text)."""
    violations = lint_script(script, brand)
    extra = [b for b in script.beats if b.overlay or b.stock_query]
    if extra:
        shadow = Script(title="", hook="", beats=[
            Beat(text=b.overlay, visual_prompt=b.stock_query) for b in script.beats
        ])
        for v in lint_script(shadow, brand):
            if v.field.startswith("beats["):
                fld = v.field.replace(".text", ".overlay").replace(".visual_prompt", ".stock_query")
                violations.append(Violation(v.rule, fld, v.match))
    return violations


# ---------------------------------------------------------------- LLM passes


def _ask(job: VideoJob, ctx: StageContext, role: str, label: str,
         make: Callable[[str | None], list[dict[str, str]]]) -> Script:
    schema = Script.model_json_schema()
    error: str | None = None
    for attempt in range(2):  # one retry with the validation error fed back
        try:
            raw = ctx.chat.complete(make(error), role=role, json_schema=schema,
                                    max_tokens=FORMAT_SPECS[job.format]["max_tokens"])
            return parse_script(raw)
        except (ValidationError, ValueError) as exc:
            error = str(exc)[:1500]
            log(job, f"script: {label} attempt {attempt + 1} invalid: {error.splitlines()[0]}")
    raise StageError(f"script: {label} reply failed validation twice: {error}")


def draft_script(job: VideoJob, brand: BrandPack, ctx: StageContext,
                 angle: AngleBrief | None = None) -> Script:
    return _ask(job, ctx, "draft", "draft", lambda err: build_messages(job, brand, err, angle))


def polish_script(job: VideoJob, brand: BrandPack, ctx: StageContext, draft: Script,
                  fixes: list[str] | None = None) -> Script:
    """Editor pass. Also used by the fact-check stage (with `fixes`) for its rewrite."""
    qa = qa_issues(draft, job.format, brand)
    script = _ask(job, ctx, "polish", "polish",
                  lambda err: build_polish_messages(job, brand, draft, qa, fixes, err))
    keep = {f: getattr(draft, f) for f in ("description", "hashtags", "title")
            if getattr(draft, f) and not getattr(script, f)}
    return normalize(script.model_copy(update=keep) if keep else script, brand)


def _is_fixture(ctx: StageContext) -> bool:
    """A reduced offline mock (MockChatClient(beats=N)) used by fast render tests."""
    from studio.pipeline.mock_llm import MockChatClient

    return isinstance(ctx.chat, MockChatClient) and ctx.chat.beats is not None


def _log_report(job: VideoJob, label: str, report: script_quality.QualityReport) -> None:
    log(job, f"script: quality gate ({label}): {report.summary()}")
    for issue in report.issues:
        log(job, f"script: quality {issue}")


def quality_gate(job: VideoJob, brand: BrandPack, ctx: StageContext, script: Script) -> Script:
    """Deterministic quality gate; on errors one repair pass via role="polish", then re-gate."""
    workdir = Path(job.workdir)
    fixture = _is_fixture(ctx)

    def run_gate() -> script_quality.QualityReport:
        return script_quality.gate(script, job.format, brand.voice.speed, fixture=fixture)

    report = run_gate()
    _log_report(job, "polish", report)
    rounds = [{"round": "polish", **report.to_dict()}]
    if report.errors:
        log(job, f"script: quality gate found {len(report.errors)} errors; one repair pass")
        fixes = [f"QUALITY ERROR (must fix): {i.check}: {i.message}" for i in report.errors]
        fixes += [f"quality warning (fix if you can): {i.check}: {i.message}"
                  for i in report.warnings]
        fixes += [f"POLICY VIOLATION, remove or reword: {v}" for v in lint_all(script, brand)]
        script = polish_script(job, brand, ctx, script, fixes=fixes)
        report = run_gate()
        _log_report(job, "repair", report)
        rounds.append({"round": "repair", **report.to_dict()})
    (workdir / "script_quality.json").write_text(json.dumps(rounds, indent=2))
    if report.errors:
        (workdir / "script.rejected.json").write_text(script.model_dump_json(indent=2))
        detail = "; ".join(str(i) for i in report.errors)
        raise StageError(f"script quality gate failed ({len(report.errors)}): {detail}")
    return script


def generate_script(job: VideoJob, brand: BrandPack, ctx: StageContext) -> Script:
    angle = pick_angle(job, brand, ctx) if angle_enabled() else None
    if angle is not None:
        (Path(job.workdir) / "angle.json").write_text(angle.model_dump_json(indent=2))
    draft = normalize(draft_script(job, brand, ctx, angle), brand)
    log(job, f"script: draft {len(draft.beats)} beats, {spoken_words(draft)} words")
    script = polish_script(job, brand, ctx, draft)
    violations = lint_all(script, brand)
    if violations:  # one targeted repair; lint_all stays the hard gate in run()
        log(job, f"script: polish broke {len(violations)} policy rules; one repair pass")
        fixes = [f"POLICY VIOLATION, remove or reword: {v}" for v in violations]
        script = polish_script(job, brand, ctx, script, fixes=fixes)
    left = [q for q in qa_issues(script, job.format, brand) if not q.startswith("policy:")]
    if left:
        log(job, f"script: polish left {len(left)} QA notes: " + " | ".join(left[:6]))
    return quality_gate(job, brand, ctx, script)  # the policy lint (assert_clean) runs after


def assert_clean(script: Script, brand: BrandPack, workdir: Path) -> None:
    violations = lint_all(script, brand)
    if violations:
        (workdir / "script.rejected.json").write_text(script.model_dump_json(indent=2))
        detail = "; ".join(str(v) for v in violations)
        raise StageError(f"policy lint failed ({len(violations)}): {detail}")


def run(job: VideoJob, brand: BrandPack, ctx: StageContext) -> VideoJob:
    workdir = Path(job.workdir)
    out = workdir / "script.json"
    key = hash_inputs(STAGE, VERSION, job.topic, job.format, brand, angle_enabled())
    if is_cached(job, STAGE, key, [out], ctx):
        if job.script is None:
            job.script = Script.model_validate_json(out.read_text())
        assert_clean(job.script, brand, workdir)
        log(job, "script: cached, skipped")
        return job

    script = generate_script(job, brand, ctx)
    assert_clean(script, brand, workdir)

    out.write_text(script.model_dump_json(indent=2))
    job.script = script
    write_cache(job, STAGE, key, [out])
    log(job, f"script: {len(script.beats)} beats, {spoken_words(script)} words, "
        f"~{estimated_seconds(script, brand.voice.speed):.0f} s")
    return job
