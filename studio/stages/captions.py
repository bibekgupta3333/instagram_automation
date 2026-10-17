"""Stage 4: captions. Word timestamps from parakeet-mlx (doc 14 §5, default), from Kokoro's
own timings (`kokoro-timestamps`, no second model), or a mock engine that spreads each
segment's words evenly over its narration time. Writes words.json, a styled
.ass and an .srt. The .ass holds Shorts-style captions (1-3 big upper-case words, thick outline
and shadow, the spoken word in the brand accent with a scale pop, kept in the 9:16 safe area:
clear of the bottom 18% UI band, the top 10% and the right rail; long-form gets smaller 1-2
line captions at the bottom) and each `beat.overlay` as an animated title card in the upper
third, timed to its beat, so no text ever has to be inside the AI images.
"""

from __future__ import annotations

import gc
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from studio.config import REPO_ROOT
from studio.contracts import BrandPack, Script, VideoJob
from studio.stages.base import (
    EngineUnavailable,
    StageContext,
    StageError,
    hash_inputs,
    is_cached,
    log,
    luminance,
    media_duration,
    palette_roles,
    write_cache,
)

STAGE = "captions"
VERSION = 4  # 4: safe caption text and nonzero ASS event durations
PARAKEET_MODEL = os.environ.get("STUDIO_PARAKEET_MODEL", "mlx-community/parakeet-tdt-0.6b-v2")
FONT_ENV = os.environ.get("STUDIO_CAPTION_FONT", "").strip()
# macOS system fallbacks, boldest first (libass finds them via CoreText)
SYSTEM_FONTS = (
    ("Arial Black", "/System/Library/Fonts/Supplemental/Arial Black.ttf"),
    ("Helvetica Neue", "/System/Library/Fonts/HelveticaNeue.ttc"),
    ("Impact", "/System/Library/Fonts/Supplemental/Impact.ttf"),
)
OVERLAY_DELAY_S = 0.12  # the title card lands just after the beat starts
OVERLAY_MAX_S = 3.0  # and leaves before it becomes wallpaper
CHUNK_S = 120.0  # parakeet chunk length for long narration (its CLI default)
CHUNK_OVER_S = 150.0  # transcribe in one pass below this

_parakeet: Any = None

Word = dict[str, Any]  # {"text": str, "start": float, "end": float}


def assets_fonts_dir() -> Path:
    return Path(os.environ.get("STUDIO_ASSETS_DIR") or REPO_ROOT / "assets") / "fonts"


def frame_size(fmt: str) -> tuple[int, int]:
    return (1080, 1920) if fmt == "short" else (1920, 1080)


# ---------------------------------------------------------------- engines


def mock_words(timings: dict[str, Any], duration: float) -> list[Word]:
    words: list[Word] = []
    segs = timings.get("segments") or [{"text": "", "start": 0.0, "end": duration}]
    for seg in segs:
        tokens = seg["text"].split()
        if not tokens:
            continue
        start, end = float(seg["start"]), float(seg["end"])
        step = (end - start) / len(tokens)
        for i, tok in enumerate(tokens):
            words.append(
                {"text": tok, "start": round(start + i * step, 3),
                 "end": round(start + (i + 1) * step, 3)}
            )
    return words


def _load_parakeet() -> Any:
    global _parakeet
    if _parakeet is None:
        try:
            from parakeet_mlx import from_pretrained  # lazy: optional local-audio extra
        except ImportError as exc:
            raise EngineUnavailable(
                "Captions engine 'parakeet' needs parakeet-mlx, which is not installed. Install "
                "the local-audio extra (`uv sync --extra local-audio`) or use --mock / "
                "--engine captions=mock (or captions=kokoro-timestamps after a kokoro TTS run)."
            ) from exc
        _parakeet = from_pretrained(PARAKEET_MODEL)
    return _parakeet


def parakeet_words(audio: Path) -> list[Word]:
    """Word timings from parakeet-mlx (verified on 0.5.3).

    `from_pretrained(repo).transcribe(path)` returns an AlignedResult whose sentences hold
    sub-word AlignedTokens (`text`, `start`, `end`, seconds). A token starting with a space
    begins a new word; others (sub-words, punctuation) continue the previous one. Audio is
    decoded with ffmpeg. Long narration is chunked to bound memory.
    """
    model = _load_parakeet()
    kwargs: dict[str, Any] = {}
    if media_duration(audio) > CHUNK_OVER_S:
        kwargs = {"chunk_duration": CHUNK_S, "overlap_duration": 15.0}
    try:
        result = model.transcribe(str(audio), **kwargs)
    finally:
        _clear_mlx_cache()  # don't let freed Metal buffers pile up across a batch
    words: list[Word] = []
    new_word = True
    for sentence in result.sentences:
        for tok in sentence.tokens:
            text = tok.text
            if not text.strip():  # a bare "▁" token: the next token starts a word
                new_word = True
                continue
            if new_word or not words or text[0].isspace():
                words.append({"text": text.strip(), "start": round(tok.start, 3),
                              "end": round(tok.end, 3)})
            else:  # sub-word or punctuation continuation
                words[-1]["text"] += text.strip()
                words[-1]["end"] = round(tok.end, 3)
            new_word = False
        new_word = True  # sentences never run into each other
    return words


def kokoro_words(workdir: Path) -> list[Word]:
    """Kokoro's own word timings (tts_words.json), written by the kokoro TTS engine.

    They come from the predicted phoneme durations that produced the audio, so they need no
    second model; they track parakeet within ~0.1 s on our tests. Parakeet stays the default
    because it measures the audio rather than trusting the synthesiser.
    """
    path = workdir / "tts_words.json"
    if not path.exists():
        raise StageError("captions: kokoro-timestamps needs tts_words.json from the kokoro TTS "
                         "engine (re-run tts with --engine tts=kokoro, or use parakeet)")
    return json.loads(path.read_text())


def _clear_mlx_cache() -> None:
    # Mock runs and cleanup must never initialise Metal merely to release it.
    mx = sys.modules.get("mlx.core")
    if mx is not None:
        mx.clear_cache()


def unload() -> None:
    """Free the resident ASR model (run_batch calls this after the captions stage)."""
    global _parakeet
    _parakeet = None
    gc.collect()
    _clear_mlx_cache()


# ---------------------------------------------------------------- writers


def _ass_colour(hex_colour: str, alpha: int = 0) -> str:
    h = hex_colour.lstrip("#")
    return f"&H{alpha:02X}{h[4:6]}{h[2:4]}{h[0:2]}".upper()


def _ass_time(t: float) -> str:
    cs = round(max(t, 0) * 100)
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _srt_time(t: float) -> str:
    ms = round(max(t, 0) * 1000)
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _clean(text: str) -> str:
    return " ".join(text.split()).replace("\\", "/").replace("{", "(").replace("}", ")")


def chunk_words(
    words: list[Word], size: int, max_chars: int = 0, max_gap: float = 0.0
) -> list[list[Word]]:
    """Group words into on-screen chunks: at most `size` words (and `max_chars` characters
    when set), always breaking after sentence punctuation and before a pause > max_gap."""
    chunks: list[list[Word]] = []
    cur: list[Word] = []
    for w in words:
        if cur:
            chars = sum(len(x["text"]) + 1 for x in cur) + len(w["text"])
            gap = float(w["start"]) - float(cur[-1]["end"])
            if (max_chars and chars > max_chars) or (max_gap and gap > max_gap):
                chunks.append(cur)
                cur = []
        cur.append(w)
        if len(cur) >= size or re.search(r"[.!?:;]$", w["text"]):
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    return chunks


def caption_font() -> str:
    """STUDIO_CAPTION_FONT, else the family of the first font bundled in assets/fonts/ (passed
    to libass as fontsdir by render), else a macOS system face (Arial Black, Helvetica Neue)."""
    if FONT_ENV:
        return FONT_ENV
    fonts = assets_fonts_dir()
    for p in sorted(fonts.glob("*")) if fonts.is_dir() else []:
        if p.suffix.lower() in (".ttf", ".otf"):
            family = _font_family(p)
            if family:
                return family
    for name, path in SYSTEM_FONTS:
        if Path(path).exists():
            return name
    return "Arial Black"


def _font_family(path: Path) -> str | None:
    try:
        from fontTools.ttLib import TTFont  # ships with matplotlib/pillow deps; optional
    except ImportError:
        return None
    try:
        with TTFont(str(path), lazy=True) as font:
            names = font["name"]
            rec = names.getName(16, 3, 1) or names.getName(1, 3, 1) or names.getName(1, 1, 0)
            return str(rec) if rec else None
    except Exception:  # noqa: BLE001 - any unreadable font file: fall through to system fonts
        return None


def _wrap_overlay(text: str, max_chars: int) -> str:
    """Upper-case card text, split into two balanced lines when longer than max_chars."""
    t = _clean(" ".join(text.split())).upper()
    words = t.split()
    if len(t) <= max_chars or len(words) < 2:
        return t
    best = min(range(1, len(words)),
               key=lambda i: abs(len(" ".join(words[:i])) - len(" ".join(words[i:]))))
    return " ".join(words[:best]) + "\\N" + " ".join(words[best:])


def overlay_events(script: Script | None, timings: dict[str, Any]) -> list[dict[str, Any]]:
    """[{text, start, end}] for each beat with an overlay, timed to the beat's narration:
    in OVERLAY_DELAY_S after the beat starts, out after at most OVERLAY_MAX_S."""
    if script is None:
        return []
    spans = {int(s["beat"]): (float(s["start"]), float(s["end"]))
             for s in timings.get("segments", []) if s.get("kind") == "beat"}
    out = []
    for i, beat in enumerate(script.beats):
        text = (beat.overlay or "").strip()
        if not text or i not in spans:
            continue
        start, end = spans[i]
        start += OVERLAY_DELAY_S
        end = min(end, start + OVERLAY_MAX_S)
        if end - start >= 0.5:
            out.append({"text": text, "start": round(start, 3), "end": round(end, 3)})
    return out


def write_ass(
    words: list[Word], brand: BrandPack, fmt: str, path: Path,
    overlays: list[dict[str, Any]] | None = None,
) -> None:
    """Shorts: 1-3 big bold upper-case words, thick outline + shadow, the spoken word in the
    brand accent with a scale pop, kept inside the safe area (clear of the bottom 18% UI band,
    the top 10% and the right-hand action rail). Long-form: smaller 1-2 line captions at the
    bottom. `overlays` become animated title cards in the upper third (brand colours)."""
    w, h = frame_size(fmt)
    roles = palette_roles(brand.palette)
    short = fmt == "short"
    font = caption_font()
    white, dark, accent = "&H00FFFFFF", _ass_colour(roles["dark"]), _ass_colour(roles["accent"])
    shadow_col = "&H64000000"  # black, ~60% opaque
    if short:
        size = int(h * 0.062)  # ~119 px on 1920
        outline, shadow = max(6, size // 10), max(3, size // 22)
        margin_v = int(h * 0.27)  # caption block sits at ~60-73% height: clear of the UI band
        margin_l, margin_r = int(w * 0.08), int(w * 0.14)  # 9:16: clear the action rail
    else:
        size = int(h * 0.05)
        outline, shadow = max(3, size // 16), 2
        margin_v = int(h * 0.06)
        margin_l = margin_r = int(w * 0.12)
    card = int(h * (0.062 if short else 0.075))
    card_text = dark if luminance(roles["accent"]) > 0.45 else white
    styles = [
        (f"Style: Word,{font},{size},{white},{accent},{dark},{shadow_col},-1,0,0,0,100,100,"
         f"{1 if short else 0},0,1,{outline},{shadow},2,{margin_l},{margin_r},{margin_v},1"),
        # BorderStyle 3 = opaque box in OutlineColour (the accent) padded by Outline px
        (f"Style: Card,{font},{card},{card_text},{accent},{accent},{shadow_col},-1,0,0,0,"
         f"100,100,2,0,3,{max(10, card // 5)},{max(4, card // 14)},8,{margin_l},{margin_r},0,1"),
    ]
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {w}", f"PlayResY: {h}",
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "YCbCr Matrix: TV.709", "",
        "[V4+ Styles]",
        ("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding"),
        *styles, "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    lines += _caption_events(words, short, accent, white)
    cx, cy = w // 2, int(h * (0.13 if short else 0.08))  # top edge: below the top 10%
    for ov in overlays or []:
        text = _wrap_overlay(ov["text"], 16 if short else 28)
        numeric = bool(re.search(r"\d", text)) and len(text) <= 9  # "40 TONS", "8 MIN"
        big = "\\fs" + str(int(card * 1.35)) if numeric else ""
        tilt = "\\frz-2" if short else ""
        anim = ("\\fad(90,140)\\fscx45\\fscy45\\t(0,150,\\fscx110\\fscy110)"
                "\\t(150,260,\\fscx100\\fscy100)")
        lines.append(
            f"Dialogue: 1,{_ass_time(ov['start'])},{_ass_time(ov['end'])},Card,,0,0,0,,"
            f"{{\\an8\\pos({cx},{cy}){tilt}{big}{anim}}}{text}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _caption_events(words: list[Word], short: bool, accent: str, white: str) -> list[str]:
    events = []
    chunks = (chunk_words(words, 3, max_chars=15, max_gap=0.45) if short
              else chunk_words(words, 8, max_chars=48, max_gap=0.7))
    for n, chunk in enumerate(chunks):
        nxt = chunks[n + 1][0]["start"] if n + 1 < len(chunks) else None
        chunk_end = float(chunk[-1]["end"]) + 0.12
        if nxt is not None and float(nxt) - chunk_end < 0.25:  # no flicker on tiny gaps
            chunk_end = float(nxt)
        if nxt is not None:
            chunk_end = min(chunk_end, float(nxt))
        texts = [_clean(x["text"]).upper() if short else _clean(x["text"]) for x in chunk]
        for i, word in enumerate(chunk):
            end = chunk[i + 1]["start"] if i + 1 < len(chunk) else chunk_end
            if round(float(end) * 100) <= round(float(word["start"]) * 100):
                continue
            if short:  # active word: accent + scale pop (ASS \t), the rest plain white
                on = (f"{{\\c{accent}\\fscx100\\fscy100\\t(0,70,\\fscx124\\fscy124)"
                      f"\\t(70,150,\\fscx112\\fscy112)}}")
                # words after the active one: the chunk's pop-in on its first event, else 100%
                off = (f"{{\\c{white}\\fscx88\\fscy88\\t(0,90,\\fscx100\\fscy100)}}" if i == 0
                       else f"{{\\c{white}\\fscx100\\fscy100}}")
            else:
                on, off = f"{{\\c{accent}}}", f"{{\\c{white}}}"
            parts = [f"{on}{t}{off}" if j == i else t for j, t in enumerate(texts)]
            lead = ""
            if i == 0:  # the chunk pops in
                lead = ("{\\fad(40,0)\\fscx88\\fscy88\\t(0,90,\\fscx100\\fscy100)}" if short
                        else "{\\fad(60,0)}")
            events.append(
                f"Dialogue: 0,{_ass_time(word['start'])},{_ass_time(end)},Word,,0,0,0,,"
                f"{lead}{' '.join(parts)}"
            )
    return events


def write_srt(words: list[Word], path: Path) -> None:
    out = []
    for n, chunk in enumerate(chunk_words(words, 7), start=1):
        text = " ".join(w["text"] for w in chunk)
        out.append(f"{n}\n{_srt_time(chunk[0]['start'])} --> {_srt_time(chunk[-1]['end'])}\n{text}\n")
    path.write_text("\n".join(out), encoding="utf-8")


# ---------------------------------------------------------------- stage


def run(job: VideoJob, brand: BrandPack, ctx: StageContext) -> VideoJob:
    if job.audio_path is None or not Path(job.audio_path).exists():
        raise StageError("captions: no narration audio")
    workdir = Path(job.workdir)
    audio = Path(job.audio_path)
    timings_path = workdir / "timings.json"
    ass, srt, words_path = workdir / "captions.ass", workdir / "captions.srt", workdir / "words.json"
    engine = ctx.engine("captions")
    tts_words = workdir / "tts_words.json"
    font = caption_font()
    overlays_src = [b.overlay for b in job.script.beats] if job.script else []
    key = hash_inputs(STAGE, VERSION, engine, audio, timings_path, brand.palette, job.format, font,
                      overlays_src, tts_words if engine == "kokoro-timestamps" else None,
                      PARAKEET_MODEL if engine == "parakeet" else None)
    if is_cached(job, STAGE, key, [ass, srt, words_path], ctx):
        job.captions_path = ass
        log(job, "captions: cached, skipped")
        return job

    if engine == "mock":
        timings = json.loads(timings_path.read_text()) if timings_path.exists() else {}
        words = mock_words(timings, media_duration(audio))
    elif engine == "parakeet":
        from studio.stages import tts

        tts.unload()  # TTS is done for this job; keep one model resident (doc 14 §9)
        words = parakeet_words(audio)
    elif engine == "kokoro-timestamps":
        words = kokoro_words(workdir)
    else:
        raise StageError(
            f"captions: unknown engine {engine!r} (parakeet, kokoro-timestamps, mock)"
        )
    if not words:
        raise StageError("captions: no words recognised")

    words_path.write_text(json.dumps(words, indent=1))
    timings = json.loads(timings_path.read_text()) if timings_path.exists() else {}
    overlays = overlay_events(job.script, timings)
    write_ass(words, brand, job.format, ass, overlays)
    write_srt(words, srt)
    job.captions_path = ass
    write_cache(job, STAGE, key, [ass, srt, words_path])
    log(job, f"captions: {engine} {len(words)} words, {len(overlays)} overlays, font {font}")
    return job
