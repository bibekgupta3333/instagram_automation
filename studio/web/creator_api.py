"""General creator API and durable, supervised queue (doc 32).

Only trusted server configuration enables billing. SQLite reservations remain held
when provider charges cannot be reconciled, including interrupted submissions.
"""
import hashlib
import json
import math
import os
import queue
import re
import sys
import tempfile
import threading
import zipfile

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from .creator_models import Creation, CreationRender, SaveCreation, VideoBrief, VideoPlan
from .jobs import REPO, TERMINAL, write_json
from .models import now, uid
from .supervisor import FatalCleanupError, resource_snapshot

PREFIX = "/api/creator"
OUTPUTS = ("video.mp4", "poster.png", "captions.srt", "metadata.json", "description.txt")
PUBLIC_JOB = (
    "id", "project_id", "project_revision", "status", "progress", "message", "error", "mode",
    "created_at", "updated_at", "video_url", "download_url", "poster_url", "package_url",
    "estimated_cost_usd", "guide_audio",
)


def money(value):
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else 0.0
    except (TypeError, ValueError):
        return 0.0


def validate_plan(brief, plan):
    for characters in (brief.characters, plan.characters if plan else []):
        names = [character.name.strip().casefold() for character in characters]
        if len(set(names)) != len(names):
            raise HTTPException(422, "Character names must be unique within each cast")
    if plan is None:
        return
    if sum(shot.duration for shot in plan.shots) != brief.duration:
        raise HTTPException(422, "Shot durations must equal the brief duration")
    if [shot.number for shot in plan.shots] != list(range(1, len(plan.shots) + 1)):
        raise HTTPException(422, "Shot numbers must be consecutive in timeline order, starting at 1")


class CreatorService:
    def __init__(self, store, *, supervisor=None, planner=None, cloud_status=None):
        self.store = store
        self.supervisor = supervisor
        self.planner = planner
        self.cloud_status_fn = cloud_status
        self.pending = queue.Queue(maxsize=32)
        self.events = {}
        self.planning = set()
        self.active = None
        self.thread = None
        self.stopping = threading.Event()
        for job in store.all("creation_job"):
            self.sync_provider(job)
            if job["status"] not in TERMINAL:
                job.update(status="failed", message="Interrupted by server restart",
                           error="Server restarted. Provider charges may need reconciliation; "
                                 "this job will not be replayed", updated_at=now())
                store.put("creation_job", job)
                self._reservation(job, "unknown" if job.get("dispatched") else "released")
                self.record("interrupted", job)

    def record(self, event, job=None, *, project_id=None):
        self.store.put("creation_event", {
            "id": uid(), "event": event, "created_at": now(),
            "job_id": job["id"] if job else None,
            "project_id": job["project_id"] if job else project_id,
        })

    def sync_provider(self, job):
        """Mirror bounded, sanitized provider journal into private SQLite records.

        The original fsynced artifact remains the crash-recovery source of truth.
        IDs/events are never exposed by the public job or package endpoints.
        """
        try:
            path = self.store.owned_file(self.store.directory("jobs", job["id"]) / "provider.json")
            if path.stat().st_size > 256 * 1024:
                return
            journal = json.loads(path.read_text())
            for index, event in enumerate(journal.get("events", [])[:1000]):
                if not isinstance(event, dict) or event.get("event") not in {
                    "submitting", "prediction", "status", "downloaded", "cancel_requested", "cancel_failed",
                }:
                    continue
                safe = {"id": f"{job['id']}_{index}", "job_id": job["id"],
                        "event": event["event"], "project_id": job["project_id"]}
                if type(event.get("shot")) is int and 0 <= event["shot"] <= 12:
                    safe["shot"] = event["shot"]
                prediction = event.get("prediction_id")
                if isinstance(prediction, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", prediction):
                    safe["prediction_id"] = prediction
                if event.get("status") in {
                    "submitting", "starting", "processing", "succeeded", "failed", "canceled",
                }:
                    safe["status"] = event["status"]
                if type(event.get("time")) in (int, float) and math.isfinite(event["time"]):
                    safe["time"] = event["time"]
                self.store.put("creation_provider_event", safe)
        except (HTTPException, OSError, ValueError, TypeError, AttributeError):
            # Partial writes/missing artifact are never proof of zero provider charges.
            return

    def start(self):
        if self.thread is None and not self.stopping.is_set():
            self.thread = threading.Thread(target=self._loop, name="creator-worker-queue", daemon=True)
            self.thread.start()

    def stop(self):
        self.stopping.set()
        with self.store.lock:
            for identifier in list(self.events):
                self.cancel(identifier)
        if self.thread:
            self.thread.join()  # The supervisor owns and verifies whole process-group cleanup.

    def has_active(self, project_id):
        return project_id in self.planning or any(
            j["project_id"] == project_id and j["status"] not in TERMINAL
            for j in self.store.all("creation_job"))

    def cloud_status(self):
        try:
            if self.cloud_status_fn:
                value = self.cloud_status_fn()
            else:
                from .creator_media import cloud_status
                value = cloud_status()
        except Exception:  # noqa: BLE001 -- never expose provider diagnostics
            value = {}
        if not isinstance(value, dict):
            value = {}
        # Explicit allowlist: raw provider errors/configuration never reach clients.
        enabled = (os.environ.get("STUDIO_CREATOR_CLOUD_ENABLED") == "1"
                   and value.get("enabled") is True)
        available = enabled and value.get("available") is True
        cap = money(os.environ.get("STUDIO_CREATOR_CLOUD_BUDGET_USD", "0"))
        held = sum(money(r["amount_usd"]) for r in self.store.all("creation_reservation")
                   if r["status"] != "released")
        return {
            "enabled": enabled, "available": available and cap > held,
            "reason": ("Cloud generation is configured; estimates are not a vendor billing cap"
                       if available and cap > held else
                       "Cloud generation is disabled, unavailable, or the server budget is exhausted"),
            "cost_per_second_usd": money(value.get("cost_per_second_usd")),
            "cost_per_shot_usd": money(value.get("cost_per_shot_usd")),
            "max_cost_usd": min(20, cap),
            "cost_note": "Estimate only; reservations do not enforce a vendor billing cap",
            "narration_available": value.get("narration_available") is True,
            "budget_usd": cap, "reserved_usd": held, "remaining_budget_usd": max(0, cap - held),
        }

    def planner_status(self):
        try:
            if self.planner:
                return {"available": True, "source": "injected"}
            from .creator_planner import planner_status
            value = planner_status()
            return {"available": value.get("available") is True,
                    "reason": "Planner available" if value.get("available") else
                              "Template planning is available; optional planner unavailable"}
        except Exception:  # noqa: BLE001
            return {"available": False, "reason": "Planner is unavailable"}

    def plan(self, identifier):
        with self.store.lock:
            current = self.store.get("creation", identifier)
            if self.has_active(identifier):
                raise HTTPException(409, "Project has an active job or planning request")
            if self.planning:
                raise HTTPException(429, "Another project is planning; try again when it finishes")
            self.planning.add(identifier)
        try:
            planner = self.planner
            if planner is None:
                from .creator_planner import plan_video
                planner = plan_video
            # Planner owns its CLI timeout/output limits. Never hold SQLite lock during CLI work.
            try:
                planned = VideoPlan.model_validate(planner(VideoBrief.model_validate(current["brief"])))
            except Exception:  # noqa: BLE001 -- even HTTPException from a provider is untrusted
                raise HTTPException(503, "Planning did not complete; check planner status") from None
            validate_plan(VideoBrief.model_validate(current["brief"]), planned)
            with self.store.lock:
                latest = self.store.get("creation", identifier)
                if latest["revision"] != current["revision"]:
                    raise HTTPException(409, "Project changed during planning; reload")
                current.update(plan=planned.model_dump(), revision=current["revision"] + 1,
                               updated_at=now())
                self.store.put("creation", current)
                self.record("planned", project_id=identifier)
                return current
        except HTTPException:
            raise
        except Exception:  # noqa: BLE001 -- no provider traceback or raw error in HTTP responses
            raise HTTPException(503, "Planning did not complete; try again after checking status") from None
        finally:
            with self.store.lock:
                self.planning.discard(identifier)

    def submit(self, identifier, request):
        with self.store.lock:
            project = self.store.get("creation", identifier)
            if self.stopping.is_set() or self.pending.full():
                raise HTTPException(429, "Queue is full or stopped until server restart")
            if self.has_active(identifier):
                raise HTTPException(409, "Project already has an active job or planning request")
            creation = Creation.model_validate(project)
            if creation.plan is None:
                raise HTTPException(409, "Save a plan before generating")
            validate_plan(creation.brief, creation.plan)
            estimate = 0.0
            if request.mode == "cloud":
                status = self.cloud_status()
                if not status["available"]:
                    raise HTTPException(409, "Cloud generation is not enabled or budget is unavailable")
                if creation.brief.audio == "narration" and not status["narration_available"]:
                    raise HTTPException(409, "Cloud narration is unavailable; choose ambient or silent")
                estimate = round(status["cost_per_second_usd"] * creation.brief.duration, 6)
                if not estimate or request.max_cost_usd < estimate:
                    raise HTTPException(422, "A sufficient cost estimate and budget are required")
                if request.max_cost_usd > min(20, status["remaining_budget_usd"]):
                    raise HTTPException(409, "Server cloud budget cannot cover this reservation")
            identifier = uid()
            job = {"id": identifier, "kind": "creation", "project_id": project["id"],
                   "project_revision": project["revision"], "status": "queued", "progress": 0,
                   "message": "Queued", "error": None, "mode": request.mode,
                   "created_at": now(), "updated_at": now(), "video_url": None,
                   "download_url": None, "poster_url": None, "package_url": None,
                   "estimated_cost_usd": estimate, "guide_audio": True,
                   "publishable": False, "dispatched": False}
            directory = self.store.directory("jobs", identifier)
            write_json(directory / "creation.json", project)
            write_json(directory / "request.json", {**request.model_dump(), "kind": "creation"})
            self.store.put("creation_job", job)
            if request.mode == "cloud":
                self.store.put("creation_reservation", {
                    "id": identifier, "job_id": identifier, "amount_usd": request.max_cost_usd,
                    "status": "reserved", "created_at": now(), "updated_at": now(),
                })
            self.record("queued", job)
            self.events[identifier] = threading.Event()
            self.pending.put_nowait(identifier)
            return self.public(job)

    def _reservation(self, job, status):
        if job["mode"] == "cloud":
            try:
                value = self.store.get("creation_reservation", job["id"])
            except HTTPException:
                return
            value.update(status=status, updated_at=now())
            self.store.put("creation_reservation", value)

    def cancel(self, identifier):
        with self.store.lock:
            job = self.store.get("creation_job", identifier)
            if job["status"] not in TERMINAL:
                if event := self.events.get(identifier):
                    event.set()
                if job["status"] == "queued":
                    job.update(status="cancelled", message="Cancelled before dispatch")
                    self._reservation(job, "released")
                else:
                    job.update(message="Cancelling; provider charges may continue")
                job["updated_at"] = now()
                self.store.put("creation_job", job)
                self.record("cancel_requested", job)
            return self.public(job)

    def public(self, job):
        if job["status"] == "running":
            self.sync_provider(job)
        public = {key: job.get(key) for key in PUBLIC_JOB}
        public["publishable"] = False
        public["review_required"] = True
        public["draft"] = True
        current = self.store.get("creation", job["project_id"])
        public["stale"] = current["revision"] != job["project_revision"]
        if job["status"] == "running":
            try:
                path = self.store.owned_file(self.store.directory("jobs", job["id"]) / "progress.json")
                progress = json.loads(path.read_text())
                public["progress"] = max(0, min(99, int(progress["progress"])))
            except (HTTPException, OSError, ValueError, TypeError, KeyError):
                pass
        return public

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
            job = self.store.get("creation_job", identifier)
            if job["status"] != "queued":
                self.events.pop(identifier, None)
                return
            event = self.events[identifier]
            job.update(status="running", message="Starting guarded worker", dispatched=True,
                       updated_at=now())
            self.store.put("creation_job", job)
            self.record("dispatched", job)
            self.active = identifier
        directory = self.store.directory("jobs", identifier)
        supervised = False
        try:
            supervisor = self.supervisor
            if supervisor is None:
                from .supervisor import run_supervised
                supervisor = run_supervised
            command = [sys.executable, "-m", "studio.web.worker", "--project",
                       str(directory / "creation.json"), "--request",
                       str(directory / "request.json"), "--out", str(directory)]
            result = supervisor(command, cwd=REPO, log_path=directory / "worker.log",
                                cancel_event=event, timeout_seconds=900)
            supervised = True
            if event.is_set() or result.get("cancelled"):
                job.update(status="cancelled", message="Cancelled; provider charges may continue")
            elif result.get("returncode") != 0 or result.get("resource_stopped") or result.get("reason"):
                job.update(status="failed", message="Worker stopped",
                           error="Generation failed or resources are blocked; check status before retrying")
            else:
                output = json.loads(self.store.owned_file(directory / "result.json").read_text())
                if output.get("kind") != "creation":
                    raise ValueError("Worker kind mismatch")
                for filename in OUTPUTS:
                    self.store.owned_file(directory / filename)
                base = f"{PREFIX}/jobs/{identifier}"
                job.update(status="succeeded", progress=100,
                           message="Draft preview complete" if job["mode"] == "preview" else
                                   "Generation complete; human review required",
                           guide_audio=bool(output.get("guide_audio", True)),
                           video_url=base + "/video", download_url=base + "/download",
                           poster_url=base + "/poster", package_url=base + "/package")
            # returncode=None proves supervisor refused to launch; no provider submission occurred.
            self._reservation(job, "released" if result.get("returncode") is None else "unknown")
        except FatalCleanupError:
            self.stopping.set()
            job.update(status="failed", message="Queue stopped until server restart",
                       error="Worker cleanup is unverified; provider charges may need reconciliation")
            self._reservation(job, "unknown")
        except Exception:  # noqa: BLE001 -- worker/filesystem/provider errors stay private
            job.update(status="cancelled" if event.is_set() else "failed",
                       message="Worker did not complete", error="Generation failed; no output is ready")
            self._reservation(job, "unknown")
            if not supervised:
                self.stopping.set()
        finally:
            with self.store.lock:
                job["updated_at"] = now()
                self.store.put("creation_job", job)
                self.sync_provider(job)
                self.record(job["status"], job)
                self.active = None
                self.events.pop(identifier, None)

    def output(self, identifier, filename):
        job = self.store.get("creation_job", identifier)
        if job["status"] != "succeeded":
            raise HTTPException(409, "Output is not ready")
        return self.store.owned_file(self.store.directory("jobs", identifier) / filename)

    def package(self, identifier):
        with self.store.lock:
            directory = self.output(identifier, "video.mp4").parent
            job = self.public(self.store.get("creation_job", identifier))
            snapshot = json.loads(self.store.owned_file(directory / "creation.json").read_text())
            files = [self.store.owned_file(directory / name)
                     for name in ("video.mp4", "poster.png", "captions.srt", "description.txt")]
            if sum(path.stat().st_size for path in files) > 1024**3:
                raise HTTPException(413, "Bundle exceeds size limit")
            hashes = {}
            for path in files:
                with path.open("rb") as stream:
                    hashes[path.name] = hashlib.file_digest(stream, "sha256").hexdigest()
            plan = snapshot["plan"]
            metadata = {"project_id": job["project_id"], "project_revision": job["project_revision"],
                        "title": plan["title"], "description": plan["description"],
                        "hashtags": plan["hashtags"], "mode": job["mode"], "stale": job["stale"],
                        "made_for_kids": snapshot["brief"]["audience"] == "kids",
                        "platforms": snapshot["brief"]["platforms"], "ai_disclosure": True,
                        "publishable": False, "approved": False, "published": False,
                        "draft": True, "review_required": True, "sha256": hashes}
            # Safe metadata allowlist preserves source-resolution honesty without raw provider data.
            source = json.loads(self.store.owned_file(directory / "metadata.json").read_text())
            for key in ("width", "height", "seconds", "fps", "source_width", "source_height"):
                value = source.get(key)
                if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                    metadata[key] = value
            if "source_width" in metadata and "source_height" in metadata:
                metadata["source_resolution"] = {
                    "width": metadata["source_width"], "height": metadata["source_height"],
                    "label": "Source scaled/cropped for export; upscaling is not native HD",
                }
            documents = {"creation.json": snapshot, "brief.json": snapshot["brief"],
                         "plan.json": plan}
            encoded = {name: json.dumps(value, indent=2) for name, value in documents.items()}
            for name, value in encoded.items():
                hashes[name] = hashlib.sha256(value.encode("utf-8")).hexdigest()
            bundle = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024)  # noqa: SIM115
            try:
                with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    for path in files:
                        archive.write(path, path.name)
                    for name, value in encoded.items():
                        archive.writestr(name, value)
                    archive.writestr("metadata.json", json.dumps(metadata, indent=2))
                    archive.writestr("review.json", json.dumps({
                        "publishable": False, "review_required": True, "stale": job["stale"],
                        "note": "Human review of rights, accuracy, audience classification and media "
                                "is required. Preview is an animatic, not finished generated footage. "
                                "This package is not publication approval.",
                    }))
                bundle.seek(0)
            except BaseException:
                bundle.close()
                raise

        def chunks():
            try:
                while block := bundle.read(256 * 1024):
                    yield block
            finally:
                bundle.close()

        return StreamingResponse(chunks(), media_type="application/zip", headers={
            "Content-Disposition": 'attachment; filename="creator-review.zip"'})


def register_creator(app, store, *, supervisor=None, planner=None, cloud_status=None):
    """Register before the frontend catch-all mount; parent owns lifecycle/security."""
    service = CreatorService(store, supervisor=supervisor, planner=planner, cloud_status=cloud_status)
    router = APIRouter(prefix=PREFIX)

    @router.get("/status")
    def status():
        resources = resource_snapshot(store.root)
        return {"planner": service.planner_status(), "cloud": service.cloud_status(),
                "resources": {key: resources[key] for key in ("pressure", "free_disk_gb")},
                "active_job": service.active}

    @router.get("/projects")
    def projects():
        return {"projects": store.all("creation")}

    @router.post("/projects")
    def create(body: VideoBrief):
        validate_plan(body, None)
        with store.lock:
            value = store.put("creation", Creation(brief=body).model_dump())
            service.record("created", project_id=value["id"])
            return value

    @router.get("/projects/{project_id}")
    def get_project(project_id: str):
        return store.get("creation", project_id)

    @router.put("/projects/{project_id}")
    def save(project_id: str, body: SaveCreation):
        with store.lock:
            current = store.get("creation", project_id)
            if body.revision != current["revision"]:
                raise HTTPException(409, "Project revision is stale; reload before saving")
            if service.has_active(project_id):
                raise HTTPException(409, "Cannot edit a project with an active job or planning request")
            validate_plan(body.brief, body.plan)
            # An unchanged old plan cannot silently apply to a changed brief.
            if (body.brief.model_dump() != current["brief"] and body.plan is not None
                    and body.plan.model_dump() == current["plan"]):
                raise HTTPException(409, "Brief changed; clear or update the stale plan")
            current.update(brief=body.brief.model_dump(),
                           plan=body.plan.model_dump() if body.plan else None,
                           revision=current["revision"] + 1, updated_at=now())
            store.put("creation", current)
            service.record("saved", project_id=project_id)
            return current

    @router.post("/projects/{project_id}/plan")
    def plan(project_id: str):
        return service.plan(project_id)

    @router.post("/projects/{project_id}/jobs")
    def submit(project_id: str, body: CreationRender):
        return service.submit(project_id, body)

    @router.get("/jobs")
    def jobs(project_id: str | None = None):
        if project_id is not None:
            store.get("creation", project_id)
        return {"jobs": [service.public(job) for job in store.all("creation_job")
                         if project_id is None or job["project_id"] == project_id]}

    @router.get("/jobs/{job_id}")
    def job(job_id: str):
        return service.public(store.get("creation_job", job_id))

    @router.post("/jobs/{job_id}/cancel")
    def cancel(job_id: str):
        return service.cancel(job_id)

    @router.get("/jobs/{job_id}/video")
    def video(job_id: str, request: Request):
        from .api import media_response
        return media_response(service.output(job_id, "video.mp4"), request, "video/mp4")

    @router.get("/jobs/{job_id}/download")
    def download(job_id: str, request: Request):
        from .api import media_response
        return media_response(service.output(job_id, "video.mp4"), request, "video/mp4", True)

    @router.get("/jobs/{job_id}/poster")
    def poster(job_id: str, request: Request):
        from .api import media_response
        return media_response(service.output(job_id, "poster.png"), request, "image/png")

    @router.get("/jobs/{job_id}/package")
    def package(job_id: str):
        return service.package(job_id)

    app.include_router(router)
    return service
