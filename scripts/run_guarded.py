"""Run a process on macOS only while memory pressure is green and disk is safe.

Usage: python scripts/run_guarded.py -- .venv/bin/studio make ...
Samples once per second; cannot guarantee the system never spikes between samples.
Only terminates the launched process group, never unrelated applications.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path


def pressure_level() -> int:
    result = subprocess.run(
        ["/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
        capture_output=True, text=True, check=True, timeout=3,
    )
    level = int(result.stdout.strip())
    if level not in (1, 2, 4):
        raise ValueError("Unknown macOS memory pressure level")
    return level


def safety_check(
    path: Path, min_free_gb: float = 10, record: Callable[[dict], None] | None = None,
) -> dict:
    sample = {
        "time": time.time(), "pressure": None, "free_gb": None,
    }
    try:
        free_bytes = shutil.disk_usage(path).free
        sample.update(free_bytes=free_bytes, free_gb=round(free_bytes / 1_000_000_000, 2))
        sample["pressure"] = pressure_level()
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        sample["error"] = str(exc)
        if record is not None:
            record(sample)
        raise
    if record is not None:
        record(sample)  # include the unsafe reading that caused a stop, not just green samples
    if sample["pressure"] != 1:
        raise RuntimeError("Memory pressure is not green; stopped workload")
    if free_bytes < min_free_gb * 1_000_000_000:
        raise RuntimeError("Disk free space below safety floor; stopped workload")
    return sample


def stop_group(proc: subprocess.Popen) -> None:
    # Kill descendants too (mflux and ffmpeg inherit this new session).
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass  # The group may be gone while its leader still needs reaping.
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--threads", type=int, default=3)
    parser.add_argument("--log", type=Path, default=Path("output/memory-pressure.jsonl"))
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command or not 1 <= args.threads <= 3:
        parser.error("Supply a command after -- and --threads between 1 and 3")
    proc = None
    resources = ExitStack()
    previous_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    return_code = 75

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    try:
        for sig in previous_handlers:
            signal.signal(sig, interrupted)
        args.log.parent.mkdir(parents=True, exist_ok=True)
        Path("output").mkdir(exist_ok=True)
        lock = os.open("output/.guarded-run.lock", os.O_CREAT | os.O_WRONLY, 0o600)
        resources.callback(os.close, lock)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another guarded job is running; wait for its GPU slot") from None
        with args.log.open("a") as log:
            def record(reading: dict) -> None:
                log.write(json.dumps(reading) + "\n")
                log.flush()

            def sample() -> None:
                safety_check(Path.cwd(), record=record)

            sample()  # Fail closed when telemetry is unavailable, before launching work.
            env = os.environ.copy()
            for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                        "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS", "STUDIO_CPU_THREADS"):
                env[key] = str(args.threads)
            env["TOKENIZERS_PARALLELISM"] = "false"
            env["HF_HUB_OFFLINE"] = "1"
            env["TRANSFORMERS_OFFLINE"] = "1"
            env["STUDIO_ALLOW_MODEL_DOWNLOAD"] = "0"
            proc = subprocess.Popen(command, env=env, start_new_session=True)
            while proc.poll() is None:
                sample()
                time.sleep(1)
            sample()
            return_code = proc.returncode
    except (OSError, ValueError, subprocess.SubprocessError, RuntimeError, KeyboardInterrupt) as exc:
        print(f"Resource guard: {exc}", file=sys.stderr)
    finally:
        # A second interrupt must not strand descendants or release their slot early.
        for sig in previous_handlers:
            signal.signal(sig, signal.SIG_IGN)
        try:
            if proc is not None:
                stop_group(proc)
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"Resource guard cleanup: {exc}", file=sys.stderr)
            return_code = 75
        finally:
            try:
                resources.close()
            finally:
                for sig, handler in previous_handlers.items():
                    signal.signal(sig, handler)
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
