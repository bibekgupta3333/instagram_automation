"""One durable job queue, driven by the supervised worker process contract."""
import json
import queue
import sys
import threading
from pathlib import Path

from fastapi import HTTPException

from .models import Project, now, uid
from .supervisor import FatalCleanupError

REPO = Path(__file__).resolve().parents[2]
TERMINAL = {"succeeded", "failed", "cancelled"}


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


class JobQueue:
    def __init__(self, store, supervisor=None):
        self.store = store
        self.supervisor = supervisor
        self.pending = queue.Queue(maxsize=32)
        self.events = {}
        self.active = None
        self.stopping = threading.Event()
        self.thread = None
        # Never replay potentially partially executed work after process restart.
        for job in store.all("job"):
            if job["status"] not in TERMINAL:
                job.update(status="failed", error="Server restarted; create a new job",
                           message="Interrupted by restart", updated_at=now())
                store.put("job", job)

    def start(self):
        self.thread = threading.Thread(target=self._loop, name="pond-worker-queue", daemon=True)
        self.thread.start()

    def stop(self):
        self.stopping.set()
        with self.store.lock:
            for identifier in list(self.events):
                self.cancel(identifier)
        if self.thread:
            self.thread.join()  # Supervisor returns only after the owned child tree has stopped.

    def has_active(self, project_id):
        return any(j["project_id"] == project_id and j["status"] not in TERMINAL
                   for j in self.store.all("job"))

    def submit(self, project, request):
        with self.store.lock:
            if self.stopping.is_set() or self.pending.full():
                raise HTTPException(429, "Job queue is full or shutting down")
            if self.has_active(project["id"]):
                raise HTTPException(409, "Project already has an active job")
            if request.kind == "character" and not any(
                c["id"] == request.character_id for c in project["characters"]
            ):
                raise HTTPException(422, "Character not found in project")
            identifier = uid()
            job = {"id": identifier, "project_id": project["id"], "kind": request.kind,
                       "status": "queued", "progress": 0, "message": "Queued", "error": None,
                       "created_at": now(), "updated_at": now(), "project_revision": project["revision"],
                       "format": request.format or project["format"],
                       "resolution": request.resolution or project["resolution"],
                       "video_url": None, "download_url": None, "poster_url": None,
                       "character_url": None, "package_url": None, "guide_audio": True, "review_ready": False}
            directory = self.store.directory("jobs", identifier)
            assets = {a["id"]: str(self.store.asset_path(project["id"], a["id"]))
                      for a in self.store.all("asset") if a["project_id"] == project["id"]}
            trusted = request.model_dump()
            trusted.update(format=job["format"], resolution=job["resolution"],
                           assets_dir=str(self.store.directory("assets", project["id"])),
                           asset_paths=assets)
            write_json(directory / "project.json", project)
            write_json(directory / "request.json", trusted)
            self.store.put("job", job)
            self.events[identifier] = threading.Event()
            self.pending.put_nowait(identifier)
            return job

    def cancel(self, identifier):
        with self.store.lock:
            job = self.store.get("job", identifier)
            if job["status"] not in TERMINAL:
                event = self.events.get(identifier)
                if event:
                    event.set()
                if job["status"] == "queued":
                    job.update(status="cancelled", message="Cancelled", updated_at=now())
                else:
                    job.update(message="Cancelling worker", updated_at=now())
                self.store.put("job", job)
            return self.public(job)

    def public(self, job):
        job = dict(job)
        if job["status"] == "running":
            try:
                progress = json.loads((self.store.directory("jobs", job["id"]) /
                                       "progress.json").read_text())
                job["progress"] = max(0, min(99, int(progress["progress"])))
                # Worker diagnostics stay in owned files, not in API responses.
                job["message"] = "Processing"
            except (OSError, ValueError, TypeError, KeyError):
                pass
        project = self.store.get("project", job["project_id"])
        job["review_ready"] = bool(
            job["kind"] == "render" and job["status"] == "succeeded"
            and project["revision"] == job["project_revision"]
            and all(project["review"].values()) and not job["guide_audio"]
            and project["audio"]["mode"] == "uploaded" and project["audio"]["asset_id"]
        )
        return job

    def _loop(self):
        while not self.stopping.is_set():
            try:
                identifier = self.pending.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self._execute(identifier)
            finally:
                self.pending.task_done()

    def _execute(self, identifier):
        with self.store.lock:
            job = self.store.get("job", identifier)
            if job["status"] != "queued":
                return
            event = self.events[identifier]
            job.update(status="running", message="Starting worker", updated_at=now())
            self.store.put("job", job)
            self.active = identifier
        directory = self.store.directory("jobs", identifier)
        supervised = False
        try:
            supervisor = self.supervisor
            if supervisor is None:
                from .supervisor import run_supervised
                supervisor = run_supervised
            command = [sys.executable, "-m", "studio.web.worker", "--project",
                       str(directory / "project.json"), "--request",
                       str(directory / "request.json"), "--out", str(directory)]
            result = supervisor(command, cwd=REPO, log_path=directory / "worker.log",
                                cancel_event=event, timeout_seconds=900)
            supervised = True
            if event.is_set() or result.get("cancelled"):
                job.update(status="cancelled", message="Cancelled")
            elif result.get("returncode") != 0 or result.get("resource_stopped") or result.get("reason"):
                reasons = {
                    "Another guarded job is running": "Another generation job is using the worker slot",
                    "Memory pressure yellow": "Memory pressure is yellow. Wait for green before rendering",
                    "Memory pressure red": "Memory pressure is red. Wait for green before rendering",
                    "Memory pressure unknown": "Memory telemetry is unavailable; the worker was not started",
                    "Disk free space below 10 GB": "Less than 10 GB disk space is available",
                    "Job disk free space below 10 GB": "Less than 10 GB is available on the output drive",
                    "Worker timed out": "The job exceeded its time limit and was stopped",
                }
                job.update(status="failed", message="Worker stopped",
                           error=reasons.get(result.get("reason"),
                                             "Worker failed or resource guard stopped the job"))
            else:
                output = json.loads(self.store.owned_file(directory / "result.json").read_text())
                if output.get("kind") != job["kind"]:
                    raise ValueError("Worker result kind mismatch")
                files = ("video.mp4", "poster.png", "captions.srt", "metadata.json") \
                    if job["kind"] == "render" else ("character.png",)
                for filename in files:
                    self.store.owned_file(directory / filename)
                job.update(status="succeeded", progress=100, message="Complete",
                           guide_audio=bool(output.get("guide_audio", True)))
                base = f"/api/jobs/{identifier}"
                if job["kind"] == "render":
                    job.update(video_url=base + "/video", download_url=base + "/download",
                               poster_url=base + "/poster", package_url=base + "/package")
                else:
                    job["character_url"] = base + "/character"
                    self._attach_character(job, directory)
        except FatalCleanupError:
            self.stopping.set()
            job.update(status="failed", message="Queue stopped until server restart",
                       error="Worker cleanup could not be verified. Inspect the owned worker "
                             "process before restarting the server")
        except Exception:  # noqa: BLE001 -- fail closed at the worker trust boundary
            # Unverified supervisor cleanup must poison this queue until restart.
            job.update(status="cancelled" if event.is_set() else "failed",
                       message="Worker did not complete", error="Worker failed; no output is ready")
            if not supervised:
                self.stopping.set()
        finally:
            with self.store.lock:
                job["updated_at"] = now()
                self.store.put("job", job)
                self.active = None
                self.events.pop(identifier, None)

    def _attach_character(self, job, directory):
        import shutil
        with self.store.lock:
            project = self.store.get("project", job["project_id"])
            if project["revision"] != job["project_revision"]:
                return
            request = json.loads((directory / "request.json").read_text())
            identifier = uid()
            filename = identifier + ".png"
            target = self.store.directory("assets", project["id"]) / filename
            source = self.store.owned_file(directory / "character.png")
            # Do not trust a successful worker to have produced a valid raster.
            from PIL import Image
            with Image.open(source) as image:
                image.verify()
            shutil.copyfile(source, target)
            self.store.put("asset", {"id": identifier, "project_id": project["id"], "kind": "image",
                                         "name": "Generated character.png", "filename": filename,
                                         "url": f"/api/projects/{project['id']}/assets/{identifier}"})
            for character in project["characters"]:
                if character["id"] == request["character_id"]:
                    character["asset_id"] = identifier
            self.store.save_project(Project.model_validate(project))
