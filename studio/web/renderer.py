"""Original, model-free Pond Studio illustrations and bounded-memory MP4 rendering.

The caller supplies validated snapshots and trusted asset paths and owns supervision.
No approval, network, environment-file loading or model generation happens here.
"""
from __future__ import annotations

import io
import json
import math
import os
import platform
import re
import shutil
import struct
import subprocess
import wave
from collections.abc import Callable, Iterator
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

FPS = 30
Progress = Callable[[int, str], None]


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _character(character: dict, t: float = 0.0) -> Image.Image:
    """Draw a new friendly animal from geometric shapes, including animated limbs."""
    species = character.get("species", "otter")
    if species not in {"otter", "rabbit", "bear", "frog"}:
        raise ValueError("Unsupported character species")
    color = character.get("color", "#B88054")
    if not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        raise ValueError("Character color must be #RRGGBB")
    accessory = character.get("accessory", "none")
    if accessory not in {"none", "bow", "scarf"}:
        raise ValueError("Unsupported accessory")
    image = Image.new("RGBA", (512, 512))
    d = ImageDraw.Draw(image)
    dark, cream = "#383341", "#FFF0CE"
    # Otters have a broad tail; the other animals have distinct ears/eye stalks.
    if species == "otter":
        d.ellipse((290, 340, 450, 438), fill=color, outline=dark, width=5)
    for x in (176, 300):
        d.ellipse((x - 32, 403, x + 48, 452), fill=color, outline=dark, width=5)
    for side in (-1, 1):
        x = 256 + side * 82
        y = 314 + int(23 * math.sin(t * 5 + side))
        d.line((256 + side * 67, 316, x + side * 48, y), fill=dark, width=38)
        d.line((256 + side * 67, 316, x + side * 48, y), fill=color, width=28)
        d.ellipse((x + side * 48 - 19, y - 19, x + side * 48 + 19, y + 19), fill=color)
    d.ellipse((153, 260, 359, 439), fill=color, outline=dark, width=5)
    d.ellipse((196, 306, 316, 415), fill=cream)
    if species == "rabbit":
        for x in (184, 283):
            d.ellipse((x, 28, x + 55, 192), fill=color, outline=dark, width=5)
            d.ellipse((x + 17, 48, x + 38, 158), fill="#F3B3AE")
    elif species != "frog":
        for x in (153, 300):
            d.ellipse((x, 123, x + 64, 192), fill=color, outline=dark, width=5)
            d.ellipse((x + 16, 138, x + 47, 170), fill=cream)
    d.ellipse((137, 137, 375, 325), fill=color, outline=dark, width=5)
    if species == "frog":
        for x in (158, 282):
            d.ellipse((x, 109, x + 74, 199), fill=color, outline=dark, width=5)
    else:
        d.ellipse((183, 224, 329, 303), fill=cream)
    blink = t % 3.1 > 2.92
    eye_y = 162 if species == "frog" else 210
    for x in (206, 306):
        if blink:
            d.arc((x - 13, eye_y - 8, x + 13, eye_y + 8), 0, 180, fill=dark, width=5)
        else:
            d.ellipse((x - 13, eye_y - 17, x + 13, eye_y + 17), fill=dark)
            d.ellipse((x - 6, eye_y - 12, x + 1, eye_y - 5), fill="white")
    if species != "frog":
        d.ellipse((245, 242, 267, 256), fill=dark)
    mouth = 6 + int(4 * (1 + math.sin(t * 6)))
    d.arc((229, 252, 283, 272 + mouth), 0, 180, fill=dark, width=4)
    for x in (177, 323):
        d.ellipse((x - 12, 242, x + 12, 255), fill="#EFACA6")
    if accessory == "scarf":
        d.rounded_rectangle((175, 291, 337, 315), radius=9, fill="#437F99")
        d.polygon([(286, 307), (320, 307), (334, 366), (301, 359)], fill="#437F99")
    elif accessory == "bow":
        d.polygon([(284, 143), (269, 111), (314, 123), (344, 105), (341, 150)], fill="#C66B96")
        d.ellipse((302, 123, 322, 144), fill="#F7C269")
    return image


def character_preview(character: dict) -> bytes:
    image = Image.new("RGBA", (512, 512), "#FFF8E9")
    image.alpha_composite(_character(character))
    stream = io.BytesIO()
    image.convert("RGB").save(stream, format="PNG")
    return stream.getvalue()


def _background(size: tuple[int, int], theme: str) -> Image.Image:
    w, h = size
    night = theme in {"bedtime", "night"}
    image = Image.new("RGB", size, "#252E59" if night else "#BDE7EF")
    d = ImageDraw.Draw(image)
    d.ellipse((int(-w * .3), int(h * .35), int(w * 1.4), int(h * 1.4)),
              fill="#3E6561" if night else "#A5CF91")
    if theme == "pond":
        d.ellipse((int(-w * .1), int(h * .59), int(w * 1.1), int(h * 1.15)), fill="#74C5D0")
        for i in range(7):
            x, y = int(w * (.06 + i * .15)), int(h * (.74 + .04 * (i % 3)))
            d.arc((x, y, x + w // 10, y + h // 30), 0, 180, fill="#C2EDF0", width=2)
    if night:
        d.ellipse((int(w * .76), int(h * .07), int(w * .9), int(h * .07 + w * .14)),
                  fill="#FFE8AD")
        for i in range(23):
            x, y = (i * 79 + 21) % w, (i * 31 + 13) % max(1, int(h * .4))
            d.ellipse((x, y, x + 3, y + 3), fill="#FFF1C4")
    elif theme != "studio":
        for i in range(12):
            x, y = (i * 97) % w, int(h * .63) + (i * 17) % max(1, h // 9)
            d.line((x, y, x, y + 14), fill="#5B8C68", width=2)
            d.ellipse((x - 4, y - 4, x + 4, y + 4), fill="#FFE4A3")
    return image


def _caption(image: Image.Image, text: str) -> None:
    if not text:
        return
    w, h = image.size
    font = ImageFont.load_default(size=max(14, round(min(w, h) * .052)))
    d = ImageDraw.Draw(image)
    # Pixel-based wrapping also handles long unbroken words and Unicode.
    lines, line = [], ""
    for char in text[:600].replace("\n", " "):
        if d.textlength(line + char, font=font) > w * .86 and line:
            lines.append(line.strip())
            line = ""
        line += char
    if line:
        lines.append(line.strip())
    lines = lines[:4]
    spacing = max(3, h // 100)
    text = "\n".join(lines)
    box = d.multiline_textbbox((0, 0), text, font=font, spacing=spacing, align="center")
    tw, th = box[2] - box[0], box[3] - box[1]
    x, y = (w - tw) // 2, h - th - int(h * .055)
    d.rounded_rectangle((x - 12, y - 8, x + tw + 12, y + th + 12),
                        radius=10, fill="#FFF8E9")
    d.multiline_text((x, y - box[1]), text, font=font, fill="#30394D",
                     spacing=spacing, align="center")


def _asset(asset_id: str | None, paths: dict, suffixes: set[str]) -> Path | None:
    if not asset_id:
        return None
    if asset_id not in paths:
        raise ValueError("Selected asset is unavailable")
    path = Path(paths[asset_id])
    if not path.is_absolute() or path.suffix.lower() not in suffixes or not path.is_file():
        raise ValueError("Selected asset must be an existing local file of the supported type")
    return path


def frame_generator(project: dict, size: tuple[int, int], assets: dict) -> Iterator[Image.Image]:
    """Retain only one scene background and that scene's small raster cutouts."""
    w, h = size
    characters = {c["id"]: c for c in project["characters"]}
    for scene in project["scenes"]:
        background = _background(size, scene.get("background", "pond"))
        cast = [characters[cid] for cid in scene.get("character_ids", [])]
        sprites = {}
        for c in cast:
            path = _asset(c.get("asset_id"), assets, {".png", ".jpg", ".jpeg", ".webp"})
            if path:
                with Image.open(path) as source:
                    if source.width * source.height > 25_000_000:
                        raise ValueError("Raster asset too large")
                    sprites[c["id"]] = ImageOps.contain(source.convert("RGBA"), (512, 512))
        for index in range(round(scene["duration"] * FPS)):
            t = index / FPS
            frame = background.copy().convert("RGBA")
            for slot, c in enumerate(cast):
                phase = t * 3 + slot * 1.7
                motion = scene.get("motion", "sway")
                sprite = sprites[c["id"]] if c["id"] in sprites else _character(c, t + slot * .31)
                side = int(min(w / max(1, len(cast)) * 1.25, h * .69, w * .75))
                if scene.get("camera") == "close":
                    side = int(side * 1.15)
                sprite = sprite.resize((side, side), Image.Resampling.BICUBIC)
                angle = math.sin(phase) * (5 if motion == "sway" else 2)
                sprite = sprite.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True)
                x = int(w * (slot + 1) / (len(cast) + 1) - sprite.width / 2)
                dy = -abs(math.sin(phase)) * h * .055 if motion == "bounce" else math.sin(phase) * h * .025
                y = int(h * .67 - sprite.height * .78 + dy)
                frame.alpha_composite(sprite, (x, y))
            frame = frame.convert("RGB")
            _caption(frame, scene.get("caption") or scene.get("narration", ""))
            yield frame


def _guide_audio(path: Path, seconds: float) -> None:
    """Stream an original instrumental sketch (no synthetic or claimed real singing)."""
    rate = 24000
    notes = (60, 64, 67, 62, 65, 69, 67, 64, 62, 65, 60, 67, 64, 62, 59, 60)
    total = round(seconds * rate)
    with wave.open(str(path), "wb") as stream:
        stream.setparams((1, 2, rate, 0, "NONE", "not compressed"))
        for start in range(0, total, 4096):
            chunk = bytearray()
            for sample in range(start, min(start + 4096, total)):
                t = sample / rate
                beat, local = int(t / .4), t % .4
                freq = 440 * 2 ** ((notes[beat % len(notes)] - 69) / 12)
                envelope = min(1, local / .015) * math.exp(-local * 6)
                melody = math.sin(math.tau * freq * t) + .22 * math.sin(math.tau * freq * 2 * t)
                bass = .23 * math.sin(math.tau * (130.8128 if beat % 8 < 4 else 174.6141) * t)
                fade = min(1, t / .1, max(0, seconds - t) / .3)
                chunk.extend(struct.pack("<h", int(6500 * (melody * envelope + bass) * fade)))
            stream.writeframesraw(chunk)


def stop_process(process: subprocess.Popen) -> None:
    """Stop only our child; its process group belongs to the outer supervisor."""
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


class EncoderError(RuntimeError):
    pass


def _encode(project: dict, request: dict, out: Path, size: tuple[int, int],
            audio: Path, seconds: float, encoder: str, progress: Progress) -> None:
    w, h = size
    output = out / "video.encoding.mp4"
    volume = float(project.get("audio", {}).get("volume", .7))
    filters = (f"apad,atrim=duration={seconds},asetpts=PTS-STARTPTS,"
               f"afade=t=in:d=0.1,afade=t=out:st={max(0, seconds-.3)}:d=0.3,"
               f"loudnorm=I=-16:TP=-1.5:LRA=11,volume={volume}")
    command = [shutil.which("ffmpeg") or "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-threads", "3", "-filter_threads", "3", "-filter_complex_threads", "3",
               "-f", "rawvideo", "-pixel_format", "rgb24", "-video_size", f"{w}x{h}",
               "-framerate", str(FPS), "-i", "pipe:0", "-threads", "3", "-i", str(audio),
               "-map", "0:v:0", "-map", "1:a:0", "-t", str(seconds), "-c:v", encoder,
               "-threads", "3", "-pix_fmt", "yuv420p"]
    command += (["-preset", "veryfast", "-crf", "21"] if encoder == "libx264"
                else ["-b:v", "5M", "-allow_sw", "0"])
    command += ["-af", filters, "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(output)]
    log_path = out / "encoder.log"
    frames = frame_generator(project, size, request.get("asset_paths", {}))
    with log_path.open("wb") as log:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                   stderr=log)  # inherit supervisor's process group
        try:
            for index, frame in enumerate(frames):
                if index == 0:
                    frame.save(out / "poster.png")
                process.stdin.write(frame.tobytes())
                if index % FPS == 0:
                    progress(10 + int(80 * index / (seconds * FPS)), f"Animating with {encoder}")
            process.stdin.close()
            if process.wait(timeout=120) != 0:
                raise EncoderError(f"{encoder} failed; see encoder.log")
        except BrokenPipeError as exc:
            raise EncoderError(f"{encoder} stopped accepting frames; see encoder.log") from exc
        finally:
            frames.close()
            stop_process(process)
            if not process.stdin.closed:
                try:
                    process.stdin.close()
                except BrokenPipeError:
                    pass
    output.replace(out / "video.mp4")


def _timestamp(seconds: float) -> str:
    ms = round(seconds * 1000)
    return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"


def validate_project(project: dict) -> float:
    scenes, characters = project.get("scenes", []), project.get("characters", [])
    if not 1 <= len(scenes) <= 24 or len(characters) > 8:
        raise ValueError("Use 1–24 scenes and at most 8 characters")
    identifiers = set()
    for c in characters:
        cid = c.get("id")
        if not isinstance(cid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", cid):
            raise ValueError("Invalid character ID")
        if cid in identifiers:
            raise ValueError("Duplicate character ID")
        identifiers.add(cid)
        _character(c)  # validate species, colour and accessory before spawning ffmpeg
    seconds, scene_ids = 0.0, set()
    for scene in scenes:
        sid = scene.get("id")
        if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", sid):
            raise ValueError("Invalid scene ID")
        if sid in scene_ids:
            raise ValueError("Duplicate scene ID")
        scene_ids.add(sid)
        duration = scene.get("duration")
        if isinstance(duration, bool) or not isinstance(duration, (int, float)):
            raise TypeError("Invalid scene duration")
        if not math.isfinite(duration) or not 1 <= duration <= 15:
            raise ValueError("Scene duration must be 1–15 seconds")
        seconds += round(duration * FPS) / FPS
        cast = scene.get("character_ids", [])
        if len(cast) > 8 or len(set(cast)) != len(cast) or any(c not in identifiers for c in cast):
            raise ValueError("Invalid scene characters")
        for field in ("title", "caption", "narration"):
            if not isinstance(scene.get(field, ""), str) or len(scene.get(field, "")) > 4000:
                raise ValueError("Scene strings must be at most 4000 characters")
        if scene.get("background", "pond") not in {"pond", "meadow", "bedtime", "night", "studio"}:
            raise ValueError("Unsupported background")
        if scene.get("motion", "sway") not in {"bounce", "sway", "float"}:
            raise ValueError("Unsupported motion")
        if scene.get("camera", "wide") not in {"wide", "close"}:
            raise ValueError("Unsupported camera")
    if seconds > 120:
        raise ValueError("Project must be at most 120 seconds")
    volume = project.get("audio", {}).get("volume", .7)
    if isinstance(volume, bool) or not isinstance(volume, (int, float)) or not 0 <= volume <= 1:
        raise ValueError("Audio volume must be 0–1")
    return seconds


def render_project(project: dict, request: dict, out: Path,
                   progress: Progress = lambda value, message: None) -> dict:
    seconds = validate_project(project)
    orientation = request.get("format", project.get("format", "landscape"))
    resolution = request.get("resolution", project.get("resolution", "draft"))
    if orientation not in {"landscape", "portrait"} or resolution not in {"draft", "final"}:
        raise ValueError("Unsupported format or resolution")
    size = (640, 360) if resolution == "draft" else (1920, 1080)
    if orientation == "portrait":
        size = size[::-1]
    out.mkdir(parents=True, exist_ok=True)
    audio_config = project.get("audio", {})
    mode = audio_config.get("mode", "guide")
    if mode not in {"guide", "uploaded"}:
        raise ValueError("Unsupported audio mode")
    guide = mode != "uploaded"
    if guide:
        audio = out / "guide.wav"
        _guide_audio(audio, seconds)
    else:
        audio = _asset(audio_config.get("asset_id"), request.get("asset_paths", {}), {".wav", ".mp3"})
        if audio is None:
            raise ValueError("Uploaded audio mode requires an audio asset")
    progress(10, "Audio prepared")
    encoder = "h264_videotoolbox" if platform.system() == "Darwin" else "libx264"
    try:
        _encode(project, request, out, size, audio, seconds, encoder, progress)
    except EncoderError:
        if encoder != "h264_videotoolbox":
            raise
        progress(10, "VideoToolbox failed; restarting frames with libx264")
        _encode(project, request, out, size, audio, seconds, "libx264", progress)
        encoder = "libx264"
    start, captions = 0.0, []
    for index, scene in enumerate(project["scenes"], 1):
        end = start + round(scene["duration"] * FPS) / FPS
        text = (scene.get("caption") or scene.get("narration", "")).replace("\r", "")
        captions.append(f"{index}\n{_timestamp(start)} --> {_timestamp(end)}\n{text}\n")
        start = end
    (out / "captions.srt").write_text("\n".join(captions), encoding="utf-8")
    review = project.get("review", {})
    ready = not guide and all(review.get(k) is True for k in
                             ("originality", "rights", "child_safety", "lyrics", "visuals"))
    result = {"kind": "render", "guide_audio": guide, "seconds": seconds, "width": size[0],
              "height": size[1], "fps": FPS, "encoder": encoder, "made_for_kids": True,
              "ai_disclosure": True, "approved": False, "published": False,
              "review_ready": ready, "draft": not ready, "resolution": resolution,
              "format": orientation, "project_revision": project.get("revision"),
              "audio_note": "Original procedural instrumental guide; no vocals" if guide
              else "User-supplied audio; rights and suitability require human review"}
    atomic_json(out / "metadata.json", result)
    (out / "description.txt").write_text(
        "Original Pond Studio animation. Made for kids. AI-assisted content.\n" + result["audio_note"],
        encoding="utf-8")
    (out / "checklist.txt").write_text(
        "Human review required: originality, audio rights, child safety, lyrics and visuals.\n"
        "No publishing or human approval is performed by this tool.\n", encoding="utf-8")
    progress(98, "Media and review metadata written")
    return result
