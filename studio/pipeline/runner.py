"""Pipeline runner: one video end to end (`make_video`) or many, stage by stage (`run_batch`).

`run_batch` follows doc 14 §9.1: every job's script, then every job's audio, then every
image, then every render, so each heavy model loads once per batch rather than once per
video. Each stage's resident model is unloaded before the next stage starts.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from studio.config import Settings, get_settings, load_brand
from studio.contracts import BrandPack, ChatClient, JobStatus, VideoJob
from studio.stages import captions, fact_check, images, render, script, tts
from studio.stages.base import (
    DEFAULT_ENGINES,
    MOCK_ENGINES,
    StageContext,
    StageError,
    log,
    sha256_file,
)

StageFn = Callable[[VideoJob, BrandPack, StageContext], VideoJob]

STAGES: list[tuple[str, StageFn]] = [
    ("script", script.run),
    ("fact_check", fact_check.run),
    ("tts", tts.run),
    ("captions", captions.run),
    ("images", images.run),
    ("render", render.run),
]
UNLOAD: dict[str, Callable[[], None]] = {
    "tts": tts.unload, "captions": captions.unload, "images": images.unload,
}


@dataclass(frozen=True)
class JobSpec:
    channel: str
    topic: str
    format: str = "short"


# ---------------------------------------------------------------- clients and context


def get_chat_client(mock: bool) -> ChatClient:
    """Mock client offline; otherwise studio.llm.get_client(), imported lazily, with a
    fallback to the mock when the LLM package is not available."""
    from studio.pipeline.mock_llm import MockChatClient

    if mock:
        return MockChatClient()
    try:
        from studio.llm import get_client  # owned by the LLM package; may not exist yet
    except ImportError:
        import warnings

        warnings.warn("studio.llm.get_client not available; using the mock ChatClient",
                      stacklevel=2)
        return MockChatClient()
    return get_client()


def build_context(
    mock: bool,
    engines: dict[str, str] | None = None,
    chat: ChatClient | None = None,
    settings: Settings | None = None,
    force: bool = False,
) -> StageContext:
    settings = settings or get_settings()
    base = dict(MOCK_ENGINES if mock else {**DEFAULT_ENGINES, **settings.engines})
    base.update(engines or {})
    # `llm=mock` mocks only the text stages, e.g. real audio without a Bedrock key.
    mock_llm = mock or base.pop("llm", None) == "mock"
    return StageContext(
        settings=settings, chat=chat or get_chat_client(mock_llm), engines=base, force=force
    )


# ---------------------------------------------------------------- job persistence


def slugify(text: str, limit: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:limit].rstrip("-") or "untitled"


def job_id_for(channel: str, topic: str, fmt: str) -> str:
    digest = hashlib.sha256(f"{channel}|{fmt}|{topic.strip()}".encode()).hexdigest()[:8]
    return f"{channel}-{fmt}-{slugify(topic, 32)}-{digest}"


def job_path(job: VideoJob) -> Path:
    return Path(job.workdir) / "job.json"


def save_job(job: VideoJob) -> None:
    path = job_path(job)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(job.model_dump_json(indent=2))
    tmp.replace(path)


def load_job(job_id: str, settings: Settings | None = None) -> VideoJob:
    settings = settings or get_settings()
    path = Path(settings.output_dir) / job_id / "job.json"
    if not path.exists():
        raise FileNotFoundError(f"no job {job_id!r} under {settings.output_dir}")
    return VideoJob.model_validate_json(path.read_text())


def list_jobs(settings: Settings | None = None) -> list[VideoJob]:
    settings = settings or get_settings()
    jobs = []
    for path in sorted(Path(settings.output_dir).glob("*/job.json")):
        try:
            jobs.append(VideoJob.model_validate_json(path.read_text()))
        except ValueError:
            continue
    return sorted(jobs, key=lambda j: j.created_at)


def plan_job(
    channel: str, topic: str, fmt: str = "short", settings: Settings | None = None,
    job_id: str | None = None,
) -> VideoJob:
    """Create output/<job_id>/job.json, or resume it if it exists (job ids are deterministic)."""
    if fmt not in ("short", "long"):
        raise ValueError("format must be 'short' or 'long'")
    settings = settings or get_settings()
    load_brand(channel, settings.brand_dir)  # validate the channel early
    job_id = job_id or job_id_for(channel, topic, fmt)
    workdir = (Path(settings.output_dir) / job_id).resolve()
    if (workdir / "job.json").exists():
        job = VideoJob.model_validate_json((workdir / "job.json").read_text())
        if job.status == JobStatus.FAILED:
            job.status = JobStatus.PLANNED
            log(job, "runner: retrying failed job")
    else:
        workdir.mkdir(parents=True, exist_ok=True)
        job = VideoJob(job_id=job_id, channel=channel, format=fmt, topic=topic, workdir=workdir)
        log(job, "runner: job planned")
    save_job(job)
    return job


# ---------------------------------------------------------------- running


def run_stage(name: str, fn: StageFn, job: VideoJob, brand: BrandPack, ctx: StageContext) -> bool:
    """Run one stage, persist job.json, return False if the job failed."""
    ctx.trace.append((name, job.job_id))
    try:
        job = fn(job, brand, ctx)
    except StageError as exc:
        job.status = JobStatus.FAILED
        log(job, f"{name}: FAILED: {exc}")
        save_job(job)
        return False
    save_job(job)
    return True


def make_video(
    channel: str,
    topic: str,
    format: str = "short",
    mock: bool = False,
    engines: dict[str, str] | None = None,
    *,
    chat: ChatClient | None = None,
    settings: Settings | None = None,
    force: bool = False,
    job_id: str | None = None,
    ctx: StageContext | None = None,
) -> VideoJob:
    ctx = ctx or build_context(mock, engines, chat, settings, force)
    job = plan_job(channel, topic, format, ctx.settings, job_id)
    brand = load_brand(channel, ctx.settings.brand_dir)
    try:
        for name, fn in STAGES:
            ok = run_stage(name, fn, job, brand, ctx)
            if name in UNLOAD:
                UNLOAD[name]()  # one heavy model resident at a time (doc 14 §9.1)
            if not ok:
                break
    finally:
        for unload in UNLOAD.values():
            unload()
    return job


def run_batch(
    jobs: Sequence[VideoJob | JobSpec] | Iterable[VideoJob | JobSpec],
    mock: bool = False,
    engines: dict[str, str] | None = None,
    *,
    chat: ChatClient | None = None,
    settings: Settings | None = None,
    force: bool = False,
    ctx: StageContext | None = None,
) -> list[VideoJob]:
    ctx = ctx or build_context(mock, engines, chat, settings, force)
    planned: list[VideoJob] = [
        j if isinstance(j, VideoJob) else plan_job(j.channel, j.topic, j.format, ctx.settings)
        for j in jobs
    ]
    brands = {j.channel: load_brand(j.channel, ctx.settings.brand_dir) for j in planned}
    alive = {j.job_id for j in planned if j.status != JobStatus.FAILED}
    for name, fn in STAGES:
        for job in planned:
            if job.job_id in alive and not run_stage(name, fn, job, brands[job.channel], ctx):
                alive.discard(job.job_id)
        if name in UNLOAD:
            UNLOAD[name]()  # one heavy model resident at a time (doc 14 §9.1)
    return planned


# ---------------------------------------------------------------- approval


def verify_approval(job: VideoJob) -> bool:
    """True only if approved, and the file on disk still matches the approved hash."""
    if job.status != JobStatus.APPROVED or not job.approved_hash or not job.video_path:
        return False
    if not Path(job.video_path).exists():
        return False
    return render.compute_content_hash(job) == job.approved_hash == job.content_hash


def approve(job_id: str, by: str, settings: Settings | None = None) -> VideoJob:
    job = load_job(job_id, settings)
    if not job.video_path or not Path(job.video_path).exists() or not job.content_hash:
        raise StageError(f"{job_id}: nothing rendered to approve")
    if job.status == JobStatus.FAILED:
        raise StageError(f"{job_id}: job failed; fix and re-render before approving")
    current = render.compute_content_hash(job)
    if current != job.content_hash:
        raise StageError(f"{job_id}: video changed since render ({current[:12]} != "
                         f"{job.content_hash[:12]}); re-run make to re-render")
    job.approved_hash = job.content_hash
    job.approved_by = by
    job.status = JobStatus.APPROVED
    log(job, f"approved by {by} at hash {job.content_hash} (video sha256 "
             f"{sha256_file(Path(job.video_path))[:12]})")
    save_job(job)
    return job
