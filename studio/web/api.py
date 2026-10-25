"""Pond Studio's local single-owner HTTP API (doc 30)."""
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import wave
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .jobs import JobQueue
from .models import CreateProject, JobRequest, Plan, Project, Review, Session, storyboard, uid
from .security import Security
from .store import Store

MAX_BODY = 26 * 1024 * 1024


class BodyLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        try:
            length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            return await JSONResponse({"detail": "Invalid content length"}, 400)(scope, receive, send)
        if length > MAX_BODY:
            return await JSONResponse({"detail": "Request too large"}, 413)(scope, receive, send)
        count = 0

        async def bounded():
            nonlocal count
            message = await receive()
            count += len(message.get("body", b""))
            if count > MAX_BODY:
                raise HTTPException(413, "Request too large")
            return message

        await self.app(scope, bounded, send)


def media_response(path, request, media_type, download=False):
    """Bounded single byte ranges, including suffixes, for browser video playback."""
    size = path.stat().st_size
    headers = {"Accept-Ranges": "bytes"}
    disposition = "attachment" if download else "inline"
    headers["Content-Disposition"] = f'{disposition}; filename="{path.name}"'
    start, end, status = 0, size - 1, 200
    raw = request.headers.get("range")
    if raw:
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", raw)
        if not match or not any(match.groups()) or not size:
            raise HTTPException(416, "Invalid byte range", headers={"Content-Range": f"bytes */{size}"})
        left, right = match.groups()
        if left:
            start = int(left)
            end = min(int(right), size - 1) if right else size - 1
        else:
            start = max(0, size - int(right))
        if start > end or start >= size:
            raise HTTPException(416, "Range is not satisfiable",
                                headers={"Content-Range": f"bytes */{size}"})
        status = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    headers["Content-Length"] = str(max(0, end - start + 1))

    def chunks():
        remaining = end - start + 1
        with path.open("rb") as source:
            source.seek(start)
            while remaining > 0:
                block = source.read(min(remaining, 256 * 1024))
                if not block:
                    break
                remaining -= len(block)
                yield block

    return StreamingResponse(chunks(), status_code=status, media_type=media_type, headers=headers)


def validated_upload(data, kind):
    """Validate actual contents; never trust a filename or browser MIME type."""
    if kind == "image":
        from PIL import Image, UnidentifiedImageError
        try:
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in {"PNG", "JPEG", "WEBP"}:
                    raise ValueError("Unsupported image")
                if image.width * image.height > 16_000_000 or max(image.size) > 8192:
                    raise ValueError("Image too large")
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                output = io.BytesIO()
                image.convert("RGBA").save(output, format="PNG")
                return output.getvalue(), ".png"
        except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
            raise HTTPException(422, "Upload must be a valid bounded PNG, JPEG or WebP") from None
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        try:
            with wave.open(io.BytesIO(data)) as sound:
                if not (1 <= sound.getnchannels() <= 2 and 8000 <= sound.getframerate() <= 96000
                        and 1 <= sound.getsampwidth() <= 4
                        and 0 < sound.getnframes() / sound.getframerate() <= 600):
                    raise ValueError("Unsupported WAV")
                expected = sound.getnframes() * sound.getnchannels() * sound.getsampwidth()
                if len(sound.readframes(sound.getnframes())) != expected:
                    raise ValueError("Truncated WAV")
            return data, ".wav"
        except (wave.Error, EOFError, ValueError):
            raise HTTPException(422, "Invalid or unsupported WAV audio") from None
    if data.startswith(b"ID3") or data[:2] in {b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"}:
        probe = shutil.which("ffprobe")
        if not probe:
            raise HTTPException(422, "MP3 validation requires ffprobe; upload WAV instead")
        with tempfile.NamedTemporaryFile(suffix=".mp3") as audio:
            audio.write(data)
            audio.flush()
            try:
                result = subprocess.run(
                    [probe, "-v", "error", "-show_entries", "stream=codec_name,codec_type:format=duration",
                     "-of", "json", audio.name], capture_output=True, timeout=5, check=True)
                info = json.loads(result.stdout)
                if not 0 < float(info["format"]["duration"]) <= 600:
                    raise ValueError("Invalid duration")
                if not info["streams"] or any(s["codec_name"] != "mp3" for s in info["streams"]):
                    raise ValueError("Not MP3")
            except (subprocess.SubprocessError, ValueError, KeyError, OSError):
                raise HTTPException(422, "Invalid MP3 audio") from None
        return data, ".mp3"
    raise HTTPException(422, "Upload must contain WAV or MP3 audio")


def create_app(data_dir=None, *, host=None, token=None, allowed_hosts=None, supervisor=None):
    host = host or os.environ.get("STUDIO_WEB_HOST", "127.0.0.1")
    token = token if token is not None else os.environ.get("STUDIO_WEB_TOKEN")
    security = Security(host, token, allowed_hosts=allowed_hosts)
    root = Path(data_dir or os.environ.get("STUDIO_WEB_DATA_DIR", "output/web-studio"))
    store = Store(root)
    jobs = JobQueue(store, supervisor)

    @asynccontextmanager
    async def lifespan(app):
        jobs.start()
        try:
            creator.start()
            yield
        finally:
            creator.stop()
            jobs.stop()
            store.db.close()

    app = FastAPI(title="Video Studio", lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.store = store
    app.state.jobs = jobs
    app.state.security = security
    app.add_middleware(BodyLimit)
    app.middleware("http")(security.middleware)

    from .creator_api import register_creator
    creator = register_creator(app, store, supervisor=supervisor)
    app.state.creator = creator

    @app.get("/create", include_in_schema=False)
    def create_editor():
        return FileResponse(Path(__file__).parent / "static" / "create.html")

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({"detail": "Request validation failed; check field types and limits"}, 422)

    @app.exception_handler(Exception)
    async def internal_error(request, exc):
        return JSONResponse({"detail": "Internal operation failed"}, 500)

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.post("/api/session")
    def session(body: Session, request: Request):
        identifier = security.session(body.token)
        if identifier is None:
            raise HTTPException(401, "Invalid token")
        response = JSONResponse({"ok": True})
        response.set_cookie("studio_session", identifier, httponly=True, samesite="strict",
                            secure=request.url.scheme == "https", max_age=43200, path="/")
        return response

    @app.delete("/api/session")
    def logout(request: Request):
        security.sessions.pop(request.cookies.get("studio_session"), None)
        response = JSONResponse({"ok": True})
        response.delete_cookie("studio_session", path="/")
        return response

    @app.get("/api/system")
    def system():
        from . import providers
        available_providers = (getattr(providers, "available_providers", None)
                               or providers.capabilities)
        from .supervisor import resource_snapshot
        sample = resource_snapshot(store.root)
        sample.update(active_job=jobs.active, providers=available_providers(),
                      auth_required=bool(security.token))
        return sample

    @app.get("/api/projects")
    def projects():
        return {"projects": store.all("project")}

    @app.post("/api/projects")
    def create(body: CreateProject):
        characters, scenes = storyboard(body.template)
        project = Project(name=body.name, topic=body.topic, characters=characters, scenes=scenes)
        return store.put("project", project.model_dump())

    @app.get("/api/projects/{project_id}")
    def get_project(project_id: str):
        return store.get("project", project_id)

    @app.put("/api/projects/{project_id}")
    def save(project_id: str, body: Project):
        if body.id != project_id:
            raise HTTPException(422, "Project ID cannot change")
        store.validate_assets(body)
        return store.save_project(body)

    @app.delete("/api/projects/{project_id}")
    def delete(project_id: str):
        with store.lock:
            store.get("project", project_id)
            if jobs.has_active(project_id):
                raise HTTPException(409, "Cannot delete a project with an active job")
            # Retain opaque files for crash safety; remove their access records.
            for job in store.all("job"):
                if job["project_id"] == project_id:
                    store.delete("job", job["id"])
            for asset in store.all("asset"):
                if asset["project_id"] == project_id:
                    store.delete("asset", asset["id"])
            store.delete("project", project_id)
        return {"ok": True}

    @app.post("/api/projects/{project_id}/plan")
    def plan(project_id: str, body: Plan, response: Response):
        response.headers["X-Storyboard-Source"] = "template-based; not LLM"
        with store.lock:
            project = store.get("project", project_id)
            characters, scenes = storyboard(body.template)
            project.update(topic=body.topic, characters=[c.model_dump() for c in characters],
                           scenes=[s.model_dump() for s in scenes], review=Review().model_dump())
            return store.save_project(Project.model_validate(project))

    @app.post("/api/projects/{project_id}/assets")
    async def upload(project_id: str, request: Request):
        store.get("project", project_id)
        async with request.form(max_files=1, max_fields=3, max_part_size=1024) as form:
            kind = form.get("kind")
            if kind not in {"image", "audio"} or form.get("rights_confirmed") != "true":
                raise HTTPException(422, "Select image/audio and confirm upload rights")
            file = form.get("file")
            if not hasattr(file, "read"):
                raise HTTPException(422, "A file is required")
            limit = (8 if kind == "image" else 25) * 1024 * 1024
            data = await file.read(limit + 1)
            if not data or len(data) > limit:
                raise HTTPException(413, "File is empty or exceeds upload limit")
            data, suffix = validated_upload(data, kind)
            identifier = uid()
            filename = identifier + suffix
            # Never retain the user filename as a path or header value.
            name = str(file.filename or "Upload").replace("\\", "/").split("/")[-1][:120]
            name = "".join(c for c in name if c.isprintable()) or "Upload"
            asset = {"id": identifier, "name": name, "kind": kind,
                         "url": f"/api/projects/{project_id}/assets/{identifier}",
                         "project_id": project_id, "filename": filename}
            with store.lock:
                store.get("project", project_id)
                if shutil.disk_usage(store.root).free - len(data) < 10_000_000_000:
                    raise HTTPException(507, "Upload would leave less than 10 GB free")
                destination = store.directory("assets", project_id) / filename
                temporary = destination.with_suffix(".tmp")
                temporary.write_bytes(data)
                temporary.replace(destination)
                store.put("asset", asset)
            return {k: asset[k] for k in ("id", "name", "kind", "url")}

    @app.get("/api/projects/{project_id}/assets/{asset_id}")
    def asset(project_id: str, asset_id: str, request: Request):
        store.get("project", project_id)
        path = store.asset_path(project_id, asset_id)
        media = {".png": "image/png", ".wav": "audio/wav", ".mp3": "audio/mpeg"}[path.suffix]
        return media_response(path, request, media)

    @app.get("/api/projects/{project_id}/characters/{character_id}/preview")
    def preview(project_id: str, character_id: str, request: Request):
        project = store.get("project", project_id)
        character = next((c for c in project["characters"] if c["id"] == character_id), None)
        if character is None:
            raise HTTPException(404, "Character not found")
        if character["asset_id"]:
            return media_response(store.asset_path(project_id, character["asset_id"], "image"),
                                  request, "image/png")
        from .renderer import character_preview
        return Response(character_preview(character), media_type="image/png")

    @app.post("/api/projects/{project_id}/jobs")
    def submit(project_id: str, body: JobRequest):
        with store.lock:
            project = store.get("project", project_id)
            store.validate_assets(Project.model_validate(project))
            return jobs.submit(project, body)

    @app.get("/api/jobs")
    def list_jobs(project_id: str | None = None):
        if project_id:
            store.get("project", project_id)
        return {"jobs": [jobs.public(j) for j in store.all("job")
                         if project_id is None or j["project_id"] == project_id]}

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str):
        return jobs.public(store.get("job", job_id))

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel(job_id: str):
        return jobs.cancel(job_id)

    def output(job_id, filename, kind):
        job = store.get("job", job_id)
        if job["status"] != "succeeded" or job["kind"] != kind:
            raise HTTPException(409, "Output is not ready for this job")
        return store.owned_file(store.directory("jobs", job_id) / filename)

    @app.get("/api/jobs/{job_id}/video")
    def video(job_id: str, request: Request):
        return media_response(output(job_id, "video.mp4", "render"), request, "video/mp4")

    @app.get("/api/jobs/{job_id}/download")
    def download(job_id: str, request: Request):
        return media_response(output(job_id, "video.mp4", "render"), request, "video/mp4", True)

    @app.get("/api/jobs/{job_id}/poster")
    def poster(job_id: str, request: Request):
        return media_response(output(job_id, "poster.png", "render"), request, "image/png")

    @app.get("/api/jobs/{job_id}/character")
    def character(job_id: str, request: Request):
        return media_response(output(job_id, "character.png", "character"), request, "image/png")

    @app.get("/api/jobs/{job_id}/package")
    def package(job_id: str):
        with store.lock:
            video_path = output(job_id, "video.mp4", "render")
            directory = video_path.parent
            job = jobs.public(store.get("job", job_id))
            snapshot = json.loads(store.owned_file(directory / "project.json").read_text())
            files = [store.owned_file(directory / name)
                     for name in ("video.mp4", "poster.png", "captions.srt")]
            if sum(f.stat().st_size for f in files) > 1024**3:
                raise HTTPException(413, "Bundle exceeds size limit")
            # Closed by the streaming generator after the response finishes.
            bundle = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)  # noqa: SIM115
            metadata = {"project_id": job["project_id"], "project_revision": job["project_revision"],
                            "made_for_kids": True, "ai_disclosure": True, "guide_audio": job["guide_audio"],
                            "review_ready": job["review_ready"],
                            "status": "review-ready" if job["review_ready"] else "draft",
                            "format": job["format"], "resolution": job["resolution"],
                            "approved": False, "published": False,
                            "target_platform": "youtube", "privacy": "private"}
            hashes = {}
            for path in files:
                with path.open("rb") as stream:
                    hashes[path.name] = hashlib.file_digest(stream, "sha256").hexdigest()
            metadata["sha256"] = hashes
            with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for path in files:
                    archive.write(path, path.name)
                archive.writestr("metadata.json", json.dumps(metadata, indent=2))
                archive.writestr("project.json", json.dumps(snapshot, indent=2))
                archive.writestr("description.txt", snapshot["name"] + "\n" + snapshot["topic"]
                                 + "\nAI-assisted original animation. Made for kids.\n")
                archive.writestr("checklist.json", json.dumps({
                    "review": snapshot["review"], "draft": not job["review_ready"],
                    "note": "Local preview bundle. Human review required; no publishing approval."}))
            bundle.seek(0)

        def chunks():
            try:
                while block := bundle.read(256 * 1024):
                    yield block
            finally:
                bundle.close()

        return StreamingResponse(chunks(), media_type="application/zip", headers={
            "Content-Disposition": 'attachment; filename="pond-preview.zip"'})

    static = Path(__file__).parent / "static"
    if static.is_dir():
        app.mount("/static", StaticFiles(directory=static), name="static")
        app.mount("/", StaticFiles(directory=static, html=True), name="frontend")
    return app
