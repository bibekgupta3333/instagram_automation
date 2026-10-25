"""Offline supervisor tests: no workers, models, network or real group signals."""

import fcntl
import json
import os
import signal
import subprocess
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from studio.web import supervisor as s


@pytest.fixture
def rig(monkeypatch, tmp_path):
    clock = [0.0]
    monkeypatch.setattr(s.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(s.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    snapshot = Mock(return_value={"platform": "Darwin", "free_disk_gb": 20, "pressure": "green"})
    monkeypatch.setattr(s, "resource_snapshot", snapshot)
    monkeypatch.setattr(s.shutil, "disk_usage", lambda path: SimpleNamespace(free=20_000_000_000))
    proc = Mock(pid=31415, returncode=0)
    proc.poll.return_value = 0
    launch = Mock(return_value=proc)
    monkeypatch.setattr(s.subprocess, "Popen", launch)
    kill = Mock(side_effect=ProcessLookupError)
    monkeypatch.setattr(s.os, "killpg", kill)
    event = Mock(spec=Event)
    event.is_set.return_value = False
    event.wait.side_effect = lambda timeout: clock.__setitem__(0, clock[0] + timeout)
    state = SimpleNamespace(
        clock=clock, snapshot=snapshot, proc=proc, launch=launch, kill=kill, event=event,
        cwd=tmp_path, log_path=tmp_path / "job" / "worker.log",
    )
    yield state
    # Fatal tests intentionally pin the lock; close it only in test teardown.
    for fd in s._POISONED_LOCKS:
        os.close(fd)
    s._POISONED_LOCKS.clear()


def run(rig, **kwargs):
    return s.run_supervised(
        ["python", "-m", "studio.web.worker"], cwd=rig.cwd, log_path=rig.log_path,
        cancel_event=rig.event, **kwargs,
    )


def assert_lock_free(rig):
    with (rig.cwd / "output/.guarded-run.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_success_and_child_environment(rig, monkeypatch):
    monkeypatch.setenv("OMP_NUM_THREADS", "99")
    monkeypatch.setenv("STUDIO_ALLOW_MODEL_DOWNLOAD", "1")
    result = run(rig)
    assert result == {"returncode": 0, "reason": None, "resource_stopped": False,
                      "cancelled": False}
    options = rig.launch.call_args.kwargs
    assert options["start_new_session"] is True
    assert options["cwd"] == rig.cwd
    assert options["stdin"] == subprocess.DEVNULL
    assert options["stderr"] == subprocess.STDOUT
    assert options["stdout"].closed
    env = options["env"]
    for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS", "STUDIO_CPU_THREADS"):
        assert env[key] == "3"
    assert env["HF_HUB_OFFLINE"] == env["TRANSFORMERS_OFFLINE"] == "1"
    assert env["STUDIO_ALLOW_MODEL_DOWNLOAD"] == "0"
    assert os.environ["OMP_NUM_THREADS"] == "99"
    rig.proc.wait.assert_called_once_with(timeout=0)
    assert_lock_free(rig)


@pytest.mark.parametrize("pressure,disk", [
    ("yellow", 20), ("red", 20), ("unknown", 20), ("green", None),
    ("green", 9.999), ("green", float("nan")),
])
def test_unsafe_start_never_launches(rig, pressure, disk):
    rig.snapshot.return_value.update(pressure=pressure, free_disk_gb=disk)
    result = run(rig)
    assert result["resource_stopped"] and result["returncode"] is None
    rig.launch.assert_not_called()
    assert_lock_free(rig)


def test_disk_floor_exactly_ten_is_safe(rig):
    rig.snapshot.return_value["free_disk_gb"] = 10
    assert run(rig)["returncode"] == 0


@pytest.mark.parametrize("free", [9_999_999_999, None])
def test_job_filesystem_is_checked_independently(rig, monkeypatch, free):
    seen = []

    def usage(path):
        seen.append(path)
        if free is None:
            raise PermissionError
        return SimpleNamespace(free=free)

    monkeypatch.setattr(s.shutil, "disk_usage", usage)
    assert run(rig)["resource_stopped"]
    assert seen == [rig.log_path.parent, rig.log_path.parent]  # start and final
    rig.launch.assert_not_called()
    assert_lock_free(rig)


def test_busy_uses_existing_crossprocess_guard_lock(rig):
    output = rig.cwd / "output"
    output.mkdir()
    with (output / ".guarded-run.lock").open("a") as existing:
        fcntl.flock(existing, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert run(rig)["resource_stopped"] is True
        rig.launch.assert_not_called()
        rig.snapshot.assert_not_called()
        with pytest.raises(BlockingIOError):
            assert_lock_free(rig)
    assert_lock_free(rig)


def test_pre_cancelled(rig):
    rig.event.is_set.return_value = True
    assert run(rig)["cancelled"]
    rig.launch.assert_not_called()


def test_running_cancel_stops_and_reaps_group(rig):
    rig.proc.poll.side_effect = [None, 0]
    rig.event.is_set.side_effect = [False, False, False, True]
    result = run(rig)
    assert result["cancelled"] and not result["resource_stopped"]
    rig.event.wait.assert_called_once_with(timeout=1)
    assert rig.kill.call_args_list[0].args == (rig.proc.pid, signal.SIGTERM)
    rig.proc.wait.assert_called_once()
    assert_lock_free(rig)
    assert readings(rig)[-1]["phase"] == "final"


def test_timeout(rig):
    rig.proc.poll.side_effect = [None, 0]
    result = run(rig, timeout_seconds=1)
    assert "timed out" in result["reason"]
    assert not result["cancelled"] and not result["resource_stopped"]
    assert_lock_free(rig)
    assert readings(rig)[-1]["phase"] == "final"


@pytest.mark.parametrize("pressure", ["yellow", "red", "unknown"])
def test_pressure_change_stops_running_worker(rig, pressure):
    good = dict(rig.snapshot.return_value)
    rig.snapshot.side_effect = [good, good, dict(good, pressure=pressure), good]
    rig.proc.poll.side_effect = [None, 0]
    result = run(rig)
    assert result["resource_stopped"] and pressure in result["reason"]
    rig.event.wait.assert_called_once_with(timeout=1)
    rig.proc.wait.assert_called_once()


def test_running_output_disk_drop_stops_worker(rig, monkeypatch):
    monkeypatch.setattr(s.shutil, "disk_usage", Mock(side_effect=[
        SimpleNamespace(free=20_000_000_000), SimpleNamespace(free=9_000_000_000),
        SimpleNamespace(free=9_000_000_000),
    ]))
    result = run(rig)
    assert result["resource_stopped"] and "Job disk" in result["reason"]
    rig.proc.wait.assert_called_once()


def test_unreaped_leader_is_fatal_even_if_group_absent(rig):
    rig.proc.poll.return_value = None
    with pytest.raises(s.FatalCleanupError):
        run(rig, timeout_seconds=1)
    assert s._POISONED_LOCKS


def test_leader_exit_still_cleans_up_descendants_with_kill(rig):
    # Leader is already reaped but its child ignores TERM.
    killed = [False]

    def kill(pid, sig):
        assert pid == rig.proc.pid
        if sig == signal.SIGKILL:
            killed[0] = True
        elif sig == 0 and killed[0]:
            raise ProcessLookupError

    rig.kill.side_effect = kill
    assert run(rig)["returncode"] == 0
    assert killed[0]
    rig.proc.wait.assert_called_once()
    assert_lock_free(rig)


@pytest.mark.parametrize("failure", ["permission_signal", "permission_probe", "alive", "reap"])
def test_unverified_cleanup_is_fatal_and_pins_lock(rig, failure):
    if failure == "permission_signal":
        rig.kill.side_effect = PermissionError
    elif failure == "permission_probe":
        rig.kill.side_effect = lambda pid, sig: (
            (_ for _ in ()).throw(PermissionError()) if sig == 0 else None
        )
    elif failure == "alive":
        rig.kill.side_effect = None
    else:
        rig.proc.wait.side_effect = subprocess.TimeoutExpired("worker", 0)
    with pytest.raises(s.FatalCleanupError):
        run(rig)
    with pytest.raises(BlockingIOError):
        assert_lock_free(rig)
    assert run(rig)["resource_stopped"]
    assert rig.launch.call_count == 1


def test_launch_failure_releases_lock_without_group_signal(rig):
    rig.launch.side_effect = PermissionError
    assert "failed" in run(rig)["reason"]
    rig.kill.assert_not_called()
    assert_lock_free(rig)


def test_unexpected_monitor_exception_cleans_up_before_propagating(rig):
    rig.snapshot.side_effect = [
        rig.snapshot.return_value, RuntimeError("monitor"), rig.snapshot.return_value,
    ]
    with pytest.raises(RuntimeError, match="monitor"):
        run(rig)
    rig.proc.wait.assert_called_once()
    assert_lock_free(rig)


def test_nonzero_exit(rig):
    rig.proc.returncode = rig.proc.poll.return_value = 7
    assert run(rig)["returncode"] == 7
    assert run(rig)["reason"] == "Worker exited unsuccessfully"


def readings(rig):
    return [json.loads(line) for line in rig.log_path.with_suffix(".resources.jsonl")
            .read_text().splitlines()]


def test_resource_jsonl_uses_enforcement_samples_once_and_flushes_each_poll(rig, monkeypatch):
    monkeypatch.setattr(s.time, "time", lambda: 1_800_000_000 + rig.clock[0])
    disk_usage = Mock(return_value=SimpleNamespace(free=20_000_000_000))
    monkeypatch.setattr(s.shutil, "disk_usage", disk_usage)
    rig.proc.poll.side_effect = [None, 0, 0]

    def wait(timeout):
        # Poll must be visible while the worker is still running.
        assert [row["phase"] for row in readings(rig)] == ["start", "poll"]
        rig.clock[0] += timeout

    rig.event.wait.side_effect = wait
    assert run(rig)["returncode"] == 0
    rows = readings(rig)
    assert [row["phase"] for row in rows] == ["start", "poll", "poll", "final"]
    assert [row["timestamp"] for row in rows] == [
        1_800_000_000, 1_800_000_000, 1_800_000_001, 1_800_000_001,
    ]
    assert rig.snapshot.call_count == disk_usage.call_count == len(rows)
    for row in rows:
        assert row["platform"] == "Darwin"
        assert row["free_disk_gb"] == 20
        assert row["pressure"] == "green"
        assert row["output_free_bytes"] == 20_000_000_000


def test_resource_jsonl_appends_on_retry(rig):
    run(rig)
    run(rig)
    assert [row["phase"] for row in readings(rig)] == ["start", "poll", "final"] * 2


@pytest.mark.parametrize("pressure", ["yellow", "unknown"])
def test_unsafe_samples_persist_even_when_start_refused(rig, pressure):
    rig.snapshot.return_value["pressure"] = pressure
    assert run(rig)["resource_stopped"]
    assert [row["phase"] for row in readings(rig)] == ["start", "final"]
    assert all(row["pressure"] == pressure for row in readings(rig))


def test_output_telemetry_failure_is_json_null(rig, monkeypatch):
    monkeypatch.setattr(s.shutil, "disk_usage", Mock(side_effect=PermissionError))
    assert run(rig)["resource_stopped"]
    assert all(row["output_free_bytes"] is None for row in readings(rig))


def test_final_sample_enforces_unknown_pressure(rig):
    good = rig.snapshot.return_value
    rig.snapshot.side_effect = [good, good, dict(good, pressure="unknown")]
    result = run(rig)
    assert result["resource_stopped"] and "unknown" in result["reason"]
    assert readings(rig)[-1]["pressure"] == "unknown"


def test_final_record_failure_cannot_mask_fatal_cleanup(rig, monkeypatch):
    original = Path.open

    def open_path(path, *args, **kwargs):
        if path.suffix == ".jsonl":
            log = Mock()
            log.write.side_effect = [None, None, PermissionError("disk")]
            return log
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_path)
    rig.kill.side_effect = PermissionError
    with pytest.raises(s.FatalCleanupError):
        run(rig)
    with pytest.raises(BlockingIOError):
        assert_lock_free(rig)


def test_poll_record_failure_still_cleans_up_worker(rig, monkeypatch):
    original = Path.open

    def open_path(path, *args, **kwargs):
        if path.suffix == ".jsonl":
            log = Mock()
            log.write.side_effect = [None, PermissionError("disk"), None]
            return log
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_path)
    assert "failed" in run(rig)["reason"]
    rig.proc.wait.assert_called_once()
    assert_lock_free(rig)


@pytest.mark.parametrize("level,expected", [(1, "green"), (2, "yellow"), (4, "red"),
                                           (9, "unknown"), ("bad", "unknown")])
def test_mac_snapshot(monkeypatch, tmp_path, level, expected):
    monkeypatch.setattr(s.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(s.shutil, "disk_usage", lambda path: SimpleNamespace(free=12_000_000_000))
    telemetry = Mock(return_value=SimpleNamespace(stdout=str(level)))
    monkeypatch.setattr(s.subprocess, "run", telemetry)
    assert s.resource_snapshot(tmp_path) == {
        "platform": "Darwin", "pressure": expected, "free_disk_gb": 12,
    }
    assert telemetry.call_args.args[0] == [
        "/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level",
    ]
    assert telemetry.call_args.kwargs["timeout"] == 3


def test_snapshot_failures_are_nonfatal(monkeypatch, tmp_path):
    monkeypatch.setattr(s.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(s.shutil, "disk_usage", Mock(side_effect=PermissionError))
    monkeypatch.setattr(s.subprocess, "run", Mock(side_effect=subprocess.TimeoutExpired("sysctl", 3)))
    assert s.resource_snapshot(tmp_path) == {
        "platform": "Darwin", "pressure": "unknown", "free_disk_gb": None,
    }


def linux_files(monkeypatch, *, total=16, available=8, limit="max", used=0, ancestor="max"):
    files = {
        "/proc/meminfo": f"MemTotal: {total * 1024**2} kB\nMemAvailable: {available * 1024**2} kB\n",
        "/proc/self/cgroup": "0::/tenant/job\n",
        "/proc/self/mountinfo": "31 20 0:28 /tenant /sys/fs/cgroup rw - cgroup2 cgroup rw\n",
        "/sys/fs/cgroup/job/memory.max": str(limit),
        "/sys/fs/cgroup/job/memory.high": "max",
        "/sys/fs/cgroup/job/memory.current": str(used),
        "/sys/fs/cgroup/memory.max": str(ancestor),
        "/sys/fs/cgroup/memory.high": "max",
        "/sys/fs/cgroup/memory.current": str(used),
    }

    def read(path, *args, **kwargs):
        if str(path) not in files:
            raise FileNotFoundError(str(path))
        return files[str(path)]

    monkeypatch.setattr(Path, "read_text", read)
    return files


@pytest.mark.parametrize("options,expected", [
    ({}, "green"), ({"available": 1}, "yellow"),
    ({"total": 64, "available": 5}, "yellow"),
    ({"limit": 4 * s._GIB, "used": 3 * s._GIB}, "yellow"),
    ({"limit": 4 * s._GIB, "used": 2 * s._GIB}, "green"),
    ({"ancestor": 8 * s._GIB, "used": 7 * s._GIB}, "yellow"),
    ({"limit": 32 * s._GIB, "used": 29 * s._GIB}, "yellow"),
])
def test_linux_host_and_cgroup_memory_floors(monkeypatch, options, expected):
    linux_files(monkeypatch, **options)
    assert s._linux_pressure() == expected


def test_linux_memory_high_and_unknown_telemetry(monkeypatch, tmp_path):
    files = linux_files(monkeypatch)
    files["/sys/fs/cgroup/job/memory.high"] = str(s._GIB)
    assert s._linux_pressure() == "yellow"
    monkeypatch.setattr(s.platform, "system", lambda: "Linux")
    files["/sys/fs/cgroup/job/memory.max"] = "broken"
    assert s.resource_snapshot(tmp_path)["pressure"] == "unknown"


def test_cgroup_v1_memory_limit_is_not_silently_ignored(monkeypatch, tmp_path):
    files = linux_files(monkeypatch)
    files['/proc/self/cgroup'] = '3:memory:/limited-container\n'
    with pytest.raises(ValueError, match='cgroup v1'):
        s._linux_pressure()
    monkeypatch.setattr(s.platform, 'system', lambda: 'Linux')
    assert s.resource_snapshot(tmp_path)['pressure'] == 'unknown'
