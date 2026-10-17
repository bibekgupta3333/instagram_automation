"""Generation prompts for Z-Image-Turbo (and FLUX.2 klein), built from a beat + the brand.

Z-Image-Turbo reads prompts with a Qwen LLM text encoder, so it follows a descriptive
natural-language brief far better than a comma tag list. It is guidance-distilled (CFG 0), so
there is NO negative prompt: exclusions must be written into the prompt as plain sentences.
Guides (Tongyi-MAI model card; fal.ai and deAPI prompting guides, read 2026-10-17) recommend:
image type + subject first, then composition/camera, setting, light, style, then constraints;
one camera/lens cue only; ~8-9 steps.

The template (one paragraph, in this order):

    Tall vertical portrait composition.  <shot framing + lens>.  <visual_prompt>.  <brand style>.
    Colour palette: <named brand colours>.  <quality boosters for the look>.
    No text, no letters, no numbers, no labels, no logos, no watermark.

Brand style and palette are identical for every beat of a video (one consistent look); only
the subject and the shot change. Hex codes are stripped from the brand style and the palette is
given as colour *names*: a text-capable encoder tends to print "#1C2B39" into the image. Words
that invite the model to draw text ("labels", "data overlays", "dimension arrows") are replaced
for the same reason; on-screen text is drawn by ffmpeg from ``Beat.overlay``, never generated.
"""

from __future__ import annotations

import re

from studio.contracts import BrandPack

NO_TEXT = "No text, no letters, no numbers, no labels, no logos, no watermark."

# shot class -> composition and lens language (one camera cue each)
SHOT_FRAMING: dict[str, str] = {
    "wide": ("Wide establishing shot on a 24mm lens, deep focus, the subject placed within a "
             "vast environment, strong leading lines and layered foreground, midground and "
             "background"),
    "medium": ("Medium shot on a 50mm lens, the subject filling the centre of the frame, "
               "gently separated from a softly defocused background"),
    "close": ("Close-up detail shot on an 85mm macro lens, shallow depth of field, the key "
              "detail tack-sharp in the centre against a creamy blurred background"),
    "aerial": ("Aerial drone shot from high above at an oblique angle, 35mm lens, vast sense of "
               "scale, clear depth falling away to the horizon"),
    "top": ("Top-down overhead flat-lay shot, camera pointing straight down, objects neatly "
            "arranged with generous space between them"),
    "low": ("Low-angle hero shot on a 24mm lens looking up at the subject, dramatic "
            "perspective, sky or ceiling behind it"),
    "cutaway": ("Technical cutaway cross-section in a three-quarter view, the outer shell sliced "
                "open to reveal the inner parts in clean distinct layers, subject centred with "
                "even space around it"),
    "diagram": ("Clean explanatory diagram composition, front-on view, a few simple bold shapes "
                "with generous empty space, the one key element clearly highlighted"),
}
DEFAULT_FRAMING = ("Cinematic medium-wide shot on a 35mm lens, the subject centred, clear "
                   "separation between foreground and background")

_SHOT_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("diagram", re.compile(r"\b(diagram|infographic|chart|graph|schematic|flow|map|"
                           r"explainer|data viz\w*)\b", re.IGNORECASE)),
    ("cutaway", re.compile(r"\b(cutaway|cut-away|cross[- ]?section|x-ray|exploded|"
                           r"inside view|interior view|section view)\b", re.IGNORECASE)),
    ("aerial", re.compile(r"\b(aerial|drone|bird'?s[- ]eye|overhead wide|satellite|"
                          r"from above)\b", re.IGNORECASE)),
    ("top", re.compile(r"\b(top[- ]down|flat[- ]?lay|overhead)\b", re.IGNORECASE)),
    ("close", re.compile(r"\b(close[- ]?up|closeup|macro|detail|insert|extreme close)\b", re.IGNORECASE)),
    ("low", re.compile(r"\b(low[- ]angle|worm'?s[- ]eye|hero shot)\b", re.IGNORECASE)),
    ("wide", re.compile(r"\b(wide|establishing|long shot|landscape|panorama\w*|vista)\b", re.IGNORECASE)),
    ("medium", re.compile(r"\b(medium|mid[- ]shot|waist|two[- ]shot|over[- ]the[- ]shoulder|"
                          r"pov|tracking)\b", re.IGNORECASE)),
)


def shot_class(shot: str) -> str:
    """Normalise a free-text shot type to one SHOT_FRAMING key, or "" when unknown/empty."""
    for name, rx in _SHOT_RULES:
        if rx.search(shot or ""):
            return name
    return ""


# Looks that are rendered/illustrated, not photographed: they get material/lighting boosters
# instead of camera/film language.
_RENDERED = re.compile(r"\b(3d|render\w*|cutaway|diagram|isometric|blueprint|vector|flat|"
                       r"illustrat\w*|ui|dashboard|chart|map|icons?|cgi)\b", re.IGNORECASE)
BOOST_RENDERED = ("Highly detailed, crisp clean edges, sharp focus throughout, physically based "
                  "materials, soft studio rim lighting, rich contrast, polished professional "
                  "quality")
BOOST_PHOTO = ("Photorealistic, highly detailed, sharp focus, realistic textures and materials, "
               "natural light with soft shadows, professional colour grading, high dynamic range")

# phrases in brand styles that make a text-capable model draw text -> text-free equivalents
_TEXTY: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bcrisp labels?\b", re.IGNORECASE), "crisp shapes"),
    (re.compile(r"\b(data )?overlays?\b", re.IGNORECASE), "graphic accents"),
    (re.compile(r"\btechnical dimension arrows\b", re.IGNORECASE), "technical guide lines"),
    (re.compile(r"\bblack type\b", re.IGNORECASE), "black ink"),
    (re.compile(r"\bline icons\b", re.IGNORECASE), "simple line shapes"),
)
_HEX = re.compile(r"\s*#[0-9a-fA-F]{6}\b")

# small named-colour table for palette hex -> words (nearest in RGB)
_NAMES: dict[str, tuple[int, int, int]] = {
    "black": (17, 17, 17), "charcoal grey": (43, 45, 49), "graphite": (30, 30, 36),
    "deep navy": (14, 30, 60), "midnight blue": (11, 16, 38), "dark slate blue": (28, 43, 57),
    "steel blue": (70, 110, 150), "dusty sky blue": (127, 167, 184), "vivid azure blue": (58, 134, 255),
    "violet": (91, 75, 219), "teal": (46, 196, 182), "deep teal": (22, 48, 43),
    "mint green": (43, 217, 160), "ledger green": (47, 107, 79), "lime green": (182, 243, 106),
    "signal orange": (242, 107, 29), "safety orange": (232, 117, 42), "copper": (217, 101, 59),
    "red-orange": (255, 90, 54), "brick red": (160, 82, 45), "amber": (255, 176, 0),
    "golden yellow": (255, 182, 39), "warm sand": (233, 216, 166), "manila beige": (232, 211, 162),
    "chalk white": (244, 241, 234), "cream": (245, 241, 230), "pale silver": (216, 222, 228),
    "chrome silver": (199, 204, 209), "walnut brown": (59, 47, 42), "white": (250, 250, 250),
}


def colour_name(hex_code: str) -> str:
    h = hex_code.strip().lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", h):
        return ""
    rgb = tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    return min(_NAMES, key=lambda n: sum((a - b) ** 2 for a, b in zip(_NAMES[n], rgb, strict=True)))


def clean_style(style_prompt: str) -> str:
    """The brand style without hex codes or text-inviting phrases."""
    s = _HEX.sub("", style_prompt or "")
    for rx, repl in _TEXTY:
        s = rx.sub(repl, s)
    s = re.sub(r"\s+,", ",", s)
    s = re.sub(r",\s*,+", ",", s)
    return re.sub(r"\s{2,}", " ", s).strip(" ,.")


def palette_phrase(palette: list[str]) -> str:
    names = list(dict.fromkeys(n for n in (colour_name(c) for c in palette) if n))
    if not names:
        return ""
    joined = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    return f"Colour palette of {joined}"


def _sentence(text: str) -> str:
    t = (text or "").strip().rstrip(".,;")
    return f"{t[0].upper()}{t[1:]}." if t else ""


def build_prompt(visual_prompt: str, brand: BrandPack, shot: str = "", fmt: str = "short") -> str:
    """The full Z-Image prompt for one beat (see the module docstring for the template)."""
    frame = ("Tall vertical portrait composition" if fmt == "short"
             else "Wide horizontal landscape composition")
    cls = shot_class(shot)
    framing = SHOT_FRAMING.get(cls, DEFAULT_FRAMING)
    style = clean_style(brand.style_prompt)
    rendered = bool(_RENDERED.search(f"{brand.style_prompt} {shot} {visual_prompt}"))
    if cls in ("diagram", "cutaway"):
        rendered = True
    parts = [
        _sentence(frame),
        _sentence(framing),
        _sentence(visual_prompt),
        _sentence(style),
        _sentence(palette_phrase(brand.palette)),
        _sentence(BOOST_RENDERED if rendered else BOOST_PHOTO),
        NO_TEXT,
    ]
    return " ".join(p for p in parts if p)
