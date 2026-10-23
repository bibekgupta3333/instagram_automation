"""Shot clips: FastWan T2V generation (guarded, one at a time, cached) or mock placeholders,
then conform each shot to the exact beat-aligned frame count for both output formats.

Raw clips: <out>/clips/raw/<name>/video.mp4 (FastWan writes raw.avi + video.mp4 + run.json).
Conformed: <out>/clips/<fmt>/shot-NN.mp4 at 30 fps, frame-exact, including the crossfade
overlap (see assemble.plan_edit). Upscaling is lanczos plus a mild unsharp (Real-ESRGAN is
removed); 16 fps sources are motion-interpolated to 30 fps at source size (cheap).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .episode import Shot, Timeline

REPO = Path(__file__).resolve().parents[2]
RUNTIME = REPO / "output" / "local-video-pilot" / "runtime" / "sd-cli"
GEN_W, GEN_H, GEN_FPS, MAX_FRAMES = 320, 576, 16, 49
FPS = 30
STYLE = ("gentle children's picture-book animation, soft pastel colours, rounded friendly "
         "shapes, calm slow camera, no text, no letters, no logos")
PALETTE = ("0xFFD6E0", "0xC9E8FF", "0xD8F5C8", "0xFFF0B8", "0xE6D9FF", "0xFFE0C2")


@dataclass(frozen=True)
class Format:
    name: str
    width: int
    height: int
    bitrate: str


VERTICAL = Format("9x16", 1080, 1920, "14M")
HORIZONTAL = Format("16x9", 1920, 1080, "14M")


def frames_for(seconds: float) -> int:
    """Smallest 4n+1 frame count covering `seconds` at 16 fps, capped at 49 (~3.06 s)."""
    need = int(-(-seconds * GEN_FPS // 1)) + 1
    frames = 4 * (-(-(need - 1) // 4)) + 1
    return max(5, min(MAX_FRAMES, frames))


def full_prompt(shot: Shot) -> str:
    p = shot.prompt.rstrip(" .")
    return p if "no text" in p.lower() else f"{p}, {STYLE}"


def clip_key(shot: Shot, frames: int) -> str:
    raw = json.dumps([full_prompt(shot), shot.seed, frames, GEN_W, GEN_H, "fastwan-t2v", 1])
    return hashlib.sha256(raw.encode()).hexdigest()[:10]


def raw_dir(out: Path, shot: Shot, frames: int, mock: bool) -> Path:
    tag = "mock" if mock else "fastwan"
    return out / "clips" / "raw" / f"shot-{shot.index:02d}-{tag}-{clip_key(shot, frames)}"


def generate_command(shot: Shot, frames: int, dest: Path, python: str = sys.executable) -> list:
    return [python, str(REPO / "scripts" / "run_guarded.py"), "--",
            python, str(REPO / "scripts" / "generate_wan_video.py"),
            "--preset", "fastwan-t2v", "--runtime", str(RUNTIME), "--output-dir", str(dest),
            "--width", str(GEN_W), "--height", str(GEN_H), "--frames", str(frames),
            "--seed", str(shot.seed), "--prompt", full_prompt(shot)]


def generate_fastwan(shot: Shot, out: Path, dry_run: bool = False) -> dict:
    """One guarded FastWan generation; skip if the cached clip exists. Never parallel."""
    frames = frames_for(shot.dur)
    final = raw_dir(out, shot, frames, mock=False)
    info = {"shot": shot.index, "engine": "fastwan-t2v", "frames": frames, "seed": shot.seed,
            "dir": str(final), "prompt": full_prompt(shot)}
    if (final / "video.mp4").is_file():
        return {**info, "cached": True}
    tmp = final.with_name(final.name + f".tmp-{int(time.time())}")
    cmd = generate_command(shot, frames, tmp)
    if dry_run:
        return {**info, "cached": False, "command": cmd}
    final.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    result = subprocess.run(cmd, cwd=REPO, check=False)
    seconds = round(time.monotonic() - start, 1)
    if result.returncode or not (tmp / "video.mp4").is_file():
        raise RuntimeError(f"FastWan shot {shot.index} failed (exit {result.returncode}); "
                           f"see {tmp / 'generation.log'}")
    tmp.rename(final)
    return {**info, "cached": False, "seconds": seconds}


def make_mock(shot: Shot, out: Path) -> dict:
    """Coloured placeholder clip at the FastWan size/fps, with a gentle moving gradient."""
    frames = frames_for(shot.dur)
    final = raw_dir(out, shot, frames, mock=True)
    video = final / "video.mp4"
    info = {"shot": shot.index, "engine": "mock", "frames": frames, "dir": str(final),
            "do_not_publish": True}
    if video.is_file():
        return {**info, "cached": True}
    final.mkdir(parents=True, exist_ok=True)
    c0 = PALETTE[shot.index % len(PALETTE)]
    c1 = PALETTE[(shot.index + 2) % len(PALETTE)]
    src = (f"gradients=s={GEN_W}x{GEN_H}:r={GEN_FPS}:c0={c0}:c1={c1}:speed=0.03:"
           f"duration={frames / GEN_FPS}:seed={shot.index + 1}")
    _ffmpeg(["-f", "lavfi", "-i", src, "-frames:v", str(frames), "-pix_fmt", "yuv420p",
             "-c:v", "h264_videotoolbox", "-b:v", "4M", str(video)])
    return {**info, "cached": False}


def conform_filter(src_frames: int, need_frames: int, fmt: Format) -> tuple[str, dict]:
    """Filtergraph [0:v] -> [v]: stretch/boomerang to cover need_frames, interpolate to
    30 fps, lanczos upscale (+ mild sharpen) to the format. 16:9 gets a blurred fill."""
    src_sec = src_frames / GEN_FPS
    need_sec = need_frames / FPS
    chain, meta = "[0:v]null", {"src_seconds": round(src_sec, 3), "need_seconds": round(need_sec, 3)}
    base = src_sec
    if need_sec > src_sec * 1.6:  # ping-pong first, then slow down the rest
        chain = "[0:v]split[f][r];[r]reverse,trim=start_frame=1,setpts=PTS-STARTPTS[rv];" \
                "[f][rv]concat=n=2:v=1:a=0"
        base = (2 * src_frames - 1) / GEN_FPS
        meta["boomerang"] = True
    factor = max(1.0, need_sec / base)
    meta["slowdown"] = round(factor, 3)
    head = (f"{chain},setpts={factor:.6f}*(PTS-STARTPTS),"
            f"minterpolate=fps={FPS}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1,"
            f"tpad=stop_mode=clone:stop_duration=1")
    sharpen = "unsharp=5:5:0.55:3:3:0.0"
    w, h = fmt.width, fmt.height
    if h >= w:
        graph = (f"{head},scale={w}:{h}:flags=lanczos:force_original_aspect_ratio=increase,"
                 f"crop={w}:{h},{sharpen},setsar=1,format=yuv420p[v]")
    else:  # vertical source in a 16:9 frame: sharp centre over a soft blurred fill
        fg_w = round(h * GEN_W / GEN_H / 2) * 2
        graph = (f"{head},split[a][b];"
                 f"[a]scale={w // 4}:{h // 4}:force_original_aspect_ratio=increase,"
                 f"crop={w // 4}:{h // 4},gblur=sigma=6,eq=brightness=0.04:saturation=0.9,"
                 f"scale={w}:{h}:flags=bicubic[bg];"
                 f"[b]scale={fg_w}:{h}:flags=lanczos,{sharpen}[fg];"
                 f"[bg][fg]overlay=(W-w)/2:0,setsar=1,format=yuv420p[v]")
    return graph, meta


def conform(raw_video: Path, src_frames: int, need_frames: int, fmt: Format,
            dest: Path) -> dict:
    graph, meta = conform_filter(src_frames, need_frames, fmt)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp.mp4")
    _ffmpeg(["-i", str(raw_video), "-filter_complex", graph, "-map", "[v]",
             "-frames:v", str(need_frames), "-r", str(FPS), "-an",
             "-c:v", "h264_videotoolbox", "-b:v", fmt.bitrate, "-pix_fmt", "yuv420p",
             str(tmp)])
    shutil.move(tmp, dest)
    return {**meta, "frames": need_frames, "path": str(dest)}


def probe_frames(video: Path) -> int:
    out = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
                          "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0",
                          str(video)], capture_output=True, text=True, check=True).stdout
    return int(out.strip().split(",")[0])


def _ffmpeg(args: list[str]) -> None:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-threads", "3", *args]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(f"ffmpeg failed: {result.stderr.strip()[-800:]}")


def all_raw(tl: Timeline, out: Path, mock: bool, dry_run: bool = False,
            only: set[int] | None = None) -> list[dict]:
    """Generate (or reuse) every raw shot clip, strictly one after another."""
    infos = []
    for shot in tl.shots:
        if only is not None and shot.index not in only:
            continue
        if not mock:
            infos.append(generate_fastwan(shot, out, dry_run))
        elif dry_run:
            infos.append({"shot": shot.index, "engine": "mock", "frames": frames_for(shot.dur)})
        else:
            infos.append(make_mock(shot, out))
    return infos
