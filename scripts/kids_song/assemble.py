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
from pathlib import Path

from .clips import FPS, Format
from .episode import Timeline, frame_boundaries

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


def build_ass(tl: Timeline, fmt: Format, mock: bool = False) -> str:
    w, h = fmt.width, fmt.height
    vertical = h >= w
    size = int(h * (0.052 if vertical else 0.078))
    margin_v = int(h * (0.20 if vertical else 0.07))
    margin_lr = int(w * 0.07)
    font = FONT if (FONTS_DIR / "Fredoka-SemiBold.ttf").is_file() else FALLBACK_FONT
    head = f"""[Script Info]
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
                  out_dir: Path, dest_name: str, mock: bool) -> dict:
    ass_name = f"captions-{fmt.name}.ass"
    (out_dir / ass_name).write_text(build_ass(tl, fmt, mock))
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
