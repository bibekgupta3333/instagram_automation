"""Edit plan, karaoke ASS captions, loudness-normalised audio and the final encodes.

Timeline maths: shot boundaries are rounded to 30 fps frames. A boundary on a bar line gets
a 12-frame (0.4 s) crossfade centred on it; any other boundary gets a 2-frame dissolve
(effectively a cut). Each conformed clip therefore carries half a fade of overlap at each
inner edge, so xfade offsets land exactly on the musical boundaries and the total length
equals the song length to the frame.
"""

from __future__ import annotations

import itertools
import json
import math
import os
import re
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

from .clips import FPS, Format
from .episode import Shot, Timeline, frame_boundaries, letters

REPO = Path(__file__).resolve().parents[2]
FONTS_DIR = REPO / "assets" / "fonts" / "kids"
FONT = "Fredoka SemiBold"  # OFL, assets/fonts/kids/; libass falls back to a system font
FALLBACK_FONT = "Arial Rounded MT Bold"
BAR_FADE, CUT_FADE = 12, 2
LOUDNORM = "I=-14:TP=-1.5:LRA=11"

# ASS colours are &HAABBGGRR
WHITE = "&H00FFFFFF"
SUNG = "&H00A8F1FF"     # pale butter  #FFF1A8
CURRENT = "&H003FD2FF"  # sunny yellow #FFD23F
OUTLINE = "&H00663A5B"  # plum         #5B3A66
SHADOW = "&H96301A33"
TITLE = "&H00D9B8FF"    # candy pink   #FFB8D9


def plan_edit(tl: Timeline, fps: int = FPS) -> list[dict]:
    b = frame_boundaries(tl, fps)
    fades = [0] + [BAR_FADE if tl.is_bar(s.start_beat) else CUT_FADE for s in tl.shots[1:]]
    plan = []
    for k, shot in enumerate(tl.shots):
        front = fades[k] // 2 if k > 0 else 0
        back = fades[k + 1] // 2 if k + 1 < len(tl.shots) else 0
        plan.append({"shot": k, "start_frame": b[k], "end_frame": b[k + 1], "front": front,
                     "back": back, "need_frames": b[k + 1] - b[k] + front + back,
                     "fade_in": fades[k], "offset": (b[k] - fades[k] / 2) / fps if k else 0.0})
    return plan


# ---------------------------------------------------------------- captions


def ass_time(t: float) -> str:
    cs = max(0, round(t * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _esc(text: str) -> str:
    return text.replace("\\", "/").replace("{", "(").replace("}", ")").replace("\n", " ")


def caption_events(tl: Timeline) -> list[tuple[float, float, int, int | None]]:
    """(start, end, line index, current note index or None) covering each visible line."""
    events = []
    for li, line in enumerate(tl.lines):
        prev_end = tl.lines[li - 1].end if li else 0.0
        nxt = tl.lines[li + 1].start if li + 1 < len(tl.lines) else tl.total_sec
        vis0 = max(line.start - 0.35, prev_end + 0.02 if li else 0.0)
        vis1 = min(line.end + 0.45, nxt - 0.37 if li + 1 < len(tl.lines) else tl.total_sec)
        vis1 = max(vis1, line.end)
        marks = [(vis0, None)]
        for idx in line.notes:
            n = tl.notes[idx]
            marks.append((n.start, idx))
        last = tl.notes[line.notes[-1]]
        marks.append((last.start + last.dur, -1))  # -1: everything sung
        marks.append((vis1, None))
        for (a, cur), (b, _) in itertools.pairwise(marks):
            if b - a > 0.005:
                events.append((a, b, li, cur))
    return events


def line_markup(tl: Timeline, li: int, current: int | None, pop: int) -> str:
    line = tl.lines[li]
    order = {idx: k for k, idx in enumerate(line.notes)}
    cur_rank = len(line.notes) if current == -1 else order.get(current, -1)
    parts = []
    for text, idx in line.pieces:
        if idx is None:
            parts.append(_esc(text))
            continue
        rank = order[idx]
        if rank == cur_rank:
            parts.append(f"{{\\1c{CURRENT}\\fscx{pop}\\fscy{pop}}}{_esc(text)}"
                         f"{{\\1c{WHITE}\\fscx100\\fscy100}}")
        elif rank < cur_rank:
            parts.append(f"{{\\1c{SUNG}}}{_esc(text)}{{\\1c{WHITE}}}")
        else:
            parts.append(_esc(text))
    return "".join(parts)


def ass_header(fmt: Format) -> str:
    w, h = fmt.width, fmt.height
    vertical = h >= w
    size = int(h * (0.052 if vertical else 0.078))
    margin_v = int(h * (0.20 if vertical else 0.07))
    margin_lr = int(w * 0.07)
    font = FONT if (FONTS_DIR / "Fredoka-SemiBold.ttf").is_file() else FALLBACK_FONT
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, \
Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, \
Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Lyric,{font},{size},{WHITE},{WHITE},{OUTLINE},{SHADOW},0,0,0,0,100,100,1,0,1,\
{max(4, size // 14)},{max(2, size // 30)},2,{margin_lr},{margin_lr},{margin_v},1
Style: Title,{font},{int(size * 1.45)},{TITLE},{WHITE},{OUTLINE},{SHADOW},0,0,0,0,100,100,2,0,1,\
{max(5, size // 11)},{max(3, size // 24)},5,{margin_lr},{margin_lr},0,1
Style: Note,{font},{max(18, size // 3)},&H40FFFFFF,{WHITE},&H80000000,&H00000000,0,0,0,0,100,\
100,0,0,1,2,0,8,20,20,20,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def build_ass(tl: Timeline, fmt: Format, mock: bool = False) -> str:
    head = ass_header(fmt)
    rows = []
    intro_end = tl.lines[0].start - 0.4 if tl.lines else min(3.0, tl.total_sec)
    if intro_end > 0.8:
        rows.append(f"Dialogue: 1,{ass_time(0.15)},{ass_time(intro_end)},Title,,0,0,0,,"
                    f"{{\\fad(350,350)}}{_esc(tl.title)}")
    for a, b, li, cur in caption_events(tl):
        rows.append(f"Dialogue: 0,{ass_time(a)},{ass_time(b)},Lyric,,0,0,0,,"
                    f"{line_markup(tl, li, cur, 114)}")
    if mock:
        rows.append(f"Dialogue: 2,{ass_time(0)},{ass_time(tl.total_sec)},Note,,0,0,0,,"
                    "MOCK CLIPS - DO NOT PUBLISH")
    return head + "\n".join(rows) + "\n"


# ---------------------------------------------------------------- song mode (sung WAV)

TITLE_CARD_SEC = 1.0
INTRO_MIN_SEC = 1.5  # a song whose first word starts at least this late "has an intro"
MIN_SHOT_SEC = 0.5


@dataclass
class Word:
    text: str
    start: float
    end: float


def load_words(path: Path, song_sec: float | None = None) -> list[Word]:
    """words.json: a list (or {"words": [...]}) of {word|text, start, end} in seconds, in
    lyric order (scripts/kids_song/sing.py writes this). Words the aligner could not place
    (start == end == 0) are interpolated between their aligned neighbours."""
    data = json.loads(Path(path).read_text())
    if isinstance(data, dict):
        data = data.get("words") or data.get("segments") or []
    raw: list[tuple[str, float | None, float | None]] = []
    for i, item in enumerate(data):
        text = str(item.get("word", item.get("text", ""))).strip()
        try:
            a, b = float(item["start"]), float(item["end"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"words.json item {i} needs numeric start/end ({exc})") from exc
        if not text:
            continue
        ok = math.isfinite(a) and math.isfinite(b) and b > a and not (a == 0 and b == 0)
        raw.append((text, a if ok else None, b if ok else None))
    known = [i for i, (_, a, _) in enumerate(raw) if a is not None]
    if not known:
        raise ValueError(f"{path} has no timed words")
    words: list[Word] = []
    for i, (text, a, b) in enumerate(raw):
        if a is None:  # spread unaligned words over the gap between aligned neighbours
            prev = max((k for k in known if k < i), default=None)
            nxt = min((k for k in known if k > i), default=None)
            first = (prev + 1) if prev is not None else 0
            count = ((nxt if nxt is not None else len(raw)) - first) or 1
            lo = raw[prev][2] if prev is not None else max(0.0, raw[nxt][1] - 0.4 * count)
            hi = raw[nxt][1] if nxt is not None else lo + 0.4 * count
            step = max(0.0, hi - lo) / count
            a, b = lo + step * (i - first), lo + step * (i - first + 1)
        words.append(Word(text, a, max(b, a + 0.04)))
    for k in range(1, len(words)):  # keep lyric order monotonic
        if words[k].start < words[k - 1].start:
            words[k].start = words[k - 1].start
            words[k].end = max(words[k].end, words[k].start + 0.04)
    if song_sec is not None:
        for w in words:
            w.start, w.end = min(w.start, song_sec), min(w.end, song_sec)
    return words


def song_timeline(tl: Timeline, song_sec: float, words: list[Word]) -> tuple[Timeline, dict]:
    """Retime the episode's shots to the real song.

    Boundaries stay on the episode's beat grid (BPM), shifted so the first sung note lands
    on the first sung word. Shots that would start after the song are dropped; the last
    kept shot is stretched or trimmed to the song end. If the song has an intro, a 1 s
    title card (shot index -1) opens the video and shot 0 starts after it."""
    sung = [n for n in tl.notes if n.sung]
    first_note = sung[0].start if sung else tl.beat_time(tl.intro_beats)
    shift = words[0].start - first_note
    has_intro = words[0].start >= INTRO_MIN_SEC
    starts = [0.0] + [s.start + shift for s in tl.shots[1:]]
    kept: list[tuple[Shot, float]] = []
    for shot, a in zip(tl.shots, starts, strict=True):
        if a >= song_sec - MIN_SHOT_SEC:
            break  # the song ends first: drop this and every later shot
        if kept and a - kept[-1][1] < MIN_SHOT_SEC:
            continue  # too short after the shift: the previous shot absorbs it
        kept.append((shot, a))
    shots: list[Shot] = []
    title = False
    if has_intro and kept:
        first_end = kept[1][1] if len(kept) > 1 else song_sec
        if first_end - TITLE_CARD_SEC >= MIN_SHOT_SEC:
            title = True
            shots.append(Shot(-1, 0.0, 0.0, 0.0, TITLE_CARD_SEC, tl.title, "", "title card", 0))
            kept[0] = (kept[0][0], TITLE_CARD_SEC)
    beat = tl.beat_sec
    for k, (shot, a) in enumerate(kept):
        b = kept[k + 1][1] if k + 1 < len(kept) else song_sec
        shots.append(replace(shot, start=a, end=b, end_beat=shot.start_beat + (b - a) / beat))
    planned_end = (kept[-1][0].end + shift) if kept else song_sec
    new = replace(tl, shots=shots, total_beats=song_sec / beat)
    info = {"song_seconds": round(song_sec, 3), "beat_shift": round(shift, 3),
            "has_intro": has_intro, "title_card": title,
            "shots_kept": len(kept), "shots_dropped": len(tl.shots) - len(kept),
            "last_shot_delta": round(song_sec - planned_end, 3),
            "first_word": round(words[0].start, 3)}
    return new, info


def display_tokens(text: str) -> list[str]:
    """Printed lyric -> one token per aligned word (hyphenated words split, keeping the
    hyphen; letterless pieces such as '...' ride on the previous token)."""
    toks: list[str] = []
    for piece in re.findall(r"[^\s-]+-?|-", text):
        if letters(piece) or not toks:
            toks.append(piece)
        else:
            toks[-1] += (" " if not toks[-1].endswith("-") else "") + piece
    return toks


def group_words(words: list[Word], lyrics_lines: list[str]) -> list[list[tuple[str, Word]]]:
    """Caption lines of (display text, word). Matches the printed lyric lines by letters
    (keeping their punctuation and capitals); falls back to splitting at pauses."""
    lines: list[list[tuple[str, Word]]] = []
    cursor = 0
    for text in lyrics_lines:
        target, acc, members = letters(text), "", []
        while cursor < len(words) and len(acc) < len(target):
            acc += letters(words[cursor].text)
            members.append(words[cursor])
            cursor += 1
        if not target or acc != target:
            lines = []
            break
        tokens = display_tokens(text)
        shown = (tokens if len(tokens) == len(members)
                 else [w.text.lower() for w in members])
        lines.append(list(zip(shown, members, strict=True)))
    if lines and cursor == len(words):
        return lines
    lines, cur = [], []
    for w in words:
        if cur and (w.start - cur[-1][1].end > 0.6 or len(cur) >= 6):
            lines.append(cur)
            cur = []
        cur.append((w.text.lower(), w))
    if cur:
        lines.append(cur)
    for line in lines:  # sentence-case the fallback lines
        line[0] = (line[0][0][:1].upper() + line[0][0][1:], line[0][1])
    return lines


def song_caption_events(lines: list[list[tuple[str, Word]]], total: float
                        ) -> list[tuple[float, float, int, int | None]]:
    """(start, end, line index, current word rank | -1 all sung | None) per visible span."""
    events = []
    for li, line in enumerate(lines):
        start, end = line[0][1].start, line[-1][1].end
        prev_end = lines[li - 1][-1][1].end if li else 0.0
        nxt = lines[li + 1][0][1].start if li + 1 < len(lines) else total
        vis0 = max(start - 0.35, prev_end + 0.02 if li else 0.0)
        vis1 = min(end + 0.45, nxt - 0.37 if li + 1 < len(lines) else total)
        vis1 = max(vis1, end)
        marks = [(vis0, None)] + [(w.start, k) for k, (_, w) in enumerate(line)]
        marks += [(end, -1), (vis1, None)]
        for (a, cur), (b, _) in itertools.pairwise(marks):
            if b - a > 0.005:
                events.append((a, b, li, cur))
    return events


def word_line_markup(line: list[tuple[str, Word]], current: int | None, pop: int) -> str:
    cur_rank = len(line) if current == -1 else (-1 if current is None else current)
    parts = []
    for k, (text, _) in enumerate(line):
        if k == cur_rank:
            parts.append(f"{{\\1c{CURRENT}\\fscx{pop}\\fscy{pop}}}{_esc(text)}"
                         f"{{\\1c{WHITE}\\fscx100\\fscy100}}")
        elif k < cur_rank:
            parts.append(f"{{\\1c{SUNG}}}{_esc(text)}{{\\1c{WHITE}}}")
        else:
            parts.append(_esc(text))
        if k + 1 < len(line) and not text.endswith("-"):
            parts.append(" ")
    return "".join(parts)


def build_song_ass(tl: Timeline, words: list[Word], fmt: Format, mock: bool = False,
                   note: str = "PLACEHOLDER ART - DO NOT PUBLISH") -> str:
    """Word-level karaoke from words.json (same Fredoka styles and safe areas)."""
    lines = group_words(words, list(tl.raw.get("lyrics_lines") or []))
    rows = []
    intro_end = words[0].start - 0.4
    if intro_end > 0.8:
        rows.append(f"Dialogue: 1,{ass_time(0.1)},{ass_time(intro_end)},Title,,0,0,0,,"
                    f"{{\\fad(250,350)}}{_esc(tl.title)}")
    for a, b, li, cur in song_caption_events(lines, tl.total_sec):
        rows.append(f"Dialogue: 0,{ass_time(a)},{ass_time(b)},Lyric,,0,0,0,,"
                    f"{word_line_markup(lines[li], cur, 114)}")
    if mock:
        rows.append(f"Dialogue: 2,{ass_time(0)},{ass_time(tl.total_sec)},Note,,0,0,0,,{note}")
    return ass_header(fmt) + "\n".join(rows) + "\n"


# ---------------------------------------------------------------- audio


def _run(cmd: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, check=False)
    if result.returncode:
        raise RuntimeError(f"{cmd[0]} failed: {result.stderr.strip()[-800:]}")
    return result.stderr


def finalize_audio(mix_wav: Path, dest: Path) -> dict:
    """Two-pass loudnorm to -14 LUFS / -1.5 dBTP (linear when possible), 48 kHz 24-bit."""
    base = ["ffmpeg", "-hide_banner", "-nostats", "-threads", "3", "-i", str(mix_wav)]
    err = _run([*base, "-af", f"loudnorm={LOUDNORM}:print_format=json", "-f", "null", "-"])
    m = json.loads(re.findall(r"\{[^{}]*\}", err)[-1])
    filt = (f"loudnorm={LOUDNORM}:measured_I={m['input_i']}:measured_TP={m['input_tp']}:"
            f"measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:"
            f"offset={m['target_offset']}:linear=true")
    _run([*base, "-af", filt, "-ar", "48000", "-c:a", "pcm_s24le", "-y", str(dest)])
    return {"loudnorm_input": {k: m[k] for k in ("input_i", "input_tp", "input_lra")}}


def audio_stats(path: Path) -> dict:
    """Integrated LUFS, true peak (dBTP), sample peak (dBFS), clipped-sample count."""
    err = _run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-filter_complex",
                ("ebur128=peak=true+sample,astats=measure_overall=Peak_level+Number_of_samples"
                ":measure_perchannel=none"), "-f", "null", "-"])
    summary = err[err.rfind("Summary:"):]
    lufs = float(re.search(r"I:\s+(-?[\d.]+|-inf) LUFS", summary).group(1))
    tp = re.search(r"True peak:\s+Peak:\s+(-?[\d.]+|-inf)", summary)
    sp = re.search(r"Sample peak:\s+Peak:\s+(-?[\d.]+|-inf)", summary)
    import soundfile as sf

    data, sr = sf.read(str(path), dtype="float32")
    clipped = int((abs(data) >= 0.9999).sum())
    return {"integrated_lufs": lufs, "true_peak_dbtp": float(tp.group(1)) if tp else None,
            "sample_peak_dbfs": float(sp.group(1)) if sp else None,
            "clipped_samples": clipped, "seconds": round(len(data) / sr, 3)}


# ---------------------------------------------------------------- video


def video_graph(plan: list[dict], ass_name: str, fonts_rel: str | None) -> str:
    n = len(plan)
    parts, cur = [], "[0:v]"
    for k in range(1, n):
        d = plan[k]["fade_in"] / FPS
        nxt = f"[x{k}]"
        parts.append(f"{cur}[{k}:v]xfade=transition=fade:duration={d:.4f}:"
                     f"offset={plan[k]['offset']:.4f}{nxt}")
        cur = nxt
    fonts = f":fontsdir={fonts_rel}" if fonts_rel else ""
    parts.append(f"{cur}ass={ass_name}{fonts},format=yuv420p[vout]")
    return ";".join(parts)


def render_format(tl: Timeline, plan: list[dict], clips: list[Path], audio: Path, fmt: Format,
                  out_dir: Path, dest_name: str, mock: bool, ass_text: str | None = None) -> dict:
    ass_name = f"captions-{fmt.name}.ass"
    (out_dir / ass_name).write_text(ass_text if ass_text is not None else build_ass(tl, fmt, mock))
    fonts_rel = os.path.relpath(FONTS_DIR, out_dir) if FONTS_DIR.is_dir() else None
    total_frames = round(tl.total_sec * FPS)
    inputs = []
    for c in clips:
        inputs += ["-i", os.path.relpath(c, out_dir)]
    inputs += ["-i", os.path.relpath(audio, out_dir)]
    graph = video_graph(plan, ass_name, fonts_rel)
    tmp = f"{dest_name}.tmp.mp4"
    _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-threads", "3", *inputs,
          "-filter_complex", graph, "-map", "[vout]", "-map", f"{len(clips)}:a",
          "-frames:v", str(total_frames), "-r", str(FPS), "-c:v", "h264_videotoolbox",
          "-b:v", fmt.bitrate, "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
          "-ar", "48000", "-t", f"{tl.total_sec:.3f}", "-movflags", "+faststart", tmp],
         cwd=out_dir)
    os.replace(out_dir / tmp, out_dir / dest_name)
    return {"path": str(out_dir / dest_name), "width": fmt.width, "height": fmt.height,
            "frames": total_frames, "seconds": round(total_frames / FPS, 3)}


def probe_duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True,
                         check=True).stdout
    return float(out.strip()) if out.strip() and out.strip() != "N/A" else math.nan
