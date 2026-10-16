"""Offline ChatClient used by --mock: returns a deterministic, schema-valid, lint-clean Script
(role="draft") or a passing FactCheck (role="fact_check"). No network."""

from __future__ import annotations

import json
import re
from typing import Any

_BEATS = [
    ("Start with the one number that changes everything here.",
     "wide establishing shot that introduces {topic}"),
    ("Now zoom in on the first domino, because it falls faster than you think.",
     "close-up of the first cause behind {topic}"),
    ("Here is the part most explanations skip, and it flips the whole picture.",
     "split-screen comparison, before and after, for {topic}"),
    ("Watch what happens one step later, when the effect starts to compound.",
     "diagram of a chain reaction spreading outward from {topic}"),
    ("The real surprise is the scale, so let's put it next to something familiar.",
     "scale comparison next to a familiar everyday object for {topic}"),
    ("And the last piece is the one nobody expects, which is why it matters most.",
     "dramatic final reveal that resolves {topic}"),
]


class MockChatClient:
    """Implements the `ChatClient` protocol from studio.contracts."""

    def __init__(self, beats: int | None = None) -> None:
        self.beats = beats  # None: 6 for Shorts, 12 for long-form
        self.calls: list[str] = []

    def complete(
        self,
        messages: list[dict[str, str]],
        *,
        role: str,
        json_schema: dict[str, Any] | None = None,
        max_tokens: int = 2048,
    ) -> str:
        self.calls.append(role)
        if role == "fact_check":
            return json.dumps({"passed": True, "issues": [], "model": "mock"})
        user = next((m["content"] for m in messages if m["role"] == "user"), "")
        topic = user.removeprefix("Topic:").strip() or "this topic"
        system = messages[0]["content"] if messages else ""
        long = "Format: long" in system
        return json.dumps(self.script(topic, long))

    def script(self, topic: str, long: bool = False) -> dict[str, Any]:
        n = self.beats or (12 if long else 6)
        subject = topic.rstrip("?.!")
        beats = []
        for i in range(n):
            text, visual = _BEATS[i % len(_BEATS)]
            beats.append({"text": text, "visual_prompt": visual.format(topic=subject.lower())})
        hook = topic if topic.endswith("?") else f"Nobody explains {subject.lower()}."
        tags = [t for t in re.findall(r"[A-Za-z]+", subject.title()) if len(t) > 3][:4]
        return {
            "title": subject[:90],
            "hook": hook,
            "beats": beats,
            "cta": "Which brings us right back to the start:",
            "sources": ["Mock source (offline run, replace with real citations)"],
            "description": f"{subject}: a mock offline script for pipeline testing.",
            "hashtags": ["#" + t for t in tags] or ["#explainer"],
        }
