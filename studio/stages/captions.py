"""Stage 4: captions. Word timestamps from parakeet-mlx (doc 14 §5), or a mock engine that
spreads each segment's words evenly over its narration time. Writes words.json, a styled
.ass (big centred karaoke-style words in the brand palette, 9:16 safe margins) and an .srt.
"""

from __future__ import annotations

import gc
import json
import os
import re
from pathlib import Path
from typing import Any

from studio.contracts import BrandPack, VideoJob
from studio.stages.base import (
    EngineUnavailable,
    StageContext,
    StageError,
    hash_inputs,
    is_cached,
    log,
    media_duration,
    palette_roles,
    write_cache,
)

STAGE = "captions"
VERSION = 1
PARAKEET_MODEL = os.environ.get("STUDIO_PARAKEET_MODEL", "mlx-community/parakeet-tdt-0.6b-v2")
FONT = os.environ.get("STUDIO_CAPTION_FONT", "Arial Black")

_parakeet: Any = None

Word = dict[str, Any]  # {"text": str, "start": float, "end": float}


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
            from parakeet_mlx import from_pretrained  # lazy: optional local-ml extra
        except ImportError as exc:
            raise EngineUnavailable(
                "Captions engine 'parakeet' needs parakeet-mlx, which is not installed. Install the "
                "local-ml extra (`uv sync --extra local-ml`) or use --mock / --engine captions=mock."
            ) from exc
        _parakeet = from_pretrained(PARAKEET_MODEL)
    return _parakeet


def parakeet_words(audio: Path) -> list[Word]:
    result = _load_parakeet().transcribe(str(audio))
    words: list[Word] = []
    for sentence in result.sentences:
        for tok in sentence.tokens:
            text = tok.text
            if not words or text.startswith(" "):
                if text.strip():
                    words.append({"text": text.strip(), "start": tok.start, "end": tok.end})
            else:  # sub-word continuation
                words[-1]["text"] += text
                words[-1]["end"] = tok.end
    return words


def unload() -> None:
    global _parakeet
    _parakeet = None
    gc.collect()


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
    return text.replace("\\", "/").replace("{", "(").replace("}", ")")


def chunk_words(words: list[Word], size: int) -> list[list[Word]]:
    chunks: list[list[Word]] = []
    cur: list[Word] = []
    for w in words:
        cur.append(w)
        if len(cur) >= size or re.search(r"[.!?:;]$", w["text"]):
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    return chunks


def write_ass(words: list[Word], brand: BrandPack, fmt: str, path: Path) -> None:
    w, h = frame_size(fmt)
    roles = palette_roles(brand.palette)
    short = fmt == "short"
    size = int(h * (0.05 if short else 0.06))
    margin_v = int(h * (0.30 if short else 0.08))  # 9:16: clear the bottom platform UI
    margin_l = int(w * 0.08)
    margin_r = int(w * (0.13 if short else 0.08))  # 9:16: clear the right-hand action rail
    style = (
        f"Style: Word,{FONT},{size},&H00FFFFFF,{_ass_colour(roles['accent'])},"
        f"{_ass_colour(roles['dark'])},&H80000000,-1,0,0,0,100,100,0,0,1,"
        f"{max(4, size // 14)},2,2,{margin_l},{margin_r},{margin_v},1"
    )
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {w}", f"PlayResY: {h}",
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        ("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding"),
        style, "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    accent = _ass_colour(roles["accent"])
    for chunk in chunk_words(words, 3 if short else 6):
        chunk_end = chunk[-1]["end"]
        texts = [_clean(x["text"]).upper() for x in chunk]
        for i, word in enumerate(chunk):
            end = chunk[i + 1]["start"] if i + 1 < len(chunk) else chunk_end
            parts = [
                f"{{\\c{accent}\\fscx115\\fscy115}}{t}{{\\r}}" if j == i else t
                for j, t in enumerate(texts)
            ]
            pop = "{\\fad(60,0)}" if i == 0 else ""
            lines.append(
                f"Dialogue: 0,{_ass_time(word['start'])},{_ass_time(end)},Word,,0,0,0,,"
                f"{pop}{' '.join(parts)}"
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


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
    key = hash_inputs(STAGE, VERSION, engine, audio, timings_path, brand.palette, job.format, FONT)
    if is_cached(job, STAGE, key, [ass, srt, words_path], ctx):
        job.captions_path = ass
        log(job, "captions: cached, skipped")
        return job

    if engine == "mock":
        timings = json.loads(timings_path.read_text()) if timings_path.exists() else {}
        words = mock_words(timings, media_duration(audio))
    elif engine == "parakeet":
        words = parakeet_words(audio)
    else:
        raise StageError(f"captions: unknown engine {engine!r} (parakeet, mock)")
    if not words:
        raise StageError("captions: no words recognised")

    words_path.write_text(json.dumps(words, indent=1))
    write_ass(words, brand, job.format, ass)
    write_srt(words, srt)
    job.captions_path = ass
    write_cache(job, STAGE, key, [ass, srt, words_path])
    log(job, f"captions: {engine} {len(words)} words")
    return job
