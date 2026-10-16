"""Stage 6: render with ffmpeg. Ken Burns (zoompan) per shot with the motion type rotated and
seeded per job so videos don't look templated (doc 04 §5.4 variation), shots timed to the
narration and re-cut every <=3.5 s (doc 06 §3: a change every 2-4 s), then the narration
(loudnorm I=-14) and burned-in ASS captions. h264_videotoolbox, falling back to libx264.

Stock clips (images.py `pexels` engine returns .mp4) mix with stills: a clip shot is read from
an offset that advances across consecutive shots of the same clip (looped if too short),
scaled and centre-cropped to fill the frame, and its own audio is never mapped (muted).

Writes video.mp4 and content_hash.txt (sha256 of the video bytes + description). Approval
binds to that hash, so any re-render that changes it voids the approval.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
import subprocess
from pathlib import Path
from typing import Any

from studio.contracts import BrandPack, JobStatus, VideoJob
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
VERSION = 2  # 2: two-pass loudnorm (single pass undershot -14 LUFS on short speech)
FPS = 30
MAX_SHOT_S = 3.5
OVERSCAN = 1.25  # pre-scale before zoompan to limit jitter and leave room to pan

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


def frame_size(fmt: str) -> tuple[int, int]:
    return (1080, 1920) if fmt == "short" else (1920, 1080)


def motion_sequence(seed: str, n: int) -> list[str]:
    """Shuffled per job, cycled; adjacent shots never repeat a motion."""
    order = list(MOTIONS)
    rng = random.Random(seed)
    rng.shuffle(order)
    start = rng.randrange(len(order))
    return [order[(start + i) % len(order)] for i in range(n)]


def plan_shots(timings: dict[str, Any], n_images: int, duration: float, seed: str) -> list[dict]:
    """Blocks of narration per image (hook joins beat 0, CTA joins the last beat), each split
    into shots of <= MAX_SHOT_S, with frame-exact boundaries summing to the audio length."""
    blocks: list[dict[str, Any]] = []
    for seg in timings.get("segments", []):
        img = min(int(seg.get("beat", 0)), n_images - 1)
        if blocks and blocks[-1]["image"] == img:
            continue
        blocks.append({"image": img, "start": float(seg["start"])})
    if not blocks:
        blocks = [{"image": i, "start": duration * i / n_images} for i in range(n_images)]
    blocks[0]["start"] = 0.0
    total_frames = max(1, round(duration * FPS))
    cuts: list[tuple[int, float]] = []  # (image, start time)
    for i, b in enumerate(blocks):
        end = blocks[i + 1]["start"] if i + 1 < len(blocks) else duration
        parts = max(1, math.ceil((end - b["start"]) / MAX_SHOT_S - 1e-6))
        step = (end - b["start"]) / parts
        cuts += [(b["image"], b["start"] + k * step) for k in range(parts)]
    motions = motion_sequence(seed, len(cuts))
    shots = []
    for k, (img, start) in enumerate(cuts):
        f0 = round(start * FPS)
        f1 = round(cuts[k + 1][1] * FPS) if k + 1 < len(cuts) else total_frames
        if f1 > f0:
            shots.append({"image": img, "frames": f1 - f0, "motion": motions[k]})
    return shots


LOUDNORM = "I=-14:TP=-1.5:LRA=11"


def _loudnorm(audio: Path) -> str:
    """Two-pass loudnorm: measure the narration, then apply linear gain to hit -14 LUFS.
    Falls back to single-pass (dynamic) if the measurement fails."""
    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-i", str(audio),
             "-af", f"loudnorm={LOUDNORM}:print_format=json", "-f", "null", "-"],
            capture_output=True, text=True, timeout=120, check=True,
        ).stderr
        m = json.loads(re.findall(r"\{[^{}]*\}", out)[-1])
        return (
            f"loudnorm={LOUDNORM}:measured_I={m['input_i']}:measured_TP={m['input_tp']}"
            f":measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}"
            f":offset={m['target_offset']}:linear=true"
        )
    except (subprocess.SubprocessError, OSError, IndexError, KeyError, ValueError):
        return f"loudnorm={LOUDNORM}"


def _filtergraph(
    shots: list[dict], w: int, h: int, audio_index: int, ass_name: str,
    loudnorm: str = f"loudnorm={LOUDNORM}",
) -> str:
    sw, sh = int(w * OVERSCAN) // 2 * 2, int(h * OVERSCAN) // 2 * 2
    parts = []
    for k, shot in enumerate(shots):
        if shot.get("kind") == "clip":
            # motion is ignored: the clip moves on its own. fps first so trim is frame-exact.
            parts.append(
                f"[{k}:v]fps={FPS},scale={w}:{h}:force_original_aspect_ratio=increase,"
                f"crop={w}:{h},setsar=1,format=yuv420p,trim=end_frame={shot['frames']},"
                f"setpts=PTS-STARTPTS[v{k}]"
            )
            continue
        z, x, y = (e.replace("D", str(max(shot["frames"] - 1, 1))) for e in MOTIONS[shot["motion"]])
        parts.append(
            f"[{k}:v]scale={sw}:{sh}:force_original_aspect_ratio=increase,crop={sw}:{sh},"
            f"zoompan=z='{z}':x='{x}':y='{y}':d={shot['frames']}:s={w}x{h}:fps={FPS},"
            f"setsar=1,format=yuv420p[v{k}]"
        )
    parts.append("".join(f"[v{k}]" for k in range(len(shots))) + f"concat=n={len(shots)}:v=1:a=0[vc]")
    parts.append(f"[vc]ass={ass_name}[vout]")
    parts.append(f"[{audio_index}:a]{loudnorm},aresample=48000[aout]")
    return ";".join(parts)


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
        shot["offset"] = round(run_t % clip_len, 3) if clip_len > dur else 0.0
        run_t += dur
    return shots


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


def run(job: VideoJob, brand: BrandPack, ctx: StageContext) -> VideoJob:
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
    key = hash_inputs(STAGE, VERSION, job.format, job.job_id, audio, ass, images, timings_path)
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
    inputs: list[str] = []
    for shot in shots:
        src = str(images[shot["image"]].resolve())
        if shot.get("kind") == "clip":
            # -an: the stock clip's own audio is never decoded or mapped (muted)
            inputs += ["-an", "-stream_loop", "-1", "-ss", f"{shot['offset']:.3f}",
                       "-t", f"{shot['frames'] / FPS + 0.5:.3f}", "-i", src]
        else:
            inputs += ["-i", src]
    inputs += ["-i", str(audio.resolve())]
    graph = _filtergraph(shots, w, h, len(shots), ass.name, _loudnorm(audio))
    (workdir / "render_plan.json").write_text(json.dumps(shots, indent=1))

    tmp = workdir / "video.tmp.mp4"
    last_error: StageError | None = None
    for enc in ENCODERS:
        try:
            run_ffmpeg(
                [*inputs, "-filter_complex", graph, "-map", "[vout]", "-map", "[aout]",
                 *enc, "-pix_fmt", "yuv420p", "-r", str(FPS),
                 "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
                 "-t", f"{duration:.3f}", "-movflags", "+faststart", tmp.name],
                cwd=workdir,  # relative ASS path: no filtergraph escaping of the workdir
            )
            last_error = None
            log(job, f"render: {enc[1]} {w}x{h} {len(shots)} shots {duration:.1f}s")
            break
        except StageError as exc:
            last_error = exc
            log(job, f"render: {enc[1]} failed, trying fallback")
    if last_error is not None:
        raise last_error
    tmp.replace(out)
    job.video_path = out
    write_cache(job, STAGE, key, [out])
    return apply_content_hash(job, compute_content_hash(job))
