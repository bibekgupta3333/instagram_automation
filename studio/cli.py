"""`studio` command line: channels, make, batch, approve, status."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from studio.config import BrandPackError, load_brands
from studio.contracts import JobStatus, VideoJob
from studio.pipeline.runner import (
    JobSpec,
    list_jobs,
    load_job,
    make_video,
    run_batch,
    verify_approval,
)
from studio.pipeline.runner import (
    approve as approve_job,
)
from studio.stages.base import StageError

app = typer.Typer(help="Local-first AI video studio (docs/).", no_args_is_help=True)
console = Console()

MockOpt = Annotated[bool, typer.Option("--mock", help="Offline: mock LLM, TTS, captions, images.")]
EngineOpt = Annotated[
    list[str] | None,
    typer.Option(
        "--engine",
        help="Override a stage engine, e.g. tts=mock, images=flux2-klein-4b, llm=mock.",
    ),
]
ForceOpt = Annotated[bool, typer.Option("--force", help="Ignore cache keys and re-run all stages.")]


def _engines(values: list[str] | None) -> dict[str, str]:
    out = {}
    for v in values or []:
        stage, sep, name = v.partition("=")
        if not sep or stage not in ("tts", "captions", "images", "llm"):
            raise typer.BadParameter(
                f"--engine expects tts|captions|images|llm=NAME, got {v!r}"
            )
        out[stage] = name
    return out


def _print_job(job: VideoJob) -> None:
    colour = {"failed": "red", "approved": "green", "rendered": "cyan"}.get(job.status, "white")
    console.print(f"[bold]{job.job_id}[/bold]  [{colour}]{job.status}[/{colour}]")
    console.print(f"  channel={job.channel} format={job.format} topic={job.topic!r}")
    if job.video_path:
        console.print(f"  video: {job.video_path}")
    if job.content_hash:
        console.print(f"  content_hash: {job.content_hash}")
    if job.approved_hash:
        ok = "valid" if verify_approval(job) else "VOID (content changed)"
        console.print(f"  approved by {job.approved_by} ({ok})")
    if job.fact_check:
        console.print(f"  fact_check: passed={job.fact_check.passed} model={job.fact_check.model}")
    for line in job.log[-6:]:
        console.print(f"  [dim]{line}[/dim]")


@app.command()
def channels() -> None:
    """List the brand packs."""
    try:
        brands = load_brands()
    except BrandPackError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    table = Table(title=f"{len(brands)} channels")
    for col in ("slug", "name", "handle", "presenter", "voice", "accuracy", "cadence"):
        table.add_column(col)
    for b in brands.values():
        presenter = f"{b.presenter}" + (f" ({b.avatar_name})" if b.avatar_name else "")
        table.add_row(
            b.slug, b.name, b.handle, presenter, b.voice.voice,
            "critical" if b.accuracy_critical else "-",
            f"{b.shorts_per_day:g}/day + {b.longform_per_week:g} long/wk",
        )
    console.print(table)


@app.command()
def make(
    channel: Annotated[str, typer.Option(help="Brand pack slug (see `studio channels`).")],
    topic: Annotated[str, typer.Option(help="Video topic.")],
    format: Annotated[str, typer.Option("--format", help="short (9:16) or long (16:9).")] = "short",
    mock: MockOpt = False,
    engine: EngineOpt = None,
    force: ForceOpt = False,
) -> None:
    """Make one video end to end."""
    if format not in ("short", "long"):
        raise typer.BadParameter("--format must be short or long")
    try:
        job = make_video(channel, topic, format, mock=mock, engines=_engines(engine), force=force)
    except BrandPackError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc
    _print_job(job)
    if job.status == JobStatus.FAILED:
        raise typer.Exit(1)


@app.command()
def batch(
    channel: Annotated[str, typer.Option(help="Brand pack slug.")],
    topics_file: Annotated[Path, typer.Option(exists=True, dir_okay=False, help="One topic per line.")],
    format: Annotated[str, typer.Option("--format")] = "short",
    mock: MockOpt = False,
    engine: EngineOpt = None,
    force: ForceOpt = False,
) -> None:
    """Make many videos, stage by stage across jobs (doc 14 §9)."""
    topics = [t.strip() for t in topics_file.read_text().splitlines()]
    topics = [t for t in topics if t and not t.startswith("#")]
    specs = [JobSpec(channel, t, format) for t in topics]
    jobs = run_batch(specs, mock=mock, engines=_engines(engine), force=force)
    for job in jobs:
        _print_job(job)
    if any(j.status == JobStatus.FAILED for j in jobs):
        raise typer.Exit(1)


@app.command()
def approve(
    job_id: Annotated[str, typer.Argument(help="Job id (see `studio status`).")],
    by: Annotated[str, typer.Option("--by", help="Reviewer name.")],
) -> None:
    """Approve the rendered video: binds approved_hash to the current content_hash."""
    try:
        job = load_job(job_id)
        console.print(f"content_hash: [bold]{job.content_hash}[/bold]")
        job = approve_job(job_id, by)
    except (FileNotFoundError, StageError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(f"[green]approved[/green] {job.job_id} by {job.approved_by}")


@app.command()
def status(job_id: Annotated[str | None, typer.Argument()] = None) -> None:
    """Show one job, or a table of all jobs."""
    if job_id:
        try:
            _print_job(load_job(job_id))
        except FileNotFoundError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
        return
    table = Table(title="jobs")
    for col in ("job_id", "channel", "format", "status", "hash", "approved"):
        table.add_column(col)
    for j in list_jobs():
        approved = (j.approved_by or "") + ("" if not j.approved_hash or verify_approval(j)
                                            else " (void)")
        table.add_row(j.job_id, j.channel, j.format, str(j.status),
                      (j.content_hash or "")[:12], approved)
    console.print(table)


track_app = typer.Typer(help="Tracking DB (output/pipeline.sqlite), rebuilt from job.json.",
                        no_args_is_help=True)
app.add_typer(track_app, name="track")

from studio.topics.cli import topics_app

app.add_typer(topics_app, name="topics")


@track_app.command("sync")
def track_sync(
    output_dir: Annotated[Path, typer.Option("--output-dir")] = Path("output"),
) -> None:
    """Rebuild the tracking DB from output/*/job.json (read-only on job files)."""
    from studio.pipeline.tracking import sync_from_jobs

    counts = sync_from_jobs(output_dir)
    console.print(", ".join(f"{k}={v}" for k, v in counts.items()))


@track_app.command("report")
def track_report(
    json_out: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Counts by channel/status, failed jobs, invalid approvals and drafts."""
    import json as _json
    import sqlite3

    from studio.pipeline.tracking import build_report

    try:
        rep = build_report()
    except sqlite3.Error as exc:
        console.print(f"[red]no tracking DB yet ({exc}); run `studio track sync`[/red]")
        raise typer.Exit(1) from exc
    if json_out:
        typer.echo(_json.dumps(rep, indent=2))
        return
    t = Table(title="jobs by channel / status")
    for col in ("channel", "status", "n"):
        t.add_column(col)
    for r in rep["by_channel_status"]:
        t.add_row(str(r["channel"]), str(r["status"]), str(r["n"]))
    console.print(t)
    for key, title in (("failed", "failed jobs"),
                       ("approval_invalid", "approved but approval not valid"),
                       ("drafts", "drafts")):
        rows = rep[key]
        if not rows:
            continue
        t = Table(title=title)
        for col in rows[0]:
            t.add_column(col, overflow="fold")
        for r in rows:
            t.add_row(*(str(v)[:160] if v is not None else "" for v in r.values()))
        console.print(t)


if __name__ == "__main__":
    app()
