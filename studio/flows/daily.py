"""Prefect 3 flow skeleton (docs 16 §4): a daytime text phase and an overnight GPU phase.

Prefect is optional (``uv sync --extra flows``). Without it this module still imports: the
decorators become pass-throughs, the flows run as plain functions, and ``PREFECT_MISSING``
explains what is unavailable (scheduling, retries, concurrency limits).

Lanes (Prefect global concurrency limits; create them once):
    prefect gcl create gpu --limit 1      # local LLM / ComfyUI / TTS / alignment share 24 GB
    prefect gcl create api --limit 8
    prefect gcl create publish --limit 1
GPU tasks are also tagged ``gpu`` so a tag-based limit works too:
    prefect concurrency-limit create gpu 1
"""

from __future__ import annotations

import contextlib
import importlib
import inspect
import logging
import os
from collections.abc import Callable, Iterator
from datetime import date
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

try:  # Prefect is an optional extra
    from prefect import flow, task  # type: ignore[import-not-found]

    PREFECT_AVAILABLE = True
    PREFECT_MISSING = ""
except ImportError:  # pragma: no cover - exercised when prefect is absent
    PREFECT_AVAILABLE = False
    PREFECT_MISSING = (
        "Prefect is not installed (`uv sync --extra flows`). Flows run as plain functions: "
        "no schedules, retries, caching or gpu/api concurrency limits."
    )

    def _passthrough(*dargs: Any, **_dkw: Any) -> Any:
        if len(dargs) == 1 and callable(dargs[0]) and not _dkw:
            return dargs[0]
        return lambda fn: fn

    flow = _passthrough  # type: ignore[assignment]
    task = _passthrough  # type: ignore[assignment]


@contextlib.contextmanager
def lane(name: str) -> Iterator[None]:
    """Hold one slot of a Prefect global concurrency limit (no-op without Prefect)."""
    if not PREFECT_AVAILABLE:
        yield
        return
    from prefect.concurrency.sync import concurrency  # type: ignore[import-not-found]

    with concurrency(name, occupy=1):
        yield


def _runner() -> Callable[..., Any]:
    """Import ``studio.pipeline.runner.run_batch`` lazily (owned by the pipeline side)."""
    try:
        mod = importlib.import_module("studio.pipeline.runner")
    except ImportError as exc:
        raise RuntimeError("studio.pipeline.runner is not available yet") from exc
    return mod.run_batch


def call_run_batch(jobs: list[Any], **kwargs: Any) -> Any:
    """Call ``run_batch(jobs, ...)`` passing only the keyword arguments its signature accepts
    (e.g. a future ``phase=``), so this skeleton tolerates signature drift."""
    fn = _runner()
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return fn(jobs, **kwargs)
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return fn(jobs, **kwargs)
    accepted = {k: v for k, v in kwargs.items() if k in params}
    dropped = sorted(set(kwargs) - set(accepted))
    if dropped:
        log.info("run_batch does not accept %s; running the full stage chain", dropped)
    return fn(jobs, **accepted)


def _job_specs(items: list[Any]) -> list[Any]:
    """dicts {channel, topic, format} -> studio.pipeline.runner.JobSpec; others pass through."""
    if not any(isinstance(i, dict) for i in items):
        return list(items)
    JobSpec = importlib.import_module("studio.pipeline.runner").JobSpec
    return [JobSpec(**i) if isinstance(i, dict) else i for i in items]


def load_day_plan(day: date) -> list[dict[str, Any]]:
    """Jobs for a day from ``$STUDIO_PLAN_DIR/<YYYY-MM-DD>.yaml`` (default output/plans):
    a YAML list of {channel, topic, format}. Missing file -> no jobs."""
    import yaml

    path = Path(os.environ.get("STUDIO_PLAN_DIR", "output/plans")) / f"{day.isoformat()}.yaml"
    if not path.exists():
        log.warning("no plan for %s at %s", day, path)
        return []
    return list(yaml.safe_load(path.read_text()) or [])


@task(retries=2, retry_delay_seconds=60, tags=["api"])
def text_batch(jobs: list[Any], mock: bool) -> Any:
    # Text phase: one local LLM stays loaded; Bedrock calls go via the api lane.
    # Until run_batch takes a phase, the full chain runs (stages are idempotent/cached).
    return call_run_batch(_job_specs(jobs), mock=mock, phase="text")


@task(retries=1, retry_delay_seconds=120, tags=["gpu"])
def gpu_batch(jobs: list[Any], mock: bool) -> Any:
    with lane("gpu"):
        return call_run_batch(_job_specs(jobs), mock=mock, phase="render")


@task(tags=["gpu"])
def unload_local_llm() -> None:
    """18:00 unload: free unified memory before the GPU queue (docs 16 §4, 14)."""
    base = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
    model = os.environ.get("STUDIO_LOCAL_MODEL", "qwen3.5:9b")
    import httpx

    try:
        httpx.post(f"{base}/api/generate", json={"model": model, "keep_alive": 0}, timeout=30)
    except httpx.HTTPError as exc:
        log.warning("could not unload %s: %s", model, exc)


@flow(name="daily-text", log_prints=True)
def daily_text_flow(
    day: date | None = None, jobs: list[Any] | None = None, mock: bool = False
) -> Any:
    """08:00-18:00: script, fact-check, package and QA for the day's jobs."""
    if PREFECT_MISSING:
        log.warning(PREFECT_MISSING)
    day = day or date.today()
    jobs = load_day_plan(day) if jobs is None else jobs
    return text_batch(jobs, mock)


@flow(name="overnight-gpu", log_prints=True)
def overnight_gpu_flow(
    day: date | None = None,
    jobs: list[Any] | None = None,
    mock: bool = False,
    unload_first: bool = True,
) -> Any:
    """18:00 onwards: unload the LLM, then TTS -> align -> visuals -> render, one at a time."""
    if PREFECT_MISSING:
        log.warning(PREFECT_MISSING)
    if unload_first and not mock:
        unload_local_llm()
    day = day or date.today()
    jobs = load_day_plan(day) if jobs is None else jobs
    return gpu_batch(jobs, mock)


def serve() -> None:  # pragma: no cover - needs Prefect and a running server
    """Register both flows with cron schedules (local time) and serve them."""
    if not PREFECT_AVAILABLE:
        raise RuntimeError(PREFECT_MISSING)
    from prefect import serve as prefect_serve  # type: ignore[import-not-found]

    prefect_serve(
        daily_text_flow.to_deployment(name="daily-text", cron="0 8 * * *"),
        overnight_gpu_flow.to_deployment(name="overnight-gpu", cron="0 18 * * *"),
    )


if __name__ == "__main__":  # pragma: no cover
    serve()
