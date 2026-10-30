"""Critical-only deterministic block lists for topic triage (doc 37 §3). Word-boundary regexes.

Only categories that are unsafe to auto-generate regardless of angle: politics/elections,
medical, crypto and finance advice, death and violence, minors, gambling and adult, and hoaxes.
Sports, celebrities, brands and breaking-news wording are NOT word-filtered here: the LLM triage
(news_event / person_or_brand classes), the capitalised-name heuristic, each brand's red_lines
and the unchanged fact_check/policy gates handle them. Keep the lists short and specific.
Each entry is (reason, klass, regex).
"""

from __future__ import annotations

import re

# klass values: ymyl_or_sensitive | unsafe
_RAW: tuple[tuple[str, str, str], ...] = (
    ("politics", "ymyl_or_sensitive",
     (r"elections?|ballots?|senator|congress(?:man|woman)?|candidates?|republicans?|"
      r"democrat(?:ic|s)?|gop|impeach\w*|white house|trump|biden|harris")),
    ("health_medical", "ymyl_or_sensitive",
     (r"cancer|diabetes|vaccines?|covid|outbreak|pandemic|symptoms?|diagnos\w*|"
      r"medication|overdose")),
    ("finance_advice", "ymyl_or_sensitive",
     (r"crypto\w*|bitcoin|ethereum|nfts?|forex|penny stocks?|day trading|meme coins?|"
      r"options trading|legal advice")),
    ("death_crime_disaster", "ymyl_or_sensitive",
     (r"dies|died|dead|death|deaths|killed|killing|murder\w*|shooting|shooter|stabb\w*|"
      r"terror\w*|hostages?|victims?|funeral|obituary|tragedy|earthquake|hurricane|tornado|"
      r"wildfire|explosion|invasion|missile|bomb\w*")),
    ("minors_kids", "unsafe",
     r"kids?|child|children|minors?|toddlers?"),
    ("gambling_adult", "unsafe",
     r"gambl\w*|casino|betting|sportsbook|onlyfans|porn\w*|nsfw|nude|escort"),
    ("hoax_conspiracy", "unsafe",
     r"hoax|conspirac\w*|qanon|flat earth|chemtrails?|miracle cure"),
)

BLOCKLISTS: tuple[tuple[str, str, re.Pattern[str]], ...] = tuple(
    (reason, klass, re.compile(rf"\b(?:{body})\b", re.IGNORECASE)) for reason, klass, body in _RAW
)

# Terms that look like names to the capitalisation heuristic but are ordinary topics.
NOT_A_NAME = frozenset({
    "black friday", "cyber monday", "new year", "new york", "united states", "daylight saving",
    "daylight saving time", "labor day", "memorial day", "independence day", "solar eclipse",
    "milky way", "north pole", "south pole", "great wall", "golden gate", "golden gate bridge",
    "panama canal", "hoover dam", "suez canal", "silicon valley",
})

# Words that make an LLM angle mechanism-only (required for accuracy_critical channels).
MECHANISM_WORDS = re.compile(
    r"\b(?:how|why|what (?:makes|happens|keeps|lets|causes)|the (?:mechanism|physics|logic|"
    r"engineering) (?:of|behind))\b", re.IGNORECASE)

INJECTION = re.compile(
    r"ignore (?:all |any )?(?:previous|prior|above)|disregard (?:the |all )?(?:previous|above|"
    r"instructions)|system prompt|you are now|new instructions|act as|jailbreak|"
    r"<\s*/?\s*(?:system|assistant|instructions?)\s*>|\bapprove (?:everything|this|all)\b",
    re.IGNORECASE)
