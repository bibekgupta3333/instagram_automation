"""Offline ChatClient used by --mock: returns a deterministic, schema-valid, lint-clean Script
(role="draft" or "polish") or a passing claim list (role="fact_check"). No network.

The script is shaped like a real Shorts script: a qualitative hook, 10 beats with escalating
mechanisms, varied `shot` types, short `overlay` keywords, `stock_query` keywords (empty on diagram
and cutaway beats) and a loop line, so offline runs exercise every Beat field."""

from __future__ import annotations

import json
import re
from typing import Any

# (text, shot, visual_prompt, overlay, stock keyword) - {topic} is the lower-cased subject.
_BEATS = [
    ("The structure relies on supports that carry its weight.", "wide",
     ("{topic} shown complete at true scale, low three-quarter angle, 24mm wide lens, subject "
     "filling the lower two thirds, hard late-afternoon sun from the left, long shadows"),
     "Support structure", "overview"),
    ("That part transfers the weight into the surrounding structure.", "macro detail",
     ("extreme close-up of the single load-bearing part inside {topic}, machined steel with fine "
     "tool marks, 100mm macro lens, shallow depth of field, cool rim light from behind"),
     "Load transfer", "steel detail"),
    ("Without support, the load can shift into weaker areas.", "diagram",
     ("clean side-elevation diagram of {topic} with the load path drawn as glowing arrows "
     "flowing down into one point, flat orthographic view, even studio lighting"),
     "Support matters", ""),
    ("So the supports spread that load across the structure.", "cutaway",
     ("cutaway of {topic} revealing twelve evenly spaced load points in a ring, isometric "
     "three-quarter view from above, soft key light, gentle ambient occlusion"),
     "Spread the load", ""),
    ("The joints can flex as the load changes.", "close-up",
     ("close-up of one load point on {topic} under strain, polished metal surface catching a "
     "thin highlight, 85mm lens at eye level, dark background, single soft key light"),
     "Flexible joints", "metal joint"),
    ("Then heat enters the story, and the numbers jump.", "medium",
     ("medium shot of {topic} running at full power, faint heat shimmer rising off the surface, "
     "50mm lens, centered composition, warm glow from below, cool fill from above"),
     "", "heat shimmer"),
    ("As surfaces heat up, the material expands and stresses joints.", "macro detail",
     ("macro detail of a heat-tinted surface on {topic}, blue and straw oxide colors, 100mm "
     "macro lens, raking side light revealing texture, shallow focus"),
     "Heat expansion", "heat metal"),
    ("A coating thinner than paper soaks up that swing.", "cutaway",
     ("layered cutaway of the protective coating on {topic}, three thin layers peeled back like "
     "pages, isometric view, crisp studio lighting, subtle shadow under each layer"),
     "thinner than paper", ""),
    ("These features work together to manage changing loads.", "aerial",
     ("aerial view looking straight down on {topic} in its real setting, early morning light, "
     "long soft shadows, subject small in the frame for scale, 35mm lens"),
     "Working together", "aerial view"),
    ("And the load still returns to those supports, which explains why", "medium",
     ("medium shot returning to the small load-bearing part of {topic}, same angle as the "
     "opening frame, 50mm lens, hard sun from the left, calm and still"),
     "", "machine part"),
]


class MockChatClient:
    """Implements the `ChatClient` protocol from studio.contracts."""

    def __init__(self, beats: int | None = None) -> None:
        self.beats = beats  # None: 10 for Shorts, 40 for long-form
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
        user = next((m["content"] for m in messages if m["role"] == "user"), "")
        if role == "fact_check":
            # Synthetic verdicts only: exercise the claim schema using the actual input.
            data = json.loads(user)
            lines = [data.get(k, "") for k in ("title", "hook", "cta", "description")]
            lines += [b.get(k, "") for b in data.get("beats", []) for k in ("text", "overlay")]
            lines += data.get("hashtags", [])
            return json.dumps({"claims": [
                {"claim": line, "verdict": "correct", "fix": ""} for line in lines if line.strip()
            ]})
        first = user.splitlines()[0] if user else ""
        topic = first.removeprefix("Topic:").strip() or "this topic"
        system = messages[0]["content"] if messages else ""
        long = "Format: long" in system
        return json.dumps(self.script(topic, long))

    def script(self, topic: str, long: bool = False) -> dict[str, Any]:
        n = self.beats or (40 if long else 10)
        subject = topic.rstrip("?.!")
        keywords = [w.lower() for w in re.findall(r"[A-Za-z]+", subject) if len(w) > 3][:2]
        beats = []
        for i in range(n):
            text, shot, visual, overlay, stock = _BEATS[i % len(_BEATS)]
            query = " ".join([*keywords, stock]) if stock else ""
            if query and len(query.split()) < 2:
                query = f"{query} closeup"
            beats.append({
                "text": text, "shot": shot, "visual_prompt": visual.format(topic=subject.lower()),
                "overlay": overlay, "stock_query": " ".join(query.split()[:4]),
            })
        hook = topic if topic.endswith("?") else "Why does a structure need room to move?"
        tags = [t for t in re.findall(r"[A-Za-z]+", subject.title()) if len(t) > 3][:4]
        return {
            "title": subject[:70],
            "hook": hook,
            "beats": beats,
            "cta": "And that is how it all comes back to this:",
            "sources": [],
            "description": f"{subject}: a mock offline script for pipeline testing.",
            "hashtags": ["#" + t for t in tags] or ["#explainer"],
        }
