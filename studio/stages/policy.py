"""Deterministic policy lint for scripts (docs 04 §5.1 item 5, 11 §4, 13 §3 and §5, 17 §Policy screen).

The lint is regex-only, so it is reproducible and cheap. It is a backstop, not a
replacement for the human review gate (04 §5.4).

Rules come from three places:
1. Always-on rules: credential words/titles (13 §5, 11 §4) and income guarantees (FTC).
2. Brand red lines: each rule lists trigger words; if any of the brand's `red_lines`
   contains one, the rule is switched on for that brand.
3. Brand `banned_templates`: literal phrases, matched case-insensitively.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from studio.contracts import BrandPack, Presenter, Script

_CRED = (
    r"pilot|captain|electrician|contractor|plumber|attorney|lawyer|cpa|cfp|financial advis[eo]r"
    r"|advis[eo]r|doctor|nurse|expert|guru|hacker|agent|realtor|broker|lender|engineer"
)


@dataclass(frozen=True)
class Rule:
    id: str
    patterns: tuple[str, ...]
    triggers: tuple[str, ...] = ()  # empty = always on
    flags: int = re.IGNORECASE
    avatar_only: bool = False


RULES: tuple[Rule, ...] = (
    Rule(
        "credential_claim",
        (
            r"\bDr\.?\s+[A-Z][a-z]+",
            r"\bDr\.(?=\s|$)",
            r"[A-Z][a-z]+,\s*(?:MD|CFP|CPA|PhD)\b",
            (rf"\b(?:I'?m|I am|we'?re|we are|as (?:a|an|your))\s+(?:a |an |your )?"
            rf"(?:licensed |certified |registered |professional |former |real )?(?:{_CRED})s?\b"),
            (r"\byour (?:trusted |friendly |go-to )?(?:financial |tech |seo |security |home |car )?"
            r"(?:advis[eo]r|expert|guru)\b"),
        ),
    ),
    Rule(
        "income_guarantee",
        (
            (r"\bguarantee[sd]?\b[^.!?]{0,40}\b(?:income|returns?|profits?|results?|rankings?|"
            r"traffic|money|earnings|#1|first page|page one)\b"),
            r"\b(?:income|returns?|profits?|results?|rankings?|earnings)\s+(?:are |is )?guaranteed\b",
            r"\bmake money online\b",
            r"\bpassive income\b",
            r"\bget rich\b",
            r"\bquit your (?:day )?job\b",
            r"\b(?:earn|make)\s+\$?[\d,]+k?\+?\s+(?:a|per|every)\s+(?:day|week|month)\b",
            r"\brisk[- ]free\b",
            r"\bfinancial freedom\b",
        ),
    ),
    Rule(
        "ai_presenter_claims_human",
        (r"\b(?:I'?m|I am)\s+(?:a\s+)?(?:real|human)\b", r"\bnot an? (?:AI|robot)\b"),
    ),  # every channel: narration is an AI voice, so it must never claim to be human
    Rule(
        "personal_advice",
        (
            r"\byou should (?:buy|sell|invest|refinance|take out|lease|finance)\b",
            r"\bwe recommend (?:buying|selling|investing|refinancing)\b",
            r"\b(?:my|our) (?:financial|legal|investment) advice\b",
            r"\bbuy (?:the|this) stock\b",
        ),
        triggers=("advice",),
    ),
    Rule(
        "offensive_security",
        (
            r"\bhow to (?:hack|break into|gain (?:unauthori[sz]ed )?access)\b",
            r"\bphishing kits?\b",
            r"\bsteal (?:their|someone'?s|a|the) (?:password|login|credentials|account)\b",
            r"\bbypass(?:ing)? (?:the |a )?paywall\b",
            r"\bstalkerware\b",
            r"\bunblock (?:netflix|streaming|region|content)\b",
            r"\bgeo-?unblock\w*\b",
            r"\bexploit (?:code|walkthrough)\b",
        ),
        triggers=("unauthorized access", "phishing kit", "stalkerware", "geo-unblocking"),
    ),
    Rule(
        "crash_content",
        (
            r"\b(?:plane|air|jet|airliner|aircraft)\s?crash(?:es|ed)?\b",
            r"\bcrash (?:recreation|footage|site)\b",
            r"\bhijack\w*\b",
            r"\bvictims?\b",
            r"\bconspirac(?:y|ies)\b",
        ),
        triggers=("crash",),
    ),
    Rule(
        "diy_hazard",
        (
            r"\banyone can (?:do|fix|install|wire|replace)\b",
            r"\bdo it yourself\b",
            r"\bDIY\b",
            r"\b(?:remove|scrape|sand|cut)\s+(?:the |your )?(?:asbestos|lead paint)\b",
            r"\b(?:open|work on|touch)\s+(?:a |the |your )?live (?:panel|circuit|wire|gas line)\b",
            r"\bstep (?:one|1)[:,]? (?:turn off|unscrew|disconnect)\b",
        ),
        triggers=("anyone can do it", "asbestos", "diy"),
    ),
    Rule(
        "financing_out_of_lane",
        (r"\bHELOCs?\b", r"\bhome equity (?:loan|line)s?\b", r"\bresale (?:ROI|value boost)\b"),
        triggers=("heloc",),
    ),
    Rule(
        "disaster_framing",
        (
            r"\b[\d,]+\s+(?:people\s+|workers\s+)?(?:died|dead|killed|deaths)\b",
            r"\b(?:hundreds|thousands|dozens) (?:of (?:people|workers) )?(?:died|were killed)\b",
            r"\bdeath toll\b",
        ),
        triggers=("death count", "disaster porn"),
    ),
    Rule(
        "black_hat_marketing",
        (
            r"\b(?:buy|bought|buying|purchase)\s+(?:followers|reviews|likes|backlinks)\b",
            r"\bfake reviews?\b",
            r"\bbot (?:traffic|followers|accounts)\b",
            r"\bprivate blog network\b",
        ),
        triggers=("fake-review", "black-hat"),
    ),
    Rule(
        "fair_housing_steering",
        (
            r"\b(?:safe|good|bad|rough|sketchy)\s+neighbou?rhoods?\b",
            r"\bneighbou?rhoods?\s+(?:for|without)\s+(?:families|people like|minorities)\b",
        ),
        triggers=("fair-housing", "steering"),
    ),
)

_MONEY = re.compile(r"\$\s?\d")
_YEAR = re.compile(r"\b(?:19|20)\d\d\b")


@dataclass(frozen=True)
class Violation:
    rule: str
    field: str
    match: str

    def __str__(self) -> str:
        return f"[{self.rule}] {self.field}: {self.match!r}"


def active_rules(brand: BrandPack) -> list[Rule]:
    red = " ".join(brand.red_lines).lower()
    out = []
    for rule in RULES:
        if rule.avatar_only and brand.presenter != Presenter.AVATAR:
            continue
        if not rule.triggers or any(t in red for t in rule.triggers):
            out.append(rule)
    return out


def script_fields(script: Script) -> list[tuple[str, str]]:
    fields = [("title", script.title), ("hook", script.hook)]
    for i, beat in enumerate(script.beats):
        fields.append((f"beats[{i}].text", beat.text))
        fields.append((f"beats[{i}].visual_prompt", beat.visual_prompt))
    fields += [("cta", script.cta), ("description", script.description)]
    fields += [(f"hashtags[{i}]", t) for i, t in enumerate(script.hashtags)]
    return [(k, v) for k, v in fields if v]


def lint_script(script: Script, brand: BrandPack) -> list[Violation]:
    violations: list[Violation] = []
    fields = script_fields(script)
    for rule in active_rules(brand):
        for pattern in rule.patterns:
            rx = re.compile(pattern, rule.flags)
            for name, text in fields:
                m = rx.search(text)
                if m:
                    violations.append(Violation(rule.id, name, m.group(0)))
    for phrase in brand.banned_templates:
        needle = phrase.lower()
        for name, text in fields:
            if needle in text.lower():
                violations.append(Violation("banned_template", name, phrase))
    red = " ".join(brand.red_lines).lower()
    if "dated and sourced" in red:
        spoken = script.narration()
        if _MONEY.search(spoken) and not _YEAR.search(spoken + " " + script.description):
            violations.append(Violation("undated_cost", "narration", "cost figure without a year"))
        if _MONEY.search(spoken) and not script.sources:
            violations.append(Violation("unsourced_cost", "sources", "cost figure without sources"))
    # de-duplicate (several patterns can hit the same text)
    seen, unique = set(), []
    for v in violations:
        if (v.rule, v.field) not in seen:
            seen.add((v.rule, v.field))
            unique.append(v)
    return unique
