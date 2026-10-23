"""Deterministic script quality gate, run after the polish pass and before the policy lint.

Ideas adapted from the MIT-licensed chaseraw/youtube-script-skill (references/quality-gates.md
and retention.md): the hook confirms the click with a concrete payoff within the first words,
the density gate cuts repeated lines and throat-clearing, the voice gate rejects AI-writing tells,
the retention gate opens and closes the central loop, and the production gate checks that
runtime and word count are compatible. The thresholds and the cliché list are our own.

The gate is regex/arithmetic only, so it is reproducible and free. It never replaces
`policy.py` (the hard compliance gate) or the human review. Issues carry a severity:
ERROR issues trigger one automatic repair pass in script.py and fail the job if they remain;
WARNING issues are logged in the job log.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Literal

from studio.contracts import Script

Severity = Literal["error", "warning"]
ERROR: Severity = "error"
WARNING: Severity = "warning"

HOOK_FRONT_WORDS = 5  # ~2 s at 150 wpm: the claim, number or question must land here
# Average narration seconds per beat (one picture each): (warn_lo, warn_hi, err_lo, err_hi).
BEAT_SECONDS = {"short": (2.5, 4.0, 2.0, 6.0), "long": (5.0, 12.0, 3.5, 16.0)}
DURATION_TOLERANCE = 0.10  # within 10% outside the target runtime is a warning, beyond an error
# Checks that a reduced offline fixture (MockChatClient(beats=N)) may fail by design.
FIXTURE_RELAXED = {"beat_count", "duration", "pacing"}

_STOP = frozenset("""
a an the and or but so of to in on at by for from with as is are was were be been being it its
this that these those there here then than into onto over under about after before through
while when what which who whom how why where can could does do did not no nor if it's that's
you your we our they their them he she his her i me my just very really also too all any each
every some more most much many such only own same other one up down out off again once will would
should may might must has have had having let's
""".split())  # noqa: SIM905 - readable word list
_QUESTION = re.compile(
    r"^(?:why|how|what|which|when|where|who|whose|can|could|does|do|did|is|are|was|were|would|"
    r"will|should)$", re.IGNORECASE)
_NUMBER = re.compile(
    r"\d|^(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|hundred|thousand|million|"
    r"billion|half|twice|double|triple)$", re.IGNORECASE)
_VAGUE = re.compile(
    r"^(?:thing|things|stuff|something|everything|anything|amazing|incredible|crazy|insane|wild|"
    r"secret|secrets|okay|ok|well|guys|everyone|folks|story)$", re.IGNORECASE)
_STOCK_OPENER = re.compile(
    r"^\s*(?:in (?:this|today'?s) video|today\b|welcome|hey\b|hi\b|hello\b|did you know|"
    r"have you ever (?:wondered|thought)|ever wondered|let'?s|so,|okay|imagine|picture this|"
    r"what'?s up|here'?s (?:the thing|why)|you won'?t believe)", re.IGNORECASE)
_OUTRO = re.compile(
    r"\b(?:subscribe|follow for|thanks for (?:watching|listening)|see you|like this video|"
    r"smash that|hit the bell|comment below)\b", re.IGNORECASE)
_PAYOFF = re.compile(
    r"\b(?:because|which (?:is|explains) why|that'?s (?:why|how)|explains?|the (?:answer|reason)|"
    r"which means|so the|that is (?:why|how)|this is (?:why|how)|turns out|in the end)\b",
    re.IGNORECASE)
_LOOP_TAIL = re.compile(
    r"(?:[:…—-]|\.\.\.|\b(?:why|because|how|that|so|and|but|when|until|is|the|this|"
    r"what|where|which))\s*$", re.IGNORECASE)
_CAMERA = re.compile(
    r"\b(?:\d+\s?mm|lens|angle|view|shot|close-?up|close up|wide|macro|aerial|overhead|top-down|"
    r"isometric|elevation|cutaway|cross-section|diagram|framing|framed|frame|composition|"
    r"eye[- ]level|low-angle|high-angle|bird'?s-eye|worm'?s-eye|three-quarter|profile|"
    r"silhouette|depth of field|bokeh|centered|foreground|background|orthographic|"
    r"perspective|portrait|panorama|tracking|dolly|telephoto)\b", re.IGNORECASE)
# A visual_prompt that asks the image model for written content renders garbled glyphs.
_TEXT_REQUEST = re.compile(
    r"\b(?:text(?!-free)|captions?|logos?|words|lettering|letters|typography|watermarks?|"
    r"labell?ed|labels?|signage|subtitles?|headlines?|written|inscriptions?|title card)\b",
    re.IGNORECASE)
_TEXT_NEGATED = re.compile(
    r"\b(?:no|without|free of|devoid of|absent|omit(?:ting)?|avoid(?:ing)?)\s+(?:any\s+|visible\s+|"
    r"on-image\s+|overlaid\s+)?(?:\w+\s+)?(?:text|captions?|logos?|words|lettering|letters|"
    r"typography|watermarks?|labels?|signage|subtitles?|headlines?|inscriptions?)"
    r"(?:\s*(?:,|/|or|and|nor)\s*(?:no\s+)?(?:text|captions?|logos?|words|lettering|letters|"
    r"typography|watermarks?|labels?|signage|people|subtitles?))*", re.IGNORECASE)

# Generic AI phrasing and creator clichés (our own list). Spoken / on-screen fields: ERROR.
CLICHES: tuple[tuple[str, str], ...] = (
    ("in this video", r"\bin (?:this|today'?s) (?:video|short|episode)\b"),
    ("let's dive in", r"\blet'?s (?:dive|jump|get) (?:in|into|right in|started)\b"),
    ("dive into", r"\b(?:dive|diving) (?:deep )?into\b"),
    ("deep dive", r"\bdeep[- ]dive\b"),
    ("buckle up", r"\bbuckle up\b"),
    ("game-changer", r"\bgame[- ]chang(?:er|ers|ing)\b"),
    ("unlock the secret/potential", (
        r"\bunlock(?:s|ing|ed)?\s+(?:the\s+|your\s+|its\s+|their\s+|a\s+)?"
        r"(?:full\s+|true\s+|hidden\s+|real\s+)?(?:secrets?|potential|power|myster(?:y|ies)|"
        r"magic|key)\b")),
    ("delve", r"\bdelv(?:e|es|ed|ing)\b"),
    ("tapestry", r"\btapestry\b"),
    ("testament to", r"\b(?:a|is a) testament to\b"),
    ("in today's world", r"\bin today'?s (?:world|fast-paced|digital)\b"),
    ("fast-paced world", r"\bfast[- ]paced world\b"),
    ("ever-evolving", r"\bever[- ](?:evolving|changing) (?:world|landscape)\b"),
    ("it's important to note", r"\bit'?s (?:important|worth) (?:to note|noting)\b"),
    ("at the end of the day", r"\bat the end of the day\b"),
    ("secret sauce", r"\bsecret sauce\b"),
    ("mind-blowing", r"\bmind[- ]?blowing\b|\bblow your mind\b"),
    ("you won't believe", r"\byou won'?t believe\b"),
    ("stay tuned", r"\bstay tuned\b"),
    ("without further ado", r"\bwithout further ado\b"),
    ("let that sink in", r"\blet that sink in\b"),
    ("here's the thing", r"\bhere'?s the (?:thing|kicker|catch|twist)\b"),
    ("the rest is history", r"\bthe rest is history\b"),
    ("revolutionize", r"\brevolutioni[sz](?:e|es|ed|ing)\b"),
    ("harness the power", r"\bharness(?:es|ing)? the (?:power|potential)\b"),
    ("navigate the complexities", r"\bnavigat(?:e|ing) the (?:complexities|world|landscape)\b"),
    ("embark on a journey", r"\bembark(?:s|ing)? on\b"),
    ("seamless", r"\bseamless(?:ly)?\b"),
    ("cutting-edge", r"\bcutting[- ]edge\b"),
    ("elevate your", r"\belevat(?:e|es|ing) your\b"),
    ("in the realm of", r"\b(?:in|into) the realm of\b"),
    ("marvel of engineering", r"\bmarvel of (?:modern )?engineering\b"),
    ("plays a crucial role", r"\bplays? a (?:crucial|vital|pivotal|key) role\b"),
    ("the power of", r"\bthe (?:true |real )?power of\b"),
    ("smash that like", r"\bsmash (?:that|the) like\b"),
)
_CLICHE_RES = tuple((name, re.compile(rx, re.IGNORECASE)) for name, rx in CLICHES)


@dataclass(frozen=True)
class QualityIssue:
    severity: Severity
    check: str
    message: str

    def __str__(self) -> str:
        return f"{self.severity.upper()} [{self.check}] {self.message}"


@dataclass
class QualityReport:
    issues: list[QualityIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[QualityIssue]:
        return [i for i in self.issues if i.severity == ERROR]

    @property
    def warnings(self) -> list[QualityIssue]:
        return [i for i in self.issues if i.severity == WARNING]

    @property
    def passed(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        return {"passed": self.passed, "errors": len(self.errors),
                "warnings": len(self.warnings), "issues": [asdict(i) for i in self.issues]}

    def summary(self) -> str:
        return f"{len(self.errors)} errors, {len(self.warnings)} warnings"


# ---------------------------------------------------------------- helpers


def _words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9][A-Za-z0-9'-]*", text)


def _norm(text: str) -> list[str]:
    return [w.lower().strip("'") for w in _words(text)]


def _stem(word: str) -> str:
    w = word.lower()
    for suf in ("ing", "ed", "es", "s"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def _content(words: list[str]) -> list[str]:
    return [w for w in words if w.lower() not in _STOP and len(w) > 2]


def _specs(fmt: str) -> dict:
    from studio.stages.script import FORMAT_SPECS  # lazy: script.py imports this module

    return FORMAT_SPECS[fmt]


def _wpm() -> int:
    from studio.stages.script import WPM

    return WPM


# ---------------------------------------------------------------- checks


def check_hook(script: Script, fmt: str) -> list[QualityIssue]:
    """The claim, number or question must land in the first ~5 words (about 2 s)."""
    hook = script.hook.strip()
    if not hook:
        return [QualityIssue(ERROR, "hook", "hook is empty.")]
    issues: list[QualityIssue] = []
    if _STOCK_OPENER.search(hook):
        issues.append(QualityIssue(ERROR, "hook", "hook opens with setup or a stock opener; "
                                   "start on the claim, the number or the question itself."))
    front = _words(hook)[:HOOK_FRONT_WORDS]
    question = hook.rstrip().endswith("?") and any(_QUESTION.match(w) for w in front)
    number = any(_NUMBER.search(w) for w in front)
    content = [w for w in _content(front) if not _VAGUE.match(w)]
    if not (question or number or len(content) >= 2):
        issues.append(QualityIssue(WARNING, "hook", f"the first {HOOK_FRONT_WORDS} words "
                                   f"('{' '.join(front)}') carry no concrete claim, number or "
                                   "question; front-load the payoff."))
    if any(_VAGUE.match(w) for w in front[:3]):
        issues.append(QualityIssue(WARNING, "hook", "hook leads with a vague word; name the "
                                   "concrete object or effect instead."))
    hi = _specs(fmt)["hook_words"][1]
    n = len(_words(hook))
    if n > hi:
        issues.append(QualityIssue(WARNING, "hook", f"hook is {n} words (~{n / _wpm() * 60:.1f} "
                                   f"s); keep it to {hi} or fewer."))
    return issues


def check_length(script: Script, fmt: str, speed: float = 1.0) -> list[QualityIssue]:
    """Beat count and estimated spoken runtime for the format."""
    s = _specs(fmt)
    issues: list[QualityIssue] = []
    n, (lo, hi) = len(script.beats), s["beats"]
    if not lo <= n <= hi:
        issues.append(QualityIssue(ERROR, "beat_count", f"{n} beats; {fmt} needs {lo}-{hi}."))
    words = len(script.narration().split())
    secs = words / _wpm() * 60 / max(speed, 0.1)
    slo, shi = s["seconds"]
    if not slo <= secs <= shi:
        far = secs < slo * (1 - DURATION_TOLERANCE) or secs > shi * (1 + DURATION_TOLERANCE)
        issues.append(QualityIssue(ERROR if far else WARNING, "duration",
                                   f"~{secs:.0f} s spoken ({words} words at {_wpm()} wpm); "
                                   f"{fmt} target is {slo}-{shi} s."))
    return issues


def check_pacing(script: Script, fmt: str, speed: float = 1.0) -> list[QualityIssue]:
    """A new picture every 2.5-4 s (Shorts): average narration seconds per beat."""
    if not script.beats:
        return []
    wlo, whi, elo, ehi = BEAT_SECONDS[fmt]
    per_sec = 60 / _wpm() / max(speed, 0.1)
    avg = sum(len(b.text.split()) for b in script.beats) / len(script.beats) * per_sec
    issues: list[QualityIssue] = []
    if not wlo <= avg <= whi:
        sev = ERROR if not elo <= avg <= ehi else WARNING
        issues.append(QualityIssue(sev, "pacing", f"beats average {avg:.1f} s of narration; aim "
                                   f"for a visual change every {wlo:g}-{whi:g} s (split or merge "
                                   "beats)."))
    for i, b in enumerate(script.beats, 1):
        secs = len(b.text.split()) * per_sec
        if secs > ehi:
            issues.append(QualityIssue(WARNING, "pacing", f"beat {i} holds one picture for "
                                       f"~{secs:.0f} s; split it."))
    return issues


def check_loop(script: Script, fmt: str) -> list[QualityIssue]:
    """Shorts need a payoff that answers the hook and a cta that loops back into it."""
    cta = script.cta.strip()
    issues: list[QualityIssue] = []
    if cta and _OUTRO.search(cta):
        issues.append(QualityIssue(ERROR, "loop", "cta is an outro or subscribe ask; make it a "
                                   "loop line that flows back into the hook."))
    if fmt != "short":
        if not cta:
            issues.append(QualityIssue(WARNING, "loop", "no closing line calls back to the hook."))
        return issues
    if not cta:
        issues.append(QualityIssue(ERROR, "loop", "cta (the loop-back line) is empty."))
    else:
        hook_stems = {_stem(w) for w in _content(_norm(script.hook))}
        shares = bool(hook_stems & {_stem(w) for w in _content(_norm(cta))})
        if not (_LOOP_TAIL.search(cta) or shares):
            issues.append(QualityIssue(WARNING, "loop", "cta ends on a closed sentence; end "
                                       "mid-thought so the replay flows into the hook."))
        if len(cta.split()) > 12:
            issues.append(QualityIssue(WARNING, "loop", f"cta is {len(cta.split())} words; keep "
                                       "the loop line short."))
    tail = " ".join(b.text for b in script.beats[-3:]) + " " + cta
    hook_stems = {_stem(w) for w in _content(_norm(script.hook))}
    if not (_PAYOFF.search(tail) or hook_stems & {_stem(w) for w in _content(_norm(tail))}):
        issues.append(QualityIssue(WARNING, "payoff", "the last beats never answer the hook; add "
                                   "a payoff beat that resolves its question or claim."))
    return issues


def check_overlays(script: Script) -> list[QualityIssue]:
    issues = []
    for i, b in enumerate(script.beats, 1):
        n = len(b.overlay.split())
        if n > 5:
            issues.append(QualityIssue(ERROR, "overlay", f"beat {i} overlay is {n} words; max 5."))
        elif b.overlay and b.overlay.strip().lower().rstrip(".") == b.text.strip().lower().rstrip("."):
            issues.append(QualityIssue(WARNING, "overlay", f"beat {i} overlay copies the narration."))
    return issues


def check_visual_prompts(script: Script) -> list[QualityIssue]:
    """Each still needs a subject plus a camera/shot, and must never request written content."""
    issues = []
    for i, b in enumerate(script.beats, 1):
        vp = b.visual_prompt.strip()
        n = len(vp.split())
        if n < 10:
            issues.append(QualityIssue(ERROR, "visual", f"beat {i} visual_prompt is {n} words; "
                                       "describe subject, setting, camera and lighting (25+)."))
        elif n < 18:
            issues.append(QualityIssue(WARNING, "visual", f"beat {i} visual_prompt is only {n} "
                                       "words; add setting, camera angle/lens and lighting."))
        if not _CAMERA.search(vp):
            issues.append(QualityIssue(WARNING, "visual", f"beat {i} visual_prompt has no camera "
                                       "or shot (e.g. 'close-up, 85mm lens', 'aerial view')."))
        subject = [w for w in _content(_norm(_CAMERA.sub(" ", vp)))]
        if n >= 10 and len(subject) < 3:
            issues.append(QualityIssue(ERROR, "visual", f"beat {i} visual_prompt names no clear "
                                       "subject; say what object is in frame."))
        m = _TEXT_REQUEST.search(_TEXT_NEGATED.sub(" ", vp))
        if m:
            issues.append(QualityIssue(ERROR, "visual", f"beat {i} visual_prompt asks for "
                                       f"'{m.group(0)}'; image models garble written content, so "
                                       "describe objects only (overlays carry all text)."))
    return issues


def _ngrams(words: list[str], n: int) -> list[tuple[str, ...]]:
    return [tuple(words[i : i + n]) for i in range(len(words) - n + 1)]


def check_repetition(script: Script) -> list[QualityIssue]:
    """No two beats share an opening; low trigram duplication across the narration."""
    issues: list[QualityIssue] = []
    openings: dict[tuple[str, ...], int] = {}
    for i, b in enumerate(script.beats, 1):
        key = tuple(_norm(b.text)[:3])
        if len(key) == 3 and key in openings:
            issues.append(QualityIssue(ERROR, "repetition", f"beats {openings[key]} and {i} open "
                                       f"the same way ('{' '.join(key)}'); vary the openings."))
        openings.setdefault(key, i)
    for name, line in (("hook", script.hook), ("cta", script.cta)):
        for i, b in enumerate(script.beats, 1):
            if _norm(line) and _norm(b.text) == _norm(line):
                issues.append(QualityIssue(ERROR, "repetition", f"beat {i} repeats the {name} "
                                           "word for word; move the story forward instead."))
    firsts = [(_norm(b.text) or [""])[0] for b in script.beats]
    for i in range(1, len(firsts)):
        if firsts[i] and firsts[i] == firsts[i - 1] and firsts[i] not in {"the", "a", "an"}:
            issues.append(QualityIssue(WARNING, "repetition", f"beats {i} and {i + 1} both start "
                                       f"with '{firsts[i]}'."))
    two = Counter(tuple(_norm(b.text)[:2]) for b in script.beats if len(_norm(b.text)) >= 2)
    for pair, count in two.items():
        if count >= 3:
            issues.append(QualityIssue(WARNING, "repetition", f"{count} beats open with "
                                       f"'{' '.join(pair)}'."))
    words = _norm(" ".join([script.hook, *(b.text for b in script.beats), script.cta]))
    grams = [g for g in _ngrams(words, 3) if any(w not in _STOP for w in g)]
    if grams:
        counts = Counter(grams)
        dup = sum(c - 1 for c in counts.values() if c > 1) / len(grams)
        if dup > 0.10:
            issues.append(QualityIssue(ERROR if dup > 0.20 else WARNING, "repetition",
                                       f"{dup:.0%} of the narration's word trigrams repeat; cut "
                                       "repeated lines and summaries."))
    long_dups = [g for g, c in Counter(_ngrams(words, 6)).items() if c > 1]
    if long_dups:
        issues.append(QualityIssue(WARNING, "repetition", "the narration repeats a phrase: '"
                                   + " ".join(long_dups[0]) + "'."))
    return issues


def check_cliches(script: Script) -> list[QualityIssue]:
    fields = [("title", script.title), ("hook", script.hook), ("cta", script.cta)]
    fields += [(f"beat {i} text", b.text) for i, b in enumerate(script.beats, 1)]
    fields += [(f"beat {i} overlay", b.overlay) for i, b in enumerate(script.beats, 1)]
    issues = []
    for where, text in [*fields, ("description", script.description)]:
        for name, rx in _CLICHE_RES:
            if text and rx.search(text):
                sev = WARNING if where == "description" else ERROR
                issues.append(QualityIssue(sev, "cliche", f"{where} uses generic phrasing "
                                           f"'{name}'; say the specific thing instead."))
    return issues


def gate(script: Script, fmt: str, speed: float = 1.0, *, fixture: bool = False) -> QualityReport:
    """Run every check. `fixture=True` (a reduced offline mock) downgrades length/pacing errors."""
    issues = [
        *check_hook(script, fmt), *check_length(script, fmt, speed),
        *check_pacing(script, fmt, speed), *check_loop(script, fmt), *check_overlays(script),
        *check_visual_prompts(script), *check_repetition(script), *check_cliches(script),
    ]
    if fixture:
        issues = [QualityIssue(WARNING, i.check, i.message + " (offline fixture)")
                  if i.check in FIXTURE_RELAXED else i for i in issues]
    return QualityReport(issues)
