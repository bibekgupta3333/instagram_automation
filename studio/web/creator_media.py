"""Doc 32 media worker: honest CPU animatics and opt-in managed Wan motion.

The parent owns the resource lock, SQLite lifetime budget reservation, and process
group. Never call this module to bypass that supervisor. No model loading occurs.
API/schema references (verified 2026-10-25):
https://replicate.com/wan-video/wan-2.2-t2v-fast/api/schema
https://replicate.com/docs/reference/http
"""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import math
import os
import re
import shutil
import signal
import socket
import ssl
import subprocess
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from PIL import Image, ImageDraw, ImageFont

from .creator_models import Creation, CreationRender
from .renderer import atomic_json, stop_process

VERSION = 1
# Closed server catalogue; no model, URL, LoRA or arbitrary provider from user input.
MODEL = "wan-video/wan-2.2-t2v-fast"  # Wan 2.2 A14B, not the local 1.3B model.
API = "https://api.replicate.com/v1"
SIZE = (1080, 1920)
PREVIEW_SIZE = (360, 640)
COST_PER_SHOT = 0.25  # Conservative estimate, NOT an invoice or hard billing guarantee.
MIN_FREE = 10_000_000_000
MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024  # Total per job, including retries.
MAX_API_BYTES = 2 * 1024 * 1024
POLL_SECONDS = 600
STYLE_PROMPTS = {
    "watercolor-anime": "Original hand-painted watercolor fantasy animation, soft pigments",
    "anime": "Original anime animation, expressive motion, clean linework",
    "cartoon": "Original cartoon animation, expressive original characters",
    "live-action": "Photorealistic live-action aesthetic, natural movement and lighting",
    "cinematic": "Cinematic composition, motivated camera movement, detailed lighting",
    "motion-graphics": "Abstract motion graphics, moving geometric shapes, visual rhythm",
}
COST_NOTE = "Conservative fixed estimate; not a strict billing guarantee. Invoice review required."


class MediaError(RuntimeError):
    """Sanitized media failure safe for worker logs."""


class _Stopped(BaseException):
    pass


def _tts_engine() -> tuple[str, str] | None:
    if os.uname().sysname == "Darwin" and Path("/usr/bin/say").is_file():
        return ("/usr/bin/say", "Samantha")
    path = shutil.which("espeak-ng")
    return (path, "en") if path else None


def cloud_status() -> dict:
    """Configuration only: no network, secret-file loading, warmup or inference."""
    enabled = os.environ.get("STUDIO_CREATOR_CLOUD_ENABLED") == "1"
    token = bool(os.environ.get("REPLICATE_API_TOKEN", "").strip())
    try:
        budget = float(os.environ.get("STUDIO_CREATOR_CLOUD_BUDGET_USD", "0"))
        if not math.isfinite(budget) or budget <= 0:
            budget = 0.0
    except ValueError:
        budget = 0.0
    ffmpeg = bool(shutil.which("ffmpeg")) and bool(shutil.which("ffprobe"))
    reason = ("Cloud billing is disabled by the administrator" if not enabled else
              "Replicate token is not configured" if not token else
              "Positive administrator lifetime budget is required" if not budget else
              "FFmpeg and FFprobe are required" if not ffmpeg else
              "Configured; actual inference and output quality are unverified")
    return {"enabled": enabled, "available": enabled and token and budget > 0 and ffmpeg,
            "reason": reason, "provider": "replicate", "model": MODEL,
            "cost_per_second_usd": COST_PER_SHOT / 5,
            "cost_per_shot_usd": COST_PER_SHOT,
            "estimated_cost_per_shot_usd": COST_PER_SHOT,
            "budget_usd": budget, "max_cost_usd": min(20.0, budget),
            "max_job_cost_usd": min(20.0, budget), "cost_note": COST_NOTE,
            "source_width": 480, "source_height": 832,
            "narration_available": _tts_engine() is not None}


def _disk(path: Path, incoming: int = 0) -> None:
    if shutil.disk_usage(path).free - incoming < MIN_FREE:
        raise MediaError("At least 10 GB free disk is required")


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _run(args: list[str], out: Path, *, stdin: str | None = None,
         timeout: float = 120, capture: bool = False) -> str:
    """No shell; inherit supervisor group; bounded runtime and CPU pools."""
    env = os.environ.copy()
    for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS", "BLIS_NUM_THREADS",
                "RAYON_NUM_THREADS"):
        env[key] = "3"
    _disk(out)
    # Raw provider data and argv containing user text never enter logs.
    with (out / "media.log").open("ab") as log:
        proc = subprocess.Popen(args, cwd=out, env=env, stdin=subprocess.PIPE if stdin is not None
                                else subprocess.DEVNULL,
                                stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                                stderr=log)
        try:
            stdout, _ = proc.communicate(stdin.encode("utf-8") if stdin is not None else None,
                                         timeout=timeout)
            if proc.returncode:
                raise MediaError("Media command failed; inspect the private media log")
            if stdout and len(stdout) > 65536:
                raise MediaError("Media probe response exceeded its bound")
            return stdout.decode("utf-8") if stdout else ""
        except subprocess.TimeoutExpired:
            raise MediaError("Media command exceeded its runtime limit") from None
        finally:
            stop_process(proc)


def _ffmpeg(out: Path, args: list[str], *, timeout: float = 120) -> None:
    _run([shutil.which("ffmpeg") or "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
          "-threads", "3", "-filter_threads", "3", "-filter_complex_threads", "3", *args],
         out, timeout=timeout)


def _probe(path: Path, out: Path) -> dict:
    try:
        return json.loads(_run([shutil.which("ffprobe") or "ffprobe", "-v", "error",
                               "-show_entries", "format=duration:stream=width,height,codec_type",
                               "-of", "json", str(path)], out, timeout=15, capture=True))
    except (ValueError, OSError):
        raise MediaError("Media source could not be probed") from None


@contextmanager
def _termination_cleanup():
    """Scoped worker-only TERM handler; restore the caller's handler on exit."""
    if threading.current_thread() is not threading.main_thread():
        raise MediaError("Cloud rendering must run in the supervised worker main thread")
    previous = signal.getsignal(signal.SIGTERM)

    def stop(signum, frame):
        raise _Stopped()

    signal.signal(signal.SIGTERM, stop)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


class _Provider:
    def __init__(self, out: Path, key: str):
        self.out = out
        self.path = out / "provider.json"
        self.journal = {"version": 1, "cache_key": key, "model": MODEL,
                        "shots": {}, "events": []}
        if self.path.exists():
            self.journal = json.loads(self.path.read_text())
            if self.journal.get("cache_key") != key or self.journal.get("model") != MODEL:
                raise MediaError("Provider journal differs; use a new job directory")
        self.client = httpx.Client(timeout=httpx.Timeout(20, connect=5), follow_redirects=False,
                                   trust_env=False,
                                   headers={"Authorization": "Bearer " +
                                            os.environ.get("REPLICATE_API_TOKEN", "")})

    def event(self, event: str, shot: int, **fields) -> None:
        entry = {"event": event, "shot": shot, "time": time.time(), **fields}
        self.journal["events"].append(entry)
        atomic_json(self.path, self.journal)

    def request(self, method: str, endpoint: str, *, data: dict | None = None) -> dict:
        # Caller supplies fixed endpoints or validated provider IDs; never follows response URLs.
        for attempt in range(3 if method == "GET" else 1):
            try:
                with self.client.stream(method, API + endpoint, json=data,
                                        headers={"Cancel-After": "10m"},
                                        timeout=2 if endpoint.endswith("/cancel") else 20) as response:
                    if (method == "GET" and attempt < 2
                            and response.status_code in {429, 500, 502, 503, 504}):
                        time.sleep(0.5 * (attempt + 1))
                        continue
                    if not 200 <= response.status_code < 300:
                        raise MediaError("Provider request failed; reconcile the provider journal")
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_API_BYTES:
                            raise MediaError("Provider response exceeded its bound")
                    value = json.loads(body)
                    if not isinstance(value, dict):
                        raise MediaError("Provider response must be an object")
                    return value
            except (httpx.HTTPError, ValueError):
                if method == "GET" and attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise MediaError("Provider request interrupted; reconcile before retrying") from None
        raise MediaError("Provider polling failed")

    def cancel(self, shot: int, entry: dict) -> None:
        if entry.get("prediction_id") and entry.get("state") not in {
                "succeeded", "failed", "canceled"}:
            # TERM already raised us into cleanup; a second signal must not abort the attempt.
            try:
                self.event("cancel_requested", shot, prediction_id=entry["prediction_id"])
                self.request("POST", f"/predictions/{entry['prediction_id']}/cancel")
            except BaseException:  # noqa: BLE001 - best effort during worker termination
                self.event("cancel_failed", shot, prediction_id=entry["prediction_id"])

    def clip(self, shot: int, prompt: str, budget: list[int]) -> Path:
        path = self.out / f"source-{shot:02}.mp4"
        entry = self.journal["shots"].get(str(shot))
        if (entry and entry.get("clip_sha256") and path.is_file()
                and _digest(path) == entry["clip_sha256"]):
            return path
        if entry and not entry.get("prediction_id"):
            raise MediaError("Submission outcome is unknown; reconcile manually, never resubmit")
        try:
            if entry is None:
                entry = {"state": "submitting"}
                self.journal["shots"][str(shot)] = entry
                self.event("submitting", shot)  # fsynced BEFORE the potentially billable POST
                response = self.request("POST", f"/models/{MODEL}/predictions", data={"input": {
                    "prompt": prompt, "num_frames": 81, "frames_per_second": 16,
                    "aspect_ratio": "9:16", "resolution": "480p", "go_fast": True}})
                prediction_id = response.get("id")
                if not isinstance(prediction_id, str) or not re.fullmatch(
                        r"[a-zA-Z0-9]{1,80}", prediction_id):
                    raise MediaError("Invalid provider ID; reconcile before retrying")
                entry.update(prediction_id=prediction_id, state="starting")
                self.event("prediction", shot, prediction_id=prediction_id, status="starting")
            # GET even after create: never persist raw responses, signed URLs or provider logs.
            deadline = time.monotonic() + POLL_SECONDS
            while time.monotonic() < deadline:
                response = self.request("GET", f"/predictions/{entry['prediction_id']}")
                status = response.get("status")
                if status not in {"starting", "processing", "succeeded", "failed", "canceled"}:
                    raise MediaError("Unexpected provider status")
                if status != entry["state"]:
                    entry["state"] = status
                    self.event("status", shot, prediction_id=entry["prediction_id"], status=status)
                if status in {"failed", "canceled"}:
                    raise MediaError("Provider prediction stopped; no automatic paid retry")
                if status == "succeeded":
                    url = response.get("output")
                    if not isinstance(url, str):
                        raise MediaError("Provider returned no video URL")
                    _download(url, path, budget)
                    info = _probe(path, self.out)
                    video = next((s for s in info.get("streams", [])
                                  if s.get("codec_type") == "video"), {})
                    if not (video.get("width") == 480 and video.get("height") == 832):
                        raise MediaError("Provider source differs from the trusted 480p catalogue")
                    duration = float(info.get("format", {}).get("duration", 0))
                    if not math.isfinite(duration) or not 4.9 <= duration <= 10:
                        raise MediaError("Provider source has invalid duration")
                    entry["clip_sha256"] = _digest(path)
                    self.event("downloaded", shot, prediction_id=entry["prediction_id"])
                    return path
                time.sleep(2)
            raise MediaError("Provider prediction timed out")
        except BaseException:
            self.cancel(shot, entry)
            raise


class _PinnedHTTPS(http.client.HTTPSConnection):
    """Connect to the validated IP while verifying TLS/SNI against the delivery host."""
    def __init__(self, host: str, address: str):
        super().__init__(host, timeout=15, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        sock = socket.create_connection((self.address, 443), timeout=self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


def _download(url: str, path: Path, budget: list[int]) -> None:
    """Pinned public DNS, strict HTTPS delivery allowlist, no redirects or proxy env."""
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.fragment
                or parsed.port not in {None, 443} or not re.fullmatch(
                    r"(?:[a-z0-9-]+\.)*replicate\.delivery", host)):
            raise ValueError()
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise ValueError()
        connection = _PinnedHTTPS(host, addresses[0][4][0])
    except (ValueError, OSError):
        raise MediaError("Untrusted or non-public provider download address") from None
    partial = path.with_suffix(".part")
    deadline = time.monotonic() + 120
    try:
        headers = {"Accept-Encoding": "identity"}
        # Replicate's reference requires auth for prediction files. Only send it after
        # delivery-host validation and DNS pinning; redirects are never followed.
        token = os.environ.get("REPLICATE_API_TOKEN", "").strip()
        if token:
            headers["Authorization"] = "Bearer " + token
        connection.request("GET", parsed.path + ("?" + parsed.query if parsed.query else ""),
                           headers=headers)
        response = connection.getresponse()
        if response.status != 200:
            raise MediaError("Provider download failed or redirected")
        declared = response.getheader("Content-Length")
        if declared and (int(declared) < 0 or int(declared) > budget[0]):
            raise MediaError("Provider download exceeds job byte budget")
        _disk(path.parent, min(budget[0], int(declared)) if declared else budget[0])
        received = 0
        with partial.open("wb") as stream:
            while True:
                if time.monotonic() > deadline:
                    raise MediaError("Provider download exceeded its runtime bound")
                chunk = response.read(min(65536, budget[0] + 1))
                if not chunk:
                    break
                budget[0] -= len(chunk)
                received += len(chunk)
                if budget[0] < 0:
                    raise MediaError("Provider download exceeds job byte budget")
                _disk(path.parent, len(chunk))
                stream.write(chunk)
        if not received or (declared and received != int(declared)):
            raise MediaError("Provider download was empty or incomplete")
        partial.replace(path)
    except (OSError, ValueError, http.client.HTTPException):
        raise MediaError("Provider download interrupted") from None
    finally:
        connection.close()
        partial.unlink(missing_ok=True)


def _card(out: Path, shot: dict, index: int, style: str) -> Path:
    w, h = PREVIEW_SIZE
    image = Image.new("RGB", (w, h), "#17232E")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=max(12, w // 20))
    small = ImageFont.load_default(size=max(10, w // 28))
    draw.text((w * .07, h * .08), "DRAFT / TYPOGRAPHY ANIMATIC", font=small, fill="#FFC976")
    draw.text((w * .07, h * .15), f"Requested style: {style}", font=small, fill="white")
    text = shot.get("caption") or shot.get("narration") or shot["prompt"]
    lines, current = [], ""
    for char in text[:500].replace("\n", " "):
        if draw.textlength(current + char, font=font) > w * .84:
            lines.append(current)
            current = ""
        current += char
    lines.append(current)
    draw.multiline_text((w * .08, h * .35), "\n".join(lines[:10]), font=font,
                        fill="white", spacing=6)
    draw.text((w * .08, h * .87), f"SHOT {index + 1} / NOT A FINAL STYLE RENDER",
              font=small, fill="#FFC976")
    path = out / f"card-{index:02}.png"
    image.save(path)
    return path


def _caption_overlay(shot: dict, index: int, out: Path) -> Path:
    """Safe-area captions are our overlays, never text requested from the model."""
    w, h = SIZE
    text = re.sub(r"[\r\n]+", " ", shot.get("caption") or shot.get("narration") or "")
    image = Image.new("RGBA", SIZE)
    draw = ImageDraw.Draw(image)
    lines = []
    for size in range(max(12, round(w * .052)), max(9, round(w * .027)), -1):
        font = ImageFont.load_default(size=size)
        lines, line = [], ""
        for char in text:
            if draw.textlength(line + char, font=font) > w * .84 and line:
                lines.append(line.strip())
                line = ""
            line += char
        if line:
            lines.append(line.strip())
        if len(lines) <= 2:
            break
    if len(lines) > 2:
        raise MediaError("Caption is too long for two readable lines; shorten it")
    if text:
        label = "\n".join(lines)
        box = draw.multiline_textbbox((0, 0), label, font=font, spacing=8, align="center")
        tw, th = box[2] - box[0], box[3] - box[1]
        x, y = (w - tw) / 2, h * .72
        pad = max(6, round(w * .018))
        draw.rounded_rectangle((x-pad, y-pad, x+tw+pad, y+th+pad), radius=pad,
                               fill=(0, 0, 0, 190))
        draw.multiline_text((x, y-box[1]), label, font=font, spacing=8,
                            align="center", fill="white")
    path = out / f"caption-{index:02}.png"
    image.save(path)
    return path


def _narration(shot: dict, index: int, out: Path, engine: tuple[str, str]) -> Path:
    text = (shot.get("narration") or shot.get("caption") or "").strip()
    if not text or len(text.split()) > 25:
        raise MediaError("Narration needs 1–25 spoken words per five-second shot")
    executable, voice = engine
    source = out / f"speech-{index:02}.{'aiff' if voice == 'Samantha' else 'wav'}"
    if voice == "Samantha":
        # Input through stdin: no user text interpreted as options or shell commands.
        _run([executable, "-v", "Samantha", "-r", "165", "-o", str(source)], out,
             stdin=text, timeout=30)
    else:
        _run([executable, "-v", "en", "-s", "165", "-w", str(source), "--stdin"], out,
             stdin=text, timeout=30)
    duration = float(_probe(source, out).get("format", {}).get("duration", 0))
    if not math.isfinite(duration) or not 0 < duration <= 9:
        raise MediaError("Narration is too long for its shot; shorten the planned words")
    speed = max(1.0, duration / 4.8)
    if speed > 1.8:
        raise MediaError("Narration would need excessive speed-up; shorten it")
    target = out / f"audio-{index:02}.wav"
    _ffmpeg(out, ["-i", str(source), "-af", (f"atempo={speed:.6f},apad,atrim=duration=5,"
                 "asetpts=PTS-STARTPTS,afade=t=out:st=4.9:d=0.1"), "-ar", "48000",
                 "-ac", "1", str(target)])
    return target


def _prompt(creation: dict, shot: dict) -> str:
    brief, plan = creation["brief"], creation["plan"]
    cast = plan.get("characters") or brief.get("characters", [])
    identities = "; ".join(f"{c['name']}: {c['description']}" for c in cast)
    return (f"{STYLE_PROMPTS[brief['style']]}. {shot['prompt']}. "
            f"Maintain these original character identities across shots: {identities or 'none'}. "
            "Continuous visible movement, coherent action, no text, no lettering, no logos, "
            "no watermarks. Do not imitate a real person's likeness.")


def _stamp(seconds: int) -> str:
    return f"00:{seconds // 60:02}:{seconds % 60:02},000"


def render_creation(creation: dict, request: dict, out: Path,
                    progress: Callable[[int, str], None] = lambda value, message: None) -> dict:
    # Worker envelope is additive; canonical render contract remains unchanged.
    creation = Creation.model_validate(creation).model_dump(mode="json")
    request = CreationRender.model_validate({k: v for k, v in request.items() if k != "kind"})
    plan, brief = creation["plan"], creation["brief"]
    if not plan or len(plan["shots"]) * 5 != brief["duration"]:
        raise MediaError("Save a plan with five-second shots matching the requested duration")
    if [s["number"] for s in plan["shots"]] != list(range(1, len(plan["shots"]) + 1)):
        raise MediaError("Shot numbers must be sequential")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise MediaError("FFmpeg and FFprobe are required")
    engine = _tts_engine() if brief["audio"] == "narration" else None
    if brief["audio"] == "narration" and engine is None:
        raise MediaError("System narration unavailable: install espeak-ng or use macOS say")
    if request.mode == "cloud":
        status = cloud_status()
        if not status["available"]:
            raise MediaError(status["reason"])
        if not len(plan["shots"]) * COST_PER_SHOT <= request.max_cost_usd <= status["max_cost_usd"]:
            raise MediaError("Cloud estimate exceeds request or administrator budget ceiling")
    out = out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    _disk(out)
    key = hashlib.sha256(json.dumps({"version": VERSION, "creation": creation,
                                    "mode": request.mode, "model": MODEL},
                                   sort_keys=True).encode()).hexdigest()
    cache_path = out / "media-cache.json"
    if cache_path.exists():
        cache = json.loads(cache_path.read_text())
        if cache.get("cache_key") == key and all(
                (out / name).is_file() and _digest(out / name) == digest
                for name, digest in cache["artifacts"].items()):
            progress(98, "Using completed media cache")
            return cache["metadata"]
    # Preparing narration before dispatch means invalid speech never costs a prediction.
    audio_paths = [_narration(shot, i, out, engine) for i, shot in enumerate(plan["shots"])] \
        if engine else []
    overlays = [_caption_overlay(shot, i, out) for i, shot in enumerate(plan["shots"])] \
        if request.mode == "cloud" else []
    provider = _Provider(out, key) if request.mode == "cloud" else None
    budget = [MAX_DOWNLOAD_BYTES]
    try:
        with _termination_cleanup():
            for i, shot in enumerate(plan["shots"]):
                progress(5 + int(75 * i / len(plan["shots"])),
                         "Generating managed motion" if provider else "Drawing draft typography")
                source = provider.clip(i, _prompt(creation, shot), budget) if provider else \
                    _card(out, shot, i, brief["style"])
                w, h = SIZE
                vf = (f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
                      "setsar=1,fps=30,trim=duration=5,setpts=PTS-STARTPTS")
                if not provider:
                    # Deliberately animated typography; never a fake requested-style result.
                    vf = (f"scale={w + 24}:{h + 24},"
                          f"crop={w}:{h}:x='12+10*sin(t*1.2)':y='12+10*cos(t*1.2)',"
                          "setsar=1,fps=30,trim=duration=5,setpts=PTS-STARTPTS")
                args = (["-loop", "1", "-framerate", "30"] if not provider else [])
                filters = (["-i", str(overlays[i]), "-filter_complex",
                            (f"[0:v]{vf}[base];[base][1:v]overlay=0:0:format=auto,"
                            "format=yuv420p[v]"), "-map", "[v]"] if provider else ["-vf", vf])
                _ffmpeg(out, [*args, "-threads", "3", "-i", str(source), *filters, "-an",
                             "-t", "5", "-c:v", "libx264", "-threads", "3", "-preset",
                             "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                             str(out / f"shot-{i:02}.mp4")])
            # Only generated local names reach concat demuxers; user strings never enter them.
            (out / "video-list.txt").write_text("".join(
                f"file 'shot-{i:02}.mp4'\n" for i in range(len(plan["shots"]))), encoding="utf-8")
            inputs = ["-f", "concat", "-safe", "1", "-i", str(out / "video-list.txt")]
            if audio_paths:
                (out / "audio-list.txt").write_text("".join(
                    f"file 'audio-{i:02}.wav'\n" for i in range(len(audio_paths))), encoding="utf-8")
                inputs += ["-f", "concat", "-safe", "1", "-i", str(out / "audio-list.txt")]
            else:
                sound = ("aevalsrc=0.025*sin(2*PI*174*t)+0.012*sin(2*PI*261*t):s=48000"
                         if brief["audio"] == "ambient" else "anullsrc=r=48000:cl=mono")
                inputs += ["-f", "lavfi", "-i", sound]
            seconds = brief["duration"]
            _ffmpeg(out, [*inputs, "-map", "0:v:0", "-map", "1:a:0", "-t", str(seconds),
                         "-c:v", "copy", "-af", f"afade=t=in:d=0.1,afade=t=out:st={seconds-.2}:d=0.2",
                         "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
                         str(out / "video.encoding.mp4")])
            (out / "video.encoding.mp4").replace(out / "video.mp4")
            _ffmpeg(out, ["-i", str(out / "video.mp4"), "-frames:v", "1",
                         str(out / "poster.png")], timeout=30)
    except _Stopped:
        raise MediaError("Creation cancelled; reconcile any provider reservation") from None
    finally:
        if provider:
            provider.client.close()
    captions = []
    for i, shot in enumerate(plan["shots"]):
        text = shot.get("caption") or shot.get("narration") or ""
        text = re.sub(r"[\r\n]+", " ", text).replace("-->", "→")
        if text:
            captions.append(f"{len(captions)+1}\n{_stamp(i*5)} --> {_stamp((i+1)*5)}\n{text}\n")
    (out / "captions.srt").write_text("\n".join(captions), encoding="utf-8")
    preview = request.mode == "preview"
    source_size = PREVIEW_SIZE if preview else (480, 832)
    audio_note = ({"ambient": "Original procedural ambient instrumental; no speech or singing",
                   "silent": "Silent AAC track; no speech or singing",
                   "narration": "System-generated English narration; no singing or human speaker claim"}
                  [brief["audio"]])
    metadata = {"kind": "creation", "mode": request.mode, "guide_audio": brief["audio"] == "ambient",
                "seconds": brief["duration"], "width": SIZE[0], "height": SIZE[1], "fps": 30,
                "encoder": "libx264", "video_codec": "h264", "audio_codec": "aac",
                "estimated_cost_usd": 0 if preview else len(plan["shots"]) * COST_PER_SHOT,
                "cost_note": COST_NOTE, "publishable": False, "approved": False,
                "published": False, "draft": True, "review_ready": False,
                "human_review_required": True, "ai_disclosure": True,
                "made_for_kids": brief["audience"] == "kids", "platforms": brief["platforms"],
                "project_revision": creation["revision"], "requested_style": brief["style"],
                "render_type": "draft-typography-animatic" if preview else "managed-generated-motion",
                "source_model": None if preview else MODEL,
                "source_width": source_size[0], "source_height": source_size[1],
                "source_resolution": {"width": source_size[0], "height": source_size[1],
                                      "label": "CPU typography animatic" if preview else
                                      "480p generated source, scaled/cropped; not native HD"},
                "preview_note": "Draft animatic; does not render the requested visual style" if preview
                else "Generated motion quality and character continuity require human review",
                "audio_mode": brief["audio"], "audio_note": audio_note,
                "narration_engine": Path(engine[0]).name if engine else None,
                "captions": "Burned-in safe-area captions plus captions.srt sidecar" if not preview else
                "Draft typography plus captions.srt sidecar",
                "title": plan["title"], "description": plan["description"],
                "hashtags": plan["hashtags"]}
    atomic_json(out / "metadata.json", metadata)
    (out / "description.txt").write_text(
        f"{plan['title']}\n\n{plan['description']}\n{' '.join(plan['hashtags'])}\n\n"
        f"AI-assisted content. {metadata['preview_note']}\n{audio_note}\n"
        "Human review required; no publishing approval.\n", encoding="utf-8")
    names = ("video.mp4", "poster.png", "captions.srt", "metadata.json", "description.txt")
    atomic_json(cache_path, {"cache_key": key, "metadata": metadata,
                             "artifacts": {name: _digest(out / name) for name in names}})
    progress(98, "Draft media and review metadata written")
    return metadata
