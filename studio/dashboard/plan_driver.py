"""Run a day plan through ``scripts/run_guarded.py`` one (channel, format) batch at a time.

``studio batch`` takes ONE channel and one topics file per invocation, so a day plan is split
into groups. ``build_invocations`` (used by the dashboard server) writes the topics files and
returns fully built argv lists; ``main`` (the action's child process) runs a manifest of them
sequentially. Every invocation goes through the resource guard; nothing here bypasses it.
"""

from __future__ import annotations

import argparse
import json
import signal
import subprocess
import sys
from pathlib import Path

GUARD_EXIT = 75  # run_guarded.py: lock held, telemetry unavailable or pressure not green


def group_entries(entries: list[dict]) -> list[tuple[str, str, list[str]]]:
    groups: dict[tuple[str, str], list[str]] = {}
    for e in entries:
        groups.setdefault((e["channel"], e.get("format") or "short"), []).append(e["topic"])
    return [(ch, fmt, topics) for (ch, fmt), topics in groups.items()]


def build_invocations(entries: list[dict], *, python: str, studio: str, run_guarded: str,
                      topics_dir: Path, tag: str, mock: bool, llm: str | None) -> list[dict]:
    topics_dir.mkdir(parents=True, exist_ok=True)
    out = []
    for i, (channel, fmt, topics) in enumerate(group_entries(entries)):
        tf = topics_dir / f"{tag}.{i}.topics.txt"
        tf.write_text("\n".join(topics) + "\n")
        argv = [python, run_guarded, "--", studio, "batch", "--channel", channel,
                "--topics-file", str(tf), "--format", fmt]
        if mock:
            argv.append("--mock")
        elif llm == "mock":
            argv += ["--engine", "llm=mock"]
        out.append({"argv": argv, "channel": channel, "format": fmt, "topics": len(topics)})
    return out


def _valid(argv: object) -> bool:
    return (isinstance(argv, list) and len(argv) > 4 and all(isinstance(a, str) for a in argv)
            and argv[1].endswith("run_guarded.py") and argv[2] == "--")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest", type=Path, required=True)
    ap.add_argument("--cwd", type=Path, default=Path.cwd())
    args = ap.parse_args(argv)
    invs = json.loads(args.manifest.read_text()).get("invocations", [])
    if not all(_valid(i.get("argv")) for i in invs):
        print("plan_driver: refusing a manifest entry that is not a run_guarded.py invocation",
              file=sys.stderr)
        return 2
    state: dict = {"stop": False, "proc": None}

    def on_term(signum, frame):
        state["stop"] = True
        if state["proc"] is not None:
            state["proc"].terminate()  # run_guarded stops its own process group

    signal.signal(signal.SIGTERM, on_term)
    worst = 0
    for n, inv in enumerate(invs, 1):
        if state["stop"]:
            return 143
        print(f"[plan_driver] {n}/{len(invs)} {inv['argv'][4:]}", flush=True)
        state["proc"] = subprocess.Popen(inv["argv"], cwd=args.cwd)  # no shell
        rc = state["proc"].wait()
        state["proc"] = None
        if rc == GUARD_EXIT:
            print("[plan_driver] guard stopped the run; not continuing", flush=True)
            return rc
        worst = max(worst, rc)
    return 143 if state["stop"] else worst


if __name__ == "__main__":
    raise SystemExit(main())
