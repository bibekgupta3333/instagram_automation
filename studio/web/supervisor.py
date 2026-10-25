"""Thread-safe worker resource guard (doc 30).

All model/render commands must enter here in the server parent, without a nested
guard. Children, including ffmpeg, must retain the worker's process group.
Catch FatalCleanupError in the backend and mark its queue poisoned: exit could
not be verified. The lock remains held until server exit in that case; never
retry work in that server. No process-global signal handlers are installed.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import platform
import shutil
import signal
import subprocess
import time
from pathlib import Path
from threading import Event

_GIB = 1024**3
_POISONED_LOCKS: list[int] = []


class FatalCleanupError(RuntimeError):
    """Worker/group exit is unverified; backend MUST poison its queue until restart."""


def _linux_pressure() -> str:
    fields = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        fields[key] = int(value.split()[0]) * 1024
    total, available = fields["MemTotal"], fields["MemAvailable"]
    if total <= 0 or not 0 <= available <= total:
        raise ValueError("Invalid available memory")
    budgets = [(total, available)]
    # Resolve our cgroup using its mount root (also works in cgroup namespaces).
    cgroups = Path("/proc/self/cgroup").read_text().splitlines()
    membership = next(
        (line[3:] for line in cgroups
         if line.startswith("0::")), None,
    )
    if membership is None and any("memory" in line.split(":")[1].split(",")
                                  for line in cgroups):
        raise ValueError("Memory-limited cgroup v1 requires migration to cgroup v2")
    if membership is not None:
        mounts = Path("/proc/self/mountinfo").read_text().splitlines()
        for line in mounts:
            before, after = line.split(" - ", 1)
            if after.split()[0] != "cgroup2":
                continue
            parts = before.split()
            root, mount = Path(parts[3]), Path(parts[4])
            # Kernel mount paths escape whitespace/backslashes with octal codes.
            for path in (str(root), str(mount)):
                if "\\" in path:
                    raise ValueError("Unsupported cgroup mount escape")
            relative = Path(membership).relative_to(root)
            group = mount / relative
            if ".." in relative.parts:
                raise ValueError("Invalid cgroup membership")
            while True:
                for name in ("memory.max", "memory.high"):
                    limit_path = group / name
                    try:
                        raw = limit_path.read_text().strip()
                    except FileNotFoundError:
                        # No memory controller at this level.
                        continue
                    if raw != "max":
                        limit = int(raw)
                        used = int((group / "memory.current").read_text().strip())
                        if limit < 0 or used < 0:
                            raise ValueError("Invalid cgroup memory")
                        budgets.append((limit, max(0, limit - used)))
                if group == mount:
                    break
                group = group.parent
            break
        else:
            raise ValueError("Cannot locate cgroup v2 mount")
    # Each ancestor limit applies, as does the host's available memory.
    if any(free < max(2 * _GIB, size * 0.1) for size, free in budgets):
        return "yellow"
    return "green"


def resource_snapshot(cwd: Path) -> dict:
    """Best-effort API telemetry, without model imports; unknown is never safe."""
    sample = {"platform": platform.system(), "free_disk_gb": None, "pressure": "unknown"}
    try:
        sample["free_disk_gb"] = shutil.disk_usage(cwd).free / 1_000_000_000
    except OSError:
        pass
    try:
        if sample["platform"] == "Darwin":
            result = subprocess.run(
                ["/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
                capture_output=True, text=True, check=True, timeout=3,
            )
            sample["pressure"] = {1: "green", 2: "yellow", 4: "red"}.get(
                int(result.stdout.strip()), "unknown",
            )
        elif sample["platform"] == "Linux":
            sample["pressure"] = _linux_pressure()
    except (OSError, ValueError, KeyError, IndexError, StopIteration, subprocess.SubprocessError):
        pass
    return sample


def _resource_sample(cwd: Path, output_path: Path) -> dict:
    sample = resource_snapshot(cwd)
    sample = {**sample, "timestamp": time.time(), "output_free_bytes": None}
    disk = sample["free_disk_gb"]
    if disk is not None and not math.isfinite(disk):
        sample["free_disk_gb"] = None
    # Job data can live on a separate EC2 volume. Sample both filesystems once.
    try:
        sample["output_free_bytes"] = shutil.disk_usage(output_path).free
    except OSError:
        pass
    return sample


def _unsafe_reason(sample: dict) -> str | None:
    disk = sample["free_disk_gb"]
    if disk is None or not math.isfinite(disk):
        return "Disk telemetry unavailable"
    if disk < 10:
        return "Disk free space below 10 GB"
    output_free = sample["output_free_bytes"]
    if output_free is None:
        return "Job disk telemetry unavailable"
    if output_free < 10_000_000_000:
        return "Job disk free space below 10 GB"
    if sample["pressure"] != "green":
        return f"Memory pressure {sample['pressure']}"
    return None


def _group_exists(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    return True  # PermissionError deliberately propagates: absence is unverified.


def _signal_group(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass


def _finish(proc: subprocess.Popen) -> None:
    """Reap leader and verify group absence, including after normal leader exit."""
    try:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            _signal_group(proc.pid, sig)
            deadline = time.monotonic() + 5
            while True:
                reaped = proc.poll() is not None
                if not _group_exists(proc.pid) and reaped:
                    proc.wait(timeout=0)
                    return
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
        raise FatalCleanupError("Worker process group did not exit after SIGKILL")
    except (OSError, subprocess.SubprocessError) as exc:
        raise FatalCleanupError("Cannot verify worker process group exit") from exc


def run_supervised(
    command: list[str], *, cwd: Path, log_path: Path, cancel_event: Event,
    timeout_seconds: int = 900,
) -> dict:
    """Run trusted argv, returning only after cleanup; fatal cleanup poisons queue.

    Busy/unsafe starts return returncode=None and resource_stopped=True. A normal
    exit has reason=None. Timeout is a failure, not a resource stop. Launch and
    filesystem failures return a generic reason without exposing paths/secrets.
    The flock inode is shared with scripts/run_guarded.py and is never unlinked.
    Observations append to log_path.with_suffix('.resources.jsonl'), flushed at
    start, each poll and final cleanup; these are samples, not a peak guarantee.
    """
    if not command or isinstance(command, (str, bytes)):
        raise ValueError("command must be a nonempty argv list")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive and finite")
    cwd = Path(cwd).resolve()
    result = {"returncode": None, "reason": None, "resource_stopped": False, "cancelled": False}
    lock = None
    proc = None
    poisoned = False
    resource_log = None

    def sample(phase: str) -> str | None:
        reading = _resource_sample(cwd, log_path.parent)
        reading["phase"] = phase
        resource_log.write(json.dumps(reading, allow_nan=False) + "\n")
        resource_log.flush()
        return _unsafe_reason(reading)

    try:
        if cancel_event.is_set():
            result.update(reason="Cancelled", cancelled=True)
            return result
        output = cwd / "output"
        output.mkdir(parents=True, exist_ok=True)
        lock = os.open(output / ".guarded-run.lock", os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            result.update(reason="Another guarded job is running", resource_stopped=True)
            return result
        log_path = Path(log_path)
        if not log_path.is_absolute():
            log_path = cwd / log_path
        log_path.parent.mkdir(parents=True, exist_ok=True)
        resource_log = log_path.with_suffix(".resources.jsonl").open("a", encoding="utf-8")
        reason = sample("start")
        if reason:
            result.update(reason=reason, resource_stopped=True)
            return result
        env = os.environ.copy()
        for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                    "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS", "STUDIO_CPU_THREADS",
                    "BLIS_NUM_THREADS", "RAYON_NUM_THREADS"):
            env[key] = "3"
        env.update(
            HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_DATASETS_OFFLINE="1",
            STUDIO_ALLOW_MODEL_DOWNLOAD="0", TOKENIZERS_PARALLELISM="false",
        )
        with log_path.open("ab") as log:
            if cancel_event.is_set():
                result.update(reason="Cancelled", cancelled=True)
                return result
            started = time.monotonic()
            proc = subprocess.Popen(
                command, cwd=cwd, env=env, start_new_session=True,
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            )
            try:
                while True:
                    if cancel_event.is_set():
                        result.update(reason="Cancelled", cancelled=True)
                        break
                    if time.monotonic() - started >= timeout_seconds:
                        result["reason"] = "Worker timed out"
                        break
                    reason = sample("poll")
                    if reason:
                        result.update(reason=reason, resource_stopped=True)
                        break
                    if proc.poll() is not None:
                        break
                    cancel_event.wait(timeout=1)
            finally:
                try:
                    _finish(proc)
                except BaseException as exc:
                    poisoned = True
                    if isinstance(exc, FatalCleanupError):
                        raise
                    raise FatalCleanupError("Worker cleanup interrupted; exit unverified") from exc
            result["returncode"] = proc.returncode
            if result["reason"] is None and proc.returncode != 0:
                result["reason"] = "Worker exited unsuccessfully"
    except (OSError, subprocess.SubprocessError):
        result["reason"] = "Worker launch or supervision failed"
    finally:
        if resource_log is not None:
            try:
                try:
                    reason = sample("final")
                    if reason and result["reason"] is None:
                        result.update(reason=reason, resource_stopped=True)
                finally:
                    resource_log.close()
            except Exception:  # noqa: BLE001 - final telemetry must not mask fatal cleanup
                # Never mask a fatal cleanup exception or release its pinned lock.
                if result["reason"] is None:
                    result.update(reason="Resource sampling failed", resource_stopped=True)
        if lock is not None:
            if poisoned:
                _POISONED_LOCKS.append(lock)
            else:
                os.close(lock)
    return result
