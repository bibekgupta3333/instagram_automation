"""Stage 6: render with ffmpeg: retention editing (doc 06 §3), burned-in ASS captions and
overlays, and a mixed, loudness-normalised soundtrack. h264_videotoolbox, falling back to libx264.

**Pacing.** Narration blocks (one per beat image; the hook joins beat 0, the CTA joins the last
beat) are cut into shots of ~2.4 s (never over MAX_SHOT_S), so something changes every 2-3 s.
The first shot cuts at FIRST_SHOT_S (fast start). Inside a block, shots alternate between the
plain move and a *punch-in* (a tighter crop of the same still/clip), which reads as a cut
without needing a new image. Hard cuts inside a block; between blocks a short (6-10 frame)
ffmpeg `xfade` transition rotated per job (dissolve, zoom-through, smooth slides, flash). Each
xfade is centred on the narration boundary: the outgoing block is padded with T//2 cloned
frames and the incoming with T-T//2, so offsets come straight from the timings and the output
stays frame-exact with the audio.

**Motion for stills** (STUDIO_MOTION): `parallax` turns every still shot into a 2.5D
depth-parallax clip of exactly the shot's length (studio/stages/motion.py, Depth Anything V2
Small; presets rotate per shot, seeded by the job id), which then goes through the clip path.
`zoompan` is the ffmpeg Ken Burns fallback. Unset/`auto` = parallax when the depth model is
downloaded, except with mock/silence narration, which stays on fast zoompan. Placeholder
images always use zoompan, even when parallax is requested. A missing
model or a parallax failure falls back to zoompan with a log line, never a failed render.
Stock clips (images.py `pixabay` engine returns .mp4) mix with stills: a clip shot is read from
an offset that advances across consecutive shots of the same clip (looped if too short),
scaled and centre-cropped to fill the frame, and its own audio is never mapped (muted).

**Text.** captions.ass (captions stage) holds the word captions and the `beat.overlay` title
cards; libass burns them in. Fonts dropped in assets/fonts/ are passed as `fontsdir`.

**Audio.** voice: high-pass 70 Hz -> gentle compression (2.5:1 at -20 dBFS). Optional music bed
from assets/music/<mood>/ (mood per brand, STUDIO_MUSIC_MOOD overrides, STUDIO_MUSIC=0 turns it
off): looped/trimmed to length, faded, set MUSIC_BED_DB under the voice and sidechain-ducked by
the voice to ~-20 dB under speech. Optional whooshes from assets/sfx/ on the transitions. The
mix then gets the two-pass loudnorm to -14 LUFS (measured on the mix itself). No music files =
voice only, exactly as before.

Writes video.mp4 and content_hash.txt (sha256 of the video bytes + description). Approval
binds to that hash, so any re-render that changes it voids the approval.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from studio.config import REPO_ROOT
from studio.contracts import BrandPack, JobStatus, VideoJob
from studio.stages import motion
from studio.stages.base import (
    StageContext,
    StageError,
    hash_inputs,
    is_cached,
    log,
    media_duration,
    run_ffmpeg,
    write_cache,
)
from studio.stages.visuals.common import is_video

STAGE = "render"
VERSION = 5  # 5: finite loudness measurements, bounded CPU threads and safe optional assets
# 3: STUDIO_MOTION=parallax (2.5D depth parallax clips for stills), zoompan fallback
# 4: retention edit: 2-3 s shots with punch-ins, fast first cut, xfade transitions between
#    beats, voice compression, music bed with sidechain ducking, transition whooshes
FPS = 30
MAX_SHOT_S = 3.0  # hard cap on a shot (doc 06 §3: a change every 2-4 s)
TARGET_SHOT_S = 2.4  # preferred shot length inside a block
FIRST_SHOT_S = 1.3  # fast start: the opening shot cuts within 1.5 s
MIN_SHOT_S = 0.7  # never split a block into slivers shorter than this
OVERSCAN = 1.25  # pre-scale before zoompan to limit jitter and leave room to pan
PUNCH = 1.22  # punch-in crop factor for the alternate shots of a block
XFADE_FRAMES = (6, 8, 10)
# xfade transitions between beats: dissolve, zoom-through, smooth slides, a white flash
TRANSITIONS = ("fade", "zoomin", "smoothleft", "smoothup", "fadewhite", "smoothright")

# zoompan expressions; D = frames-1 of the shot. No commas, so no filtergraph escaping.
MOTIONS: dict[str, tuple[str, str, str]] = {
    "zoom_in": ("1+0.18*on/D", "iw/2-iw/zoom/2", "ih/2-ih/zoom/2"),
    "zoom_out": ("1.18-0.18*on/D", "iw/2-iw/zoom/2", "ih/2-ih/zoom/2"),
    "pan_right": ("1.15", "(iw-iw/zoom)*on/D", "(ih-ih/zoom)/2"),
    "pan_left": ("1.15", "(iw-iw/zoom)*(1-on/D)", "(ih-ih/zoom)/2"),
    "pan_down": ("1.15", "(iw-iw/zoom)/2", "(ih-ih/zoom)*on/D"),
    "pan_up": ("1.15", "(iw-iw/zoom)/2", "(ih-ih/zoom)*(1-on/D)"),
    "zoom_in_top": ("1+0.2*on/D", "iw/2-iw/zoom/2", "(ih-ih/zoom)*0.25"),
    "push_diagonal": ("1.06+0.12*on/D", "(iw-iw/zoom)*on/D", "(ih-ih/zoom)*on/D"),
}
# punch-in for a zoompan still: start tight (PUNCH x) and keep pushing slowly
PUNCH_MOTION = (f"{PUNCH + 0.08}+0.07*on/D", "iw/2-iw/zoom/2", "(ih-ih/zoom)*0.42")

ASSETS_DIR = REPO_ROOT / "assets"
AUDIO_SUFFIXES = (".mp3", ".m4a", ".wav", ".ogg", ".flac", ".aac")
# brand slug -> music moods (folders under assets/music/), first non-empty one wins
BRAND_MOODS: dict[str, tuple[str, ...]] = {
    "gravity-what-if": ("cinematic", "ambient"),
    "megabuild-blueprint": ("cinematic", "documentary"),
    "wing-and-turbine": ("cinematic", "documentary"),
    "business-model-files": ("documentary", "corporate"),
    "car-cost-truth": ("upbeat", "corporate"),
    "square-foot-money": ("upbeat", "corporate"),
    "furnace-to-faucet": ("upbeat", "documentary"),
    "software-shortcut": ("tech", "upbeat"),
    "lock-screen-logic": ("tech", "ambient"),
    "rank-test-lab": ("tech", "corporate"),
}
DEFAULT_MOODS = ("upbeat",)
MUSIC_BED_DB = -12.0  # music loudness vs the voice before ducking (between sentences)
# sidechain keyed by the voice. Measured on a 42 s Kokoro narration (100 ms RMS windows): the
# bed sits ~-13 dB under the voice in pauses and ~-20 dB under it while the voice speaks
DUCK = "threshold=0.03:ratio=4:attack=20:release=350:makeup=1:knee=4"
SFX_DB = -9.0  # whoosh loudness vs the voice
VOICE_CHAIN = "highpass=f=70,acompressor=threshold=0.1:ratio=2.5:attack=8:release=160:makeup=1.5"
LOUDNORM = "I=-14:TP=-1.5:LRA=11"


def frame_size(fmt: str) -> tuple[int, int]:
    return (1080, 1920) if fmt == "short" else (1920, 1080)


def motion_sequence(seed: str, n: int) -> list[str]:
    """Shuffled per job, cycled; adjacent shots never repeat a motion."""
    order = list(MOTIONS)
    rng = random.Random(seed)
    rng.shuffle(order)
    start = rng.randrange(len(order))
    return [order[(start + i) % len(order)] for i in range(n)]


def transition_sequence(seed: str, n: int) -> list[tuple[str, int]]:
    """(xfade transition, frames) per beat boundary; rotated per job, no adjacent repeats."""
    rng = random.Random(f"xfade:{seed}")
    order = list(TRANSITIONS)
    rng.shuffle(order)
    return [(order[i % len(order)], rng.choice(XFADE_FRAMES)) for i in range(n)]


def _split_block(start: float, end: float, first: bool) -> list[float]:
    """Shot start times for one narration block: ~TARGET_SHOT_S, <= MAX_SHOT_S, and on the
    opening block a fast first cut at FIRST_SHOT_S."""
    starts = [start]
    if first and end - start > FIRST_SHOT_S + MIN_SHOT_S:
        start += FIRST_SHOT_S
        starts.append(start)
    length = end - start
    parts = max(1, round(length / TARGET_SHOT_S), math.ceil(length / MAX_SHOT_S - 1e-6))
    while parts > 1 and length / parts < MIN_SHOT_S:
        parts -= 1
    step = length / parts
    starts += [start + k * step for k in range(1, parts)]
    return starts


def plan_shots(timings: dict[str, Any], n_images: int, duration: float, seed: str) -> list[dict]:
    """Blocks of narration per image (hook joins beat 0, CTA joins the last beat), each split
    into shots (_split_block) with frame-exact boundaries summing to the audio length.

    Each shot: image, frames, motion, block (index of its narration block), punch (alternate
    shots inside a block are a tighter crop), and on the first shot of every block after the
    first: xfade + xfade_frames (the transition into it)."""
    if n_images < 1 or not math.isfinite(duration) or duration <= 0:
        raise StageError("render: need images and a finite positive narration duration")
    blocks: list[dict[str, Any]] = []
    for seg in timings.get("segments", []):
        img = max(0, min(int(seg.get("beat", 0)), n_images - 1))
        if blocks and blocks[-1]["image"] == img:
            continue
        blocks.append({"image": img, "start": float(seg["start"])})
    if not blocks:
        blocks = [{"image": i, "start": duration * i / n_images} for i in range(n_images)]
    blocks[0]["start"] = 0.0
    total_frames = max(1, round(duration * FPS))
    cuts: list[tuple[int, int, int, float]] = []  # (image, block, index in block, start)
    for i, b in enumerate(blocks):
        end = blocks[i + 1]["start"] if i + 1 < len(blocks) else duration
        for j, t in enumerate(_split_block(b["start"], end, first=i == 0)):
            cuts.append((b["image"], i, j, t))
    motions = motion_sequence(seed, len(cuts))
    shots: list[dict[str, Any]] = []
    for k, (img, blk, j, start) in enumerate(cuts):
        f0 = round(start * FPS)
        f1 = round(cuts[k + 1][3] * FPS) if k + 1 < len(cuts) else total_frames
        if f1 > f0:
            shots.append({"image": img, "frames": f1 - f0, "motion": motions[k], "block": blk,
                          "punch": j % 2 == 1})
    # renumber blocks densely (a zero-length block can vanish) and attach transitions
    order = {b: n for n, b in enumerate(dict.fromkeys(s["block"] for s in shots))}
    trans = transition_sequence(seed, max(0, len(order) - 1))
    prev = None
    for s in shots:
        s["block"] = order[s["block"]]
        if s["block"] != prev and s["block"] > 0:
            name, frames = trans[s["block"] - 1]
            s["xfade"], s["xfade_frames"] = name, frames
        prev = s["block"]
    _fit_transitions(shots)
    return shots


def _fit_transitions(shots: list[dict]) -> None:
    """Shrink a transition (or drop it for a hard cut) when a neighbouring block is too short
    to hold half of it on each side."""
    lengths: dict[int, int] = {}
    for s in shots:
        lengths[s["block"]] = lengths.get(s["block"], 0) + s["frames"]
    for s in shots:
        if "xfade" not in s:
            continue
        room = min(lengths[s["block"] - 1], lengths[s["block"]]) // 2
        if room < 4:
            s.pop("xfade")
            s.pop("xfade_frames")
        else:
            s["xfade_frames"] = min(s["xfade_frames"], room)


# ---------------------------------------------------------------- audio


def _loudnorm_json(stderr: str) -> dict[str, str]:
    return json.loads(re.findall(r"\{[^{}]*\}", stderr)[-1])


def _cpu_threads() -> str:
    try:
        return str(max(1, int(os.environ.get("STUDIO_CPU_THREADS", "3"))))
    except ValueError as exc:
        raise StageError("render: STUDIO_CPU_THREADS must be an integer") from exc


def measure(inputs: list[str], graph: str, cwd: Path | None = None) -> dict[str, str] | None:
    """Run `graph` (an -filter_complex whose output pad is [m]) through loudnorm's analyser
    and return its JSON (input_i, input_tp, ...), or None when the measurement fails."""
    threads = _cpu_threads()
    bounded_inputs = []
    for arg in inputs:
        if arg == "-i":
            bounded_inputs += ["-threads", threads]
        bounded_inputs.append(arg)
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-nostdin",
           "-filter_threads", threads, "-filter_complex_threads", threads,
           *bounded_inputs, "-filter_complex",
           f"{graph};[m]loudnorm={LOUDNORM}:print_format=json[o]", "-map", "[o]",
           "-threads", threads, "-f", "null", "-"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=180, check=True,
                             cwd=cwd).stderr
        m = _loudnorm_json(out)
        if not all(math.isfinite(float(m[k])) for k in
                   ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")):
            return None
        return m
    except (subprocess.SubprocessError, OSError, IndexError, KeyError, ValueError):
        return None


def _input_lufs(path: Path, seconds: float | None = None) -> float | None:
    trim = ["-t", f"{seconds:.3f}"] if seconds else []
    m = measure([*trim, "-i", str(path)], "[0:a]anull[m]")
    if m is None or not math.isfinite(float(m["input_i"])) or float(m["input_i"]) < -70:
        return None
    return float(m["input_i"])


def _loudnorm(audio: Path) -> str:
    """Two-pass loudnorm of the narration alone (kept for callers/tests): measure, then apply
    a linear gain to hit -14 LUFS. Falls back to single-pass (dynamic) if measuring fails."""
    m = measure(["-i", str(audio)], "[0:a]anull[m]")
    return _loudnorm_filter(m)


def _loudnorm_filter(m: dict[str, str] | None) -> str:
    if m is None:
        return f"loudnorm={LOUDNORM}"
    return (
        f"loudnorm={LOUDNORM}:measured_I={m['input_i']}:measured_TP={m['input_tp']}"
        f":measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}"
        f":offset={m['target_offset']}:linear=true"
    )


def assets_dir() -> Path:
    return Path(os.environ.get("STUDIO_ASSETS_DIR") or ASSETS_DIR).resolve()


def _audio_files(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir()
                  if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES
                  and not p.name.startswith("."))


def brand_moods(brand: BrandPack | None) -> tuple[str, ...]:
    env = os.environ.get("STUDIO_MUSIC_MOOD", "").strip().lower()
    if env:
        return tuple(m.strip() for m in env.split(",") if m.strip())
    return BRAND_MOODS.get(brand.slug if brand else "", DEFAULT_MOODS)


def pick_music(brand: BrandPack | None, seed: str) -> Path | None:
    """A track from assets/music/<mood>/ for the brand's first mood that has any, picked
    deterministically per job (so re-renders are stable). None when music is off/absent."""
    if os.environ.get("STUDIO_MUSIC", "1").strip().lower() in ("0", "off", "no", "false"):
        return None
    for mood in brand_moods(brand):
        tracks = _audio_files(assets_dir() / "music" / mood)
        if tracks:
            idx = int(hashlib.sha256(f"music:{seed}".encode()).hexdigest(), 16) % len(tracks)
            return tracks[idx]
    return None


def pick_sfx() -> list[Path]:
    if os.environ.get("STUDIO_SFX", "1").strip().lower() in ("0", "off", "no", "false"):
        return []
    return _audio_files(assets_dir() / "sfx")


def transition_times(shots: list[dict]) -> list[float]:
    """Seconds at which each beat-to-beat transition is centred (the block boundary)."""
    t, out = 0, []
    for s in shots:
        if "xfade" in s:
            out.append(t / FPS)
        t += s["frames"]
    return out


def audio_graph(
    voice_idx: int, duration: float, music_idx: int | None = None, music_gain_db: float = 0.0,
    sfx: list[tuple[int, float, list[float]]] | None = None,
) -> str:
    """Pre-loudnorm audio graph ending in [m]: compressed voice, optional ducked music bed,
    optional whooshes. sfx = [(input index, gain dB, [start seconds, ...]), ...]."""
    parts = [f"[{voice_idx}:a]aresample=48000,aformat=channel_layouts=stereo,{VOICE_CHAIN}"
             + (",asplit=2[voice][key]" if music_idx is not None else "[voice]")]
    mix = ["[voice]"]
    if music_idx is not None:
        fade_out = max(duration - 1.5, 0.0)
        parts.append(
            f"[{music_idx}:a]aresample=48000,aformat=channel_layouts=stereo,"
            f"atrim=0:{duration:.3f},asetpts=N/SR/TB,volume={music_gain_db:.2f}dB,"
            f"afade=t=in:d=0.4,afade=t=out:st={fade_out:.3f}:d=1.5[bed]"
        )
        parts.append(f"[bed][key]sidechaincompress={DUCK}[duck]")
        mix.append("[duck]")
    n = 0
    for idx, gain, starts in sfx or []:
        if not starts:
            continue
        labels = [f"[sx{n + i}]" for i in range(len(starts))]
        parts.append(f"[{idx}:a]aresample=48000,aformat=channel_layouts=stereo,"
                     f"volume={gain:.2f}dB,asplit={len(starts)}{''.join(labels)}"
                     if len(starts) > 1 else
                     f"[{idx}:a]aresample=48000,aformat=channel_layouts=stereo,"
                     f"volume={gain:.2f}dB{labels[0]}")
        for i, t in enumerate(starts):
            ms = max(0, round(t * 1000))
            # padded to the full length: amix inputs that end early dip the mix level
            parts.append(f"{labels[i]}adelay={ms}:all=1,apad=whole_dur={duration:.3f}"
                         f"[sd{n + i}]")
            mix.append(f"[sd{n + i}]")
        n += len(starts)
    if len(mix) == 1:
        parts.append("[voice]anull[m]")
    else:
        parts.append(f"{''.join(mix)}amix=inputs={len(mix)}:duration=first:normalize=0:"
                     "dropout_transition=0[m]")
    return ";".join(parts)


def plan_audio(
    audio: Path, duration: float, shots: list[dict], brand: BrandPack | None, seed: str,
    first_input: int,
) -> dict[str, Any]:
    """Decide the soundtrack: inputs (ffmpeg args), the pre-loudnorm graph and a summary."""
    # loudness of the voice as it enters the mix (after the compressor's makeup gain)
    vm = measure(["-i", str(audio)], f"[0:a]aresample=48000,{VOICE_CHAIN}[m]")
    voice_lufs = float(vm["input_i"]) if vm and float(vm["input_i"]) > -70 else None
    inputs = ["-i", str(audio.resolve())]
    voice_idx, nxt = first_input, first_input + 1
    music, music_idx, music_gain = pick_music(brand, seed), None, 0.0
    summary: dict[str, Any] = {"music": None, "sfx": []}
    if music is not None:
        m_lufs = _input_lufs(music, seconds=min(duration + 5, 600))
        if m_lufs is None or voice_lufs is None:
            summary["music_skipped"] = f"could not measure loudness of {music.name}"
            music = None
        else:
            music_gain = (voice_lufs + MUSIC_BED_DB) - m_lufs
            inputs += ["-stream_loop", "-1", "-i", str(music.resolve())]
            music_idx, nxt = nxt, nxt + 1
            summary.update(music=str(music), music_gain_db=round(music_gain, 2))
    sfx_specs: list[tuple[int, float, list[float]]] = []
    sfx_files = pick_sfx()
    times = transition_times(shots)
    if sfx_files and times and voice_lufs is not None:
        by_file: dict[int, list[float]] = {}
        for k, t in enumerate(times):
            by_file.setdefault(k % len(sfx_files), []).append(max(t - 0.18, 0.0))
        for fi, starts in sorted(by_file.items()):
            s_lufs = _input_lufs(sfx_files[fi])
            if s_lufs is None:
                continue  # optional, unreadable/silent effects must not break the render
            gain = (voice_lufs + SFX_DB) - s_lufs
            inputs += ["-i", str(sfx_files[fi].resolve())]
            sfx_specs.append((nxt, gain, starts))
            summary["sfx"].append(sfx_files[fi].name)
            nxt += 1
    graph = audio_graph(voice_idx, duration, music_idx, music_gain, sfx_specs)
    return {"inputs": inputs, "graph": graph, "summary": summary,
            "files": [p for p in (music, *sfx_files) if p is not None]}


# ---------------------------------------------------------------- video graph


def _shot_filter(k: int, shot: dict, w: int, h: int) -> str:
    sw, sh = int(w * OVERSCAN) // 2 * 2, int(h * OVERSCAN) // 2 * 2
    if shot.get("kind") in CLIP_KINDS:
        # motion is ignored: the clip moves on its own. fps first so trim is frame-exact.
        punch = ""
        if shot.get("punch"):
            cw, ch = int(w / PUNCH) // 2 * 2, int(h / PUNCH) // 2 * 2
            punch = f",crop={cw}:{ch}:{(w - cw) // 2}:{int((h - ch) * 0.42)},scale={w}:{h}"
        return (
            f"[{k}:v]fps={FPS},scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h}{punch},setsar=1,format=yuv420p,trim=end_frame={shot['frames']},"
            f"setpts=PTS-STARTPTS[v{k}]"
        )
    expr = PUNCH_MOTION if shot.get("punch") else MOTIONS[shot["motion"]]
    z, x, y = (e.replace("D", str(max(shot["frames"] - 1, 1))) for e in expr)
    return (
        f"[{k}:v]scale={sw}:{sh}:force_original_aspect_ratio=increase,crop={sw}:{sh},"
        f"zoompan=z='{z}':x='{x}':y='{y}':d={shot['frames']}:s={w}x{h}:fps={FPS},"
        f"setsar=1,format=yuv420p[v{k}]"
    )


def _quote_path(p: Path) -> str:
    """Single-quoted filter option value (literal inside the quotes; ' itself can't occur)."""
    if "'" in str(p):
        raise StageError(f"render: path with a quote is not supported in filtergraphs: {p}")
    return f"'{p}'"


def video_graph(shots: list[dict], w: int, h: int, ass_name: str,
                fonts_dir: Path | None = None) -> str:
    """Per-shot filters, hard cuts (concat) inside a block, centred xfades between blocks."""
    parts = [_shot_filter(k, s, w, h) for k, s in enumerate(shots)]
    groups: list[list[int]] = []
    for k, s in enumerate(shots):
        if not groups or s["block"] != shots[groups[-1][0]]["block"]:
            groups.append([])
        groups[-1].append(k)
    t_in = [shots[g[0]].get("xfade_frames", 0) if "xfade" in shots[g[0]] else 0 for g in groups]
    starts, acc = [], 0
    for g in groups:
        starts.append(acc)
        acc += sum(shots[k]["frames"] for k in g)
    for gi, g in enumerate(groups):
        head = t_in[gi] - t_in[gi] // 2
        tail = t_in[gi + 1] // 2 if gi + 1 < len(groups) else 0
        src = "".join(f"[v{k}]" for k in g)
        chain = f"{src}concat=n={len(g)}:v=1:a=0" if len(g) > 1 else f"{src}null"
        pad = ""
        if head or tail:
            pad = f",tpad=start_mode=clone:start={head}:stop_mode=clone:stop={tail}"
        parts.append(f"{chain}{pad},settb=1/{FPS},setpts=N[g{gi}]")
    cur = "[g0]"
    for gi in range(1, len(groups)):
        t = t_in[gi]
        out = f"[x{gi}]"
        if t:
            name = shots[groups[gi][0]]["xfade"]
            offset = (starts[gi] - (t - t // 2)) / FPS
            parts.append(f"{cur}[g{gi}]xfade=transition={name}:duration={t / FPS:.4f}:"
                         f"offset={offset:.4f}{out}")
        else:
            parts.append(f"{cur}[g{gi}]concat=n=2:v=1:a=0,settb=1/{FPS},setpts=N{out}")
        cur = out
    fonts = f":fontsdir={_quote_path(fonts_dir)}" if fonts_dir else ""
    parts.append(f"{cur}ass={ass_name}{fonts}[vout]")
    return ";".join(parts)


def _filtergraph(
    shots: list[dict], w: int, h: int, audio_index: int, ass_name: str,
    loudnorm: str = f"loudnorm={LOUDNORM}", audio: str | None = None,
    fonts_dir: Path | None = None,
) -> str:
    """Full graph: video_graph -> [vout]; audio (pre-loudnorm graph ending in [m], or the
    plain narration at audio_index) -> loudnorm -> [aout]."""
    pre = audio if audio is not None else f"[{audio_index}:a]anull[m]"
    return ";".join([video_graph(shots, w, h, ass_name, fonts_dir), pre,
                     f"[m]{loudnorm},aresample=48000[aout]"])


ENCODERS = [
    ["-c:v", "h264_videotoolbox", "-b:v", "8M", "-maxrate", "12M", "-bufsize", "16M",
     "-profile:v", "high", "-allow_sw", "1"],
    ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-profile:v", "high"],
]


def mark_clips(shots: list[dict], images: list[Path], durations: dict[int, float]) -> list[dict]:
    """Tag shots whose visual is a video clip with kind=clip and a source offset (seconds).
    Consecutive shots of one clip continue where the previous one stopped; past the clip's
    end they wrap (the input is looped)."""
    run_img, run_t = None, 0.0
    for shot in shots:
        img = shot["image"]
        if not is_video(images[img]):
            run_img = None
            continue
        if img != run_img:
            run_img, run_t = img, 0.0
        dur = shot["frames"] / FPS
        clip_len = durations.get(img, 0.0)
        shot["kind"] = "clip"
        shot["offset"] = round(run_t % clip_len, 3) if clip_len > 0 else 0.0
        run_t += dur
    return shots


CLIP_KINDS = ("clip", "parallax")  # shots fed from a video input (stock clip or parallax)
MOTION_MODES = ("parallax", "zoompan")


def motion_mode(ctx: StageContext) -> tuple[str, str]:
    """(mode, reason) for still shots, from STUDIO_MOTION = parallax | zoompan | auto (unset)."""
    req = os.environ.get("STUDIO_MOTION", "").strip().lower() or "auto"
    if req not in (*MOTION_MODES, "auto"):
        raise StageError(f"render: STUDIO_MOTION={req!r}; use parallax, zoompan or auto")
    if req == "zoompan":
        return "zoompan", "STUDIO_MOTION=zoompan"
    if ctx.engine("images") == "mock":
        return "zoompan", "placeholder images (no depth model)"
    if req == "auto" and ctx.engine("tts") in ("mock", "silence"):
        return "zoompan", "mock/silence narration (no depth model)"
    if not motion.available():
        return "zoompan", f"depth model {motion.REPO} not downloaded (make models STAGE=video)"
    return "parallax", "depth model available" if req == "auto" else "STUDIO_MOTION=parallax"


def resolved_shot_hints(job: VideoJob, images: list[Path]) -> list[str]:
    """Use the image manifest's framing, with script metadata as a legacy fallback."""
    beats = job.script.beats if job.script else []
    return [motion.shot_hint(image) or (beats[i].shot if i < len(beats) else "")
            for i, image in enumerate(images)]


def add_parallax(job: VideoJob, shots: list[dict], images: list[Path], w_dir: Path,
                 shot_hints: list[str] | None = None) -> None:
    """Render a parallax clip of exactly shot['frames'] for every still shot and tag it
    kind=parallax (src, offset 0, motion = preset). Raises on failure (caller falls back)."""
    presets = motion.preset_sequence(job.job_id, len(shots))
    hints = shot_hints if shot_hints is not None else resolved_shot_hints(job, images)
    out_dir = w_dir / "motion"
    depth_dir = w_dir / ".cache" / "depth"
    for k, shot in enumerate(shots):
        if shot.get("kind") in CLIP_KINDS:
            continue
        img = images[shot["image"]]
        hint = hints[shot["image"]]
        preset = motion.preset_for_shot(hint, presets[k])
        identity = hash_inputs(img, job.format, preset, hint, shot["frames"],
                               motion.MOTION_VERSION, os.environ.get("STUDIO_PARALLAX_AMP", ""))
        name = f"{identity[:24]}_{preset}_{shot['frames']}f_v{motion.MOTION_VERSION}.mp4"
        clip = out_dir / name
        if not clip.exists():
            motion.parallax_clip(img, clip, shot["frames"] / FPS, job.format, preset,
                                 cache_dir=depth_dir, shot=hint)
        shot.update(kind="parallax", src=str(clip), offset=0.0, motion=preset)


def drop_parallax(shots: list[dict], seed: str) -> None:
    """Undo add_parallax (fallback to zoompan) and restore the zoompan motion sequence."""
    for shot, m in zip(shots, motion_sequence(seed, len(shots)), strict=True):
        if shot.get("kind") == "parallax":
            for k in ("kind", "src", "offset"):
                shot.pop(k, None)
        if shot.get("kind") is None:
            shot["motion"] = m


def compute_content_hash(job: VideoJob) -> str:
    h = hashlib.sha256()
    with Path(job.video_path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    h.update((job.script.description if job.script else "").encode())
    return h.hexdigest()


def apply_content_hash(job: VideoJob, new_hash: str) -> VideoJob:
    """Set content_hash; an approval bound to a different hash is voided."""
    old = job.content_hash
    job.content_hash = new_hash
    (Path(job.workdir) / "content_hash.txt").write_text(new_hash + "\n")
    if job.approved_hash and job.approved_hash != new_hash:
        log(job, f"render: content changed ({(old or '')[:12]} -> {new_hash[:12]}); approval "
                 f"by {job.approved_by} voided")
        job.approved_hash = None
        job.approved_by = None
        job.status = JobStatus.RENDERED
    elif job.status in (JobStatus.PLANNED, JobStatus.FAILED):
        job.status = JobStatus.RENDERED
    return job


def fonts_dir() -> Path | None:
    d = assets_dir() / "fonts"
    has = d.is_dir() and any(p.suffix.lower() in (".ttf", ".otf", ".ttc") for p in d.iterdir())
    return d if has else None


def run(job: VideoJob, brand: BrandPack | None, ctx: StageContext) -> VideoJob:
    workdir = Path(job.workdir)
    audio = Path(job.audio_path) if job.audio_path else None
    ass = Path(job.captions_path) if job.captions_path else None
    images = [Path(p) for p in job.image_paths]
    timings_path = workdir / "timings.json"
    for need, label in ((audio, "narration"), (ass, "captions")):
        if need is None or not need.exists():
            raise StageError(f"render: missing {label}")
    if not images or not all(p.exists() for p in images):
        raise StageError("render: missing images")
    out = workdir / "video.mp4"
    mode, why = motion_mode(ctx)
    shot_hints = resolved_shot_hints(job, images) if mode == "parallax" else []
    music, sfx, fonts = pick_music(brand, job.job_id), pick_sfx(), fonts_dir()
    key = hash_inputs(STAGE, VERSION, job.format, job.job_id, audio, ass, images, timings_path,
                      mode, motion.MOTION_VERSION if mode == "parallax" else 0,
                      shot_hints, os.environ.get("STUDIO_PARALLAX_AMP", "")
                      if mode == "parallax" else None,
                      music, sfx, sorted(p for p in fonts.iterdir() if p.is_file())
                      if fonts else None)
    if is_cached(job, STAGE, key, [out], ctx):
        job.video_path = out
        log(job, "render: cached, skipped")
        return apply_content_hash(job, compute_content_hash(job))

    w, h = frame_size(job.format)
    duration = media_duration(audio)
    timings = json.loads(timings_path.read_text()) if timings_path.exists() else {}
    shots = plan_shots(timings, len(images), duration, seed=job.job_id)
    clip_durations = {i: media_duration(p) for i, p in enumerate(images) if is_video(p)}
    if clip_durations:
        mark_clips(shots, images, clip_durations)
    if mode == "parallax":
        try:
            add_parallax(job, shots, images, workdir, shot_hints)
        except (StageError, RuntimeError, OSError, ValueError) as exc:
            mode, why = "zoompan", f"parallax failed: {str(exc).splitlines()[0][:160]}"
            drop_parallax(shots, job.job_id)
        finally:
            motion.unload()
    log(job, f"render: motion={mode} ({why})")
    inputs: list[str] = []
    threads = _cpu_threads()
    for shot in shots:
        src = str(Path(shot.get("src") or images[shot["image"]]).resolve())
        if shot.get("kind") in CLIP_KINDS:
            # -an: the stock clip's own audio is never decoded or mapped (muted)
            inputs += ["-an", "-stream_loop", "-1", "-ss", f"{shot['offset']:.3f}",
                       "-t", f"{shot['frames'] / FPS + 0.5:.3f}", "-threads", threads, "-i", src]
        else:
            inputs += ["-threads", threads, "-i", src]
    sound = plan_audio(audio, duration, shots, brand, job.job_id, first_input=len(shots))
    # second loudnorm pass is measured on the final mix (voice chain + music + sfx)
    n_in = len(shots)
    shifted = re.sub(r"\[(\d+):a\]", lambda m: f"[{int(m.group(1)) - n_in}:a]", sound["graph"])
    mix_m = measure(sound["inputs"], shifted)
    inputs += sound["inputs"]
    graph = _filtergraph(shots, w, h, n_in, ass.name, _loudnorm_filter(mix_m),
                         audio=sound["graph"], fonts_dir=fonts)
    (workdir / "render_plan.json").write_text(json.dumps(shots, indent=1))
    summary = sound["summary"]
    if summary.get("music"):
        log(job, f"render: music {Path(summary['music']).name} "
                 f"({summary['music_gain_db']:+.1f} dB, ducked)")
    elif summary.get("music_skipped"):
        log(job, f"render: music skipped ({summary['music_skipped']})")
    else:
        log(job, f"render: no music for moods {list(brand_moods(brand))} (voice only)")

    tmp = workdir / "video.tmp.mp4"
    last_error: StageError | None = None
    for enc in ENCODERS:
        try:
            run_ffmpeg(
                [*inputs, "-filter_complex", graph, "-map", "[vout]", "-map", "[aout]",
                 *enc, "-threads", threads, "-pix_fmt", "yuv420p", "-r", str(FPS),
                 "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
                 "-t", f"{duration:.3f}", "-movflags", "+faststart", tmp.name],
                cwd=workdir,  # relative ASS path: no filtergraph escaping of the workdir
            )
            last_error = None
            n_x = sum("xfade" in s for s in shots)
            log(job, f"render: {enc[1]} {w}x{h} {len(shots)} shots, {n_x} transitions "
                     f"{duration:.1f}s")
            break
        except StageError as exc:
            last_error = exc
            log(job, f"render: {enc[1]} failed, trying fallback")
    if last_error is not None:
        raise last_error
    tmp.replace(out)
    shutil.rmtree(workdir / "motion", ignore_errors=True)  # intermediates; depth stays cached
    job.video_path = out
    write_cache(job, STAGE, key, [out], motion=mode, music=summary.get("music"),
                sfx=summary.get("sfx"))
    return apply_content_hash(job, compute_content_hash(job))
