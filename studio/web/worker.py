"""Supervised worker CLI implementing doc 30's atomic filesystem protocol."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from studio.web.providers import generate_character
from studio.web.renderer import atomic_json, render_project


def run(project: dict, request: dict, out: Path) -> dict:
    out = out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    # A retry may never expose a previous successful result as this attempt's result.
    (out / "result.json").unlink(missing_ok=True)
    last_progress = 0

    def progress(value: int, message: str) -> None:
        nonlocal last_progress
        last_progress = max(last_progress, min(100, int(value)))
        atomic_json(out / "progress.json", {"progress": last_progress, "message": message})

    progress(0, "Starting supervised worker")
    try:
        kind = request.get("kind", "render")
        if kind == "render":
            result = render_project(project, request, out, progress)
        elif kind == "creation":
            from .creator_media import render_creation
            result = render_creation(project, request, out, progress)
        elif kind == "character":
            character = next((c for c in project.get("characters", [])
                              if c.get("id") == request.get("character_id")), None)
            if character is None:
                raise ValueError("Character not found in snapshot")
            progress(10, "Drawing original character")
            result = generate_character(character, request.get("provider", "illustration"), out)
            atomic_json(out / "metadata.json", result)
        else:
            raise ValueError("Unsupported job kind")
        atomic_json(out / "result.json", result)
        progress(100, "Complete")
        return result
    except BaseException:
        (out / "result.json").unlink(missing_ok=True)
        progress(last_progress, "Worker failed; inspect worker log")
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        project = json.loads(args.project.read_text(encoding="utf-8"))
        request = json.loads(args.request.read_text(encoding="utf-8"))
        run(project, request, args.out)
    except Exception:  # noqa: BLE001 — CLI boundary logs failure and exits nonzero
        # Do not put host paths or raw provider errors into browser-facing progress JSON.
        import traceback
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
