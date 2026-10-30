# ruff: noqa: B008
"""``studio topics`` sub-app (mounted by studio/cli.py): fetch | triage | list | pick."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import typer

from studio.pipeline.pipeline_schema import connect

topics_app = typer.Typer(help="Trend intake, topic triage and day-plan picking (doc 37).",
                         no_args_is_help=True)

FIXTURE = Path(__file__).parent / "fixtures" / "sample_trends.xml"


@topics_app.command("fetch")
def fetch(
    source: str = typer.Option("trends", help="trends | library | file"),
    region: str = typer.Option("US"),
    file: Path | None = typer.Option(None, "--file", help="topics file for --source file"),
    mock: bool = typer.Option(False, "--mock", help="use the bundled sample RSS (no network)"),
) -> None:
    """Fetch raw candidates into trend_candidates."""
    from studio.topics.intake import (
        FileSource,
        GoogleTrendsRSS,
        LibrarySource,
        parse_rss,
        save_candidates,
    )

    if source == "trends":
        rows = parse_rss(FIXTURE.read_bytes(), region=region) if mock else GoogleTrendsRSS(region).fetch()
    elif source == "library":
        rows = LibrarySource().fetch()
    elif source == "file":
        if not file:
            raise typer.BadParameter("--file is required for --source file")
        rows = FileSource(file, region).fetch()
    else:
        raise typer.BadParameter("source must be trends, library or file")
    con = connect()
    new = save_candidates(rows, con)
    typer.echo(f"fetched {len(rows)} item(s) from {source}; {new} new candidate(s)")


@topics_app.command("triage")
def triage(
    mock_llm: bool = typer.Option(False, "--mock-llm", help="offline keyword 'LLM' (no backend)"),
    limit: int | None = typer.Option(None, help="max candidates to triage"),
) -> None:
    """Classify untriaged candidates and write topics (proposed / rejected)."""
    from studio.config import load_brands
    from studio.topics.triage import make_mock_client, triage_pending

    if mock_llm:
        client = make_mock_client()
    else:
        from studio.llm.client import get_client
        client = get_client()
    con = connect()
    rep = triage_pending(con, client, load_brands(), limit=limit)
    typer.echo(f"triaged {len(rep.results)}: {rep.accepted} proposed, "
               f"{len(rep.results) - rep.accepted} rejected, {rep.deferred} deferred")
    for k, n in sorted(rep.reject_counts().items()):
        typer.echo(f"  rejected class {k}: {n}")
    for k, n in sorted(rep.reason_counts().items()):
        typer.echo(f"  reason {k}: {n}")


@topics_app.command("list")
def list_topics(
    status: str | None = typer.Option(None, "--status"),
    klass: str | None = typer.Option(None, "--class"),
    limit: int = typer.Option(50),
) -> None:
    """List topics (never prints raw trend terms for rejected rows beyond the safety metadata)."""
    con = connect()
    q, args = "SELECT id, channel, class, status, reject_reason, topic_text FROM topics WHERE 1=1", []
    if status:
        q += " AND status=?"; args.append(status)
    if klass:
        q += " AND class=?"; args.append(klass)
    for r in con.execute(q + " ORDER BY id DESC LIMIT ?", (*args, limit)):
        typer.echo(f"{r['id']:>4} {r['status']:<18} {r['class'] or '-':<20} {r['channel'] or '-':<22} "
                   f"{r['reject_reason'] or ''} | {r['topic_text']}")


@topics_app.command("pick")
def pick(
    per_channel: int = typer.Option(1, help="topics per channel"),
    daily_cap: int | None = typer.Option(None, help="env STUDIO_DAILY_CAP, default 20, ceiling 1000"),
    day: str | None = typer.Option(None, "--date", help="YYYY-MM-DD, default today"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Write output/plans/<date>.yaml from proposed topics ({channel, topic, format})."""
    from studio.config import load_brands
    from studio.topics.pick import daily_cap as cap_of
    from studio.topics.pick import pick_topics, plan_path, read_plan, write_plan

    d = datetime.strptime(day, "%Y-%m-%d").date() if day else date.today()
    cap = cap_of(daily_cap)
    con = connect()
    rows = [dict(r) for r in con.execute(
        "SELECT t.id, t.channel, t.topic_text, t.format, COALESCE(c.volume,0) AS volume FROM topics t "
        "LEFT JOIN trend_candidates c ON c.id=t.candidate_id WHERE t.status='proposed' "
        "ORDER BY volume DESC, t.id")]
    path = plan_path(d)
    existing = read_plan(path)
    picked = pick_topics(rows, load_brands(), per_channel=per_channel, cap=cap, existing=existing)
    if not dry_run and picked:
        write_plan(path, existing + picked)
        con.executemany("UPDATE topics SET status='approved_for_batch' WHERE id=?", [(p["id"],) for p in picked])
        con.commit()
    typer.echo(f"picked {len(picked)} (cap {cap}, {len(existing)} already planned) -> {path}")
    for p in picked:
        typer.echo(f"  {p['channel']}: {p['topic']}")
