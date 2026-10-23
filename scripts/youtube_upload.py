"""Private YouTube uploads for finished videos; a human reviews and clicks Publish (infra/YOUTUBE.md).

    uv run --no-sync python scripts/youtube_upload.py auth [--playlists]
    uv run --no-sync python scripts/youtube_upload.py approve RUN_DIR --by NAME [--meta META]
    uv run --no-sync python scripts/youtube_upload.py upload RUN_DIR --brand SLUG --meta META \
        [--playlist ID] [--schedule ISO] [--dry-run]
    uv run --no-sync python scripts/youtube_upload.py status [RUN_DIR] [--meta META]

RUN_DIR is a kids-song output folder (``run.json`` + ``video.mp4``, from scripts/kids_song/make.py)
or a pipeline job folder (``job.json``; approval comes from ``studio approve``).

``approve`` is for the HUMAN only. It needs an interactive terminal and a typed confirmation,
and binds the approval to the sha256 of the video, the thumbnail and the metadata (title,
description, tags, category, audience). Any later change voids it. Agents must never run it.

Uploads are always ``private``. ``--schedule`` (optional) sets YouTube's ``publishAt``: YouTube
then makes the video public at that time by itself. Without it, nothing goes public until the
user clicks Publish in YouTube Studio.

meta.json: {"title": str, "description": str, "tags": [str], "thumbnail": "thumb.jpg",
            "category_id": "24"|"27", "video": "9x16"|"16x9", "playlist": "PL..."}
Relative paths resolve against the meta.json folder.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from studio.config import BrandPackError, load_brand, load_dotenv_once
from studio.contracts import BrandPack, Platform, PublishRequest
from studio.publish import youtube
from studio.publish.gate import DIRECT_PRIVACY, publish_problems

HASH_SCHEME = "youtube-direct-v1"
FORMAT_PLATFORM = {"9x16": Platform.YT_SHORTS, "16x9": Platform.YT_LONG}
EXIT_BLOCKED = 3

WARNING = (
    "WARNING: approval is a human decision. Only the channel owner, having watched the whole "
    "video and read the metadata, may run this. AI agents must never run `approve`."
)


class CliError(RuntimeError):
    pass


# ---------------------------------------------------------------- loading


@dataclass
class Target:
    run_dir: Path
    kind: str  # "kids-run" | "job"
    video: Path
    platform: Platform
    meta: youtube.UploadMeta
    run: dict[str, Any]
    content_hash: str
    approved_hash: str
    approved_by: str | None
    job_id: str
    channel: str


def load_meta(path: Path, *, made_for_kids: bool) -> tuple[youtube.UploadMeta, dict[str, Any]]:
    if not path.is_file():
        raise CliError(f"meta file not found: {path}")
    raw = json.loads(path.read_text())
    unknown = set(raw) - {"title", "description", "tags", "thumbnail", "category_id", "video",
                          "playlist"}
    if unknown:
        raise CliError(f"meta.json: unknown keys {sorted(unknown)}")
    if not isinstance(raw.get("tags", []), list):
        raise CliError("meta.json: tags must be a list")
    thumb = raw.get("thumbnail")
    thumb_path = (path.parent / thumb).resolve() if thumb else None
    meta = youtube.UploadMeta(
        title=str(raw.get("title", "")),
        description=str(raw.get("description", "")),
        tags=[str(t) for t in raw.get("tags", [])],
        category_id=str(raw.get("category_id", youtube.DEFAULT_CATEGORY_ID)),
        thumbnail=thumb_path,
        playlist_id=raw.get("playlist") or None,
        made_for_kids=made_for_kids,
    )
    return meta, raw


def meta_digest(meta: youtube.UploadMeta) -> str:
    """Hash of the reviewed metadata. publish_at and playlist come from CLI flags, so excluded."""
    reviewed = {k: v for k, v in meta.canonical().items()
                if k not in {"publish_at", "playlist_id", "thumbnail"}}
    return hashlib.sha256(json.dumps(reviewed, sort_keys=True).encode()).hexdigest()


def content_hash_for(video_sha: str, meta: youtube.UploadMeta) -> str:
    thumb_sha = youtube.sha256_file(meta.thumbnail) if meta.thumbnail and \
        meta.thumbnail.is_file() else "-"
    raw = f"{HASH_SCHEME}|{video_sha}|{thumb_sha}|{meta_digest(meta)}"
    return hashlib.sha256(raw.encode()).hexdigest()


def resolve_target(run_dir: Path, meta_path: Path, brand: BrandPack) -> Target:
    run_dir = run_dir.resolve()
    run_json, job_json = run_dir / "run.json", run_dir / "job.json"
    if run_json.is_file():
        run = json.loads(run_json.read_text())
        if run.get("do_not_publish") or run.get("mock_clips"):
            raise CliError("run.json is marked do_not_publish / mock clips: never upload this")
        if bool(run.get("made_for_kids")) != brand.made_for_kids:
            raise CliError(f"run.json made_for_kids={run.get('made_for_kids')!r} does not match "
                           f"brand {brand.slug!r} ({brand.made_for_kids})")
        if run.get("ai_disclosure") is not True:
            raise CliError("run.json must have ai_disclosure: true")
        meta, raw = load_meta(meta_path, made_for_kids=brand.made_for_kids)
        fmt = str(raw.get("video", "9x16"))
        if fmt not in FORMAT_PLATFORM:
            raise CliError(f"meta.json video must be one of {sorted(FORMAT_PLATFORM)}")
        entry = (run.get("videos") or {}).get(fmt) or {}
        name = Path(entry.get("path") or ("video.mp4" if fmt == "9x16" else f"video-{fmt}.mp4"))
        video = run_dir / name.name
        if not video.is_file():
            raise CliError(f"video not found: {video}")
        chash = content_hash_for(youtube.sha256_file(video), meta)
        approval = next((a for a in run.get("approvals", []) if a.get("content_hash") == chash),
                        None)
        return Target(run_dir, "kids-run", video, FORMAT_PLATFORM[fmt], meta, run, chash,
                      approval["content_hash"] if approval else "",
                      approval.get("approved_by") if approval else None,
                      str(run.get("slug") or run_dir.name), brand.slug)
    if job_json.is_file():
        from studio.contracts import VideoJob
        from studio.pipeline.runner import verify_approval

        job = VideoJob.model_validate_json(job_json.read_text())
        if job.channel != brand.slug:
            raise CliError(f"job channel {job.channel!r} != --brand {brand.slug!r}")
        meta, _ = load_meta(meta_path, made_for_kids=brand.made_for_kids)
        ok = verify_approval(job)
        platform = Platform.YT_LONG if job.format == "long" else Platform.YT_SHORTS
        return Target(run_dir, "job", Path(job.video_path or ""), platform, meta, {},
                      job.content_hash or "", (job.approved_hash or "") if ok else "",
                      job.approved_by, job.job_id, job.channel)
    raise CliError(f"{run_dir} has neither run.json nor job.json")


def build_request(t: Target, brand: BrandPack) -> PublishRequest:
    return PublishRequest(
        job_id=t.job_id, channel=brand.slug, platforms=[t.platform],
        media_url=t.video.resolve().as_uri(), title=t.meta.title,
        caption_by_platform={t.platform.value: t.meta.description}, tags=list(t.meta.tags),
        scheduled_at=t.meta.publish_at, ai_disclosure=bool(brand.ai_disclosure),
        made_for_kids=bool(brand.made_for_kids), content_hash=t.content_hash,
        approved_hash=t.approved_hash,
    )


def parse_schedule(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CliError(f"--schedule must be ISO 8601 with a timezone: {exc}") from exc
    if dt.tzinfo is None:
        raise CliError("--schedule needs a timezone, e.g. 2026-11-01T09:00:00+00:00")
    return dt


def _brand(slug: str) -> BrandPack:
    try:
        return load_brand(slug)
    except BrandPackError as exc:
        raise CliError(str(exc)) from exc


# ---------------------------------------------------------------- commands


def cmd_auth(args: argparse.Namespace) -> int:
    load_dotenv_once()
    path = youtube.run_auth_flow(playlists=args.playlists)
    print(f"YouTube token cached at {path} (mode 600). Scopes: "
          f"{', '.join(youtube.scopes_for(args.playlists))}")
    return 0


def cmd_approve(args: argparse.Namespace, *, stdin=None, stdout=None) -> int:
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    print(WARNING, file=stdout)
    run_dir = Path(args.run_dir).resolve()
    if not (run_dir / "run.json").is_file():
        raise CliError("approve works on kids-run folders with run.json; for pipeline jobs a "
                       "human runs `studio approve JOB_ID --by NAME`")
    if not stdin.isatty():
        raise CliError("approve needs an interactive terminal: a human must type the "
                       "confirmation (refusing non-interactive input)")
    if not args.by.strip():
        raise CliError("--by must name the human approver")
    brand = _brand(args.brand)
    meta_path = Path(args.meta) if args.meta else run_dir / "meta.json"
    t = resolve_target(run_dir, meta_path, brand)
    errs = youtube.meta_problems(t.meta)
    if errs:
        raise CliError("fix meta.json first: " + "; ".join(errs))
    print(youtube.dumps({
        "video": str(t.video), "video_sha256": youtube.sha256_file(t.video),
        "platform": t.platform.value, "made_for_kids": t.meta.made_for_kids,
        "meta": t.meta.canonical(), "content_hash": t.content_hash,
    }), file=stdout)
    code = t.content_hash[:8]
    print(f"Type {code} to approve this exact video + metadata as {args.by!r}: ", end="",
          file=stdout, flush=True)
    if stdin.readline().strip() != code:
        raise CliError("confirmation did not match; nothing recorded")
    run = json.loads((run_dir / "run.json").read_text())
    run.setdefault("approvals", []).append({
        "content_hash": t.content_hash, "hash_scheme": HASH_SCHEME,
        "video": t.video.name, "video_sha256": youtube.sha256_file(t.video),
        "thumbnail_sha256": youtube.sha256_file(t.meta.thumbnail) if t.meta.thumbnail else None,
        "meta_sha256": meta_digest(t.meta), "brand": brand.slug,
        "approved_by": args.by.strip(), "approved_at": datetime.now(UTC).isoformat(),
        "via": "scripts/youtube_upload.py approve (interactive)",
    })
    run["approved"] = True
    (run_dir / "run.json").write_text(json.dumps(run, indent=1))
    print(f"approved by {args.by.strip()} at content hash {t.content_hash}", file=stdout)
    return 0


def cmd_upload(args: argparse.Namespace, *, service: Any = None,
               ledger: youtube.YouTubeLedger | None = None, stdout=None) -> int:
    stdout = stdout or sys.stdout
    load_dotenv_once()
    brand = _brand(args.brand)
    t = resolve_target(Path(args.run_dir), Path(args.meta), brand)
    if args.playlist:
        t.meta.playlist_id = args.playlist
    t.meta.publish_at = parse_schedule(args.schedule)
    request = build_request(t, brand)
    problems = publish_problems(request, brand=brand, direct_privacy=t.meta.privacy)
    problems += youtube.meta_problems(t.meta)
    if t.platform not in brand.platforms:
        problems.append(f"brand {brand.slug!r} does not list {t.platform.value}")
    if args.dry_run:
        plan = None if problems else youtube.request_plan(t.video, t.meta)
        print(youtube.dumps({"dry_run": True, "privacy": DIRECT_PRIVACY,
                             "gate": "OK" if not problems else "BLOCKED", "problems": problems,
                             "content_hash": t.content_hash, "calls": plan}), file=stdout)
        return EXIT_BLOCKED if problems else 0
    if problems:
        print("upload blocked:\n- " + "\n- ".join(problems), file=stdout)
        return EXIT_BLOCKED
    res = youtube.upload(t.video, t.meta, request=request, brand=brand, service=service,
                         ledger=ledger)
    if t.kind == "kids-run":
        run = json.loads((t.run_dir / "run.json").read_text())
        run["youtube"] = {"video_id": res.video_id, "privacy": DIRECT_PRIVACY,
                          "content_hash": t.content_hash,
                          "publish_at": t.meta.publish_at.isoformat() if t.meta.publish_at
                          else None, "uploaded_at": datetime.now(UTC).isoformat()}
        run["published"] = False  # the user publishes in YouTube Studio
        (t.run_dir / "run.json").write_text(json.dumps(run, indent=1))
    print(f"{'already uploaded' if res.reused else 'uploaded'} PRIVATE video {res.video_id} "
          f"(thumbnail {'set' if res.thumbnail_set else 'NOT set'}"
          f"{', playlist added' if res.playlist_added else ''}).\n"
          f"Review and click Publish: {res.studio_url}", file=stdout)
    return 0


def cmd_status(args: argparse.Namespace, *, ledger: youtube.YouTubeLedger | None = None,
               stdout=None) -> int:
    stdout = stdout or sys.stdout
    ledger = ledger or youtube.YouTubeLedger()
    out: dict[str, Any] = {"uploads": ledger.all()}
    if args.run_dir:
        brand = _brand(args.brand)
        meta = Path(args.meta) if args.meta else Path(args.run_dir) / "meta.json"
        t = resolve_target(Path(args.run_dir), meta, brand)
        out["run"] = {"content_hash": t.content_hash, "approved": bool(t.approved_hash),
                      "approved_by": t.approved_by, "ledger": ledger.get(t.content_hash)}
    print(youtube.dumps(out), file=stdout)
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("auth", help="run the OAuth Desktop flow and cache the token")
    a.add_argument("--playlists", action="store_true", help="also grant the youtube scope")
    p = sub.add_parser("approve", help="HUMAN ONLY: record approval bound to the video sha256")
    p.add_argument("run_dir")
    p.add_argument("--by", required=True)
    p.add_argument("--brand", default="plinkety-pond")
    p.add_argument("--meta")
    u = sub.add_parser("upload", help="private upload (+thumbnail, +playlist)")
    u.add_argument("run_dir")
    u.add_argument("--brand", required=True)
    u.add_argument("--meta", required=True)
    u.add_argument("--playlist")
    u.add_argument("--schedule", help="ISO 8601 publishAt; YouTube publishes it then")
    u.add_argument("--dry-run", action="store_true", help="print the exact API requests only")
    s = sub.add_parser("status", help="ledger rows and a run's approval state")
    s.add_argument("run_dir", nargs="?")
    s.add_argument("--brand", default="plinkety-pond")
    s.add_argument("--meta")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {"auth": cmd_auth, "approve": cmd_approve, "upload": cmd_upload,
                "status": cmd_status}
    try:
        return handlers[args.cmd](args)
    except (CliError, youtube.MetaInvalid, youtube.PrivacyViolation, youtube.NeedsReconcile,
            FileNotFoundError, PermissionError, NotImplementedError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
