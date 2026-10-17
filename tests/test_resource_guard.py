"""Safety checks use mocked macOS telemetry; no models or real services."""

import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "guard", Path(__file__).parents[1] / "scripts" / "run_guarded.py",
)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


@pytest.fixture(autouse=True)
def isolated_workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(guard.shutil, "disk_usage", lambda p: SimpleNamespace(free=20e9))
    # Unexpected process/telemetry calls must never reach the host or a model.
    monkeypatch.setattr(guard.subprocess, "run", lambda *a, **k: pytest.fail("real telemetry"))
    monkeypatch.setattr(guard.subprocess, "Popen", lambda *a, **k: pytest.fail("real launch"))
    monkeypatch.setattr(guard.os, "killpg", lambda *a: pytest.fail("real signal"))


@pytest.mark.parametrize("pressure", [2, 4])
def test_yellow_or_red_refuses_launch(monkeypatch, tmp_path, pressure):
    monkeypatch.setattr(guard, "pressure_level", lambda: pressure)
    monkeypatch.setattr(guard.subprocess, "Popen", lambda *a, **k: pytest.fail("launched"))
    assert guard.main(["--log", str(tmp_path / "log"), "--", "ignored"]) == 75


def test_low_disk_refuses_work(monkeypatch, tmp_path):
    monkeypatch.setattr(guard, "pressure_level", lambda: 1)
    monkeypatch.setattr(guard.shutil, "disk_usage", lambda p: SimpleNamespace(free=9e9))
    with pytest.raises(RuntimeError, match="Disk"):
        guard.safety_check(tmp_path)


def test_unsafe_readings_are_logged_before_stopping(monkeypatch, tmp_path):
    monkeypatch.setattr(guard, "pressure_level", lambda: 2)
    monkeypatch.setattr(guard.shutil, "disk_usage", lambda p: SimpleNamespace(free=20e9))
    readings = []
    with pytest.raises(RuntimeError, match="green"):
        guard.safety_check(tmp_path, record=readings.append)
    assert readings[0]["pressure"] == 2


def test_disk_floor_uses_unrounded_bytes(monkeypatch, tmp_path):
    monkeypatch.setattr(guard, "pressure_level", lambda: 1)
    monkeypatch.setattr(guard.shutil, "disk_usage", lambda p: SimpleNamespace(free=9_999_999_999))
    with pytest.raises(RuntimeError, match="Disk"):
        guard.safety_check(tmp_path)


def test_unknown_pressure_refuses_work(monkeypatch):
    monkeypatch.setattr(guard.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="0"))
    with pytest.raises(ValueError, match="Unknown"):
        guard.pressure_level()


def test_monitor_failure_stops_only_spawned_group(monkeypatch, tmp_path):
    levels = iter([1, 2])
    monkeypatch.setattr(guard, "pressure_level", lambda: next(levels))
    monkeypatch.setattr(guard.shutil, "disk_usage", lambda p: SimpleNamespace(free=20e9))
    child = SimpleNamespace(pid=12345, poll=lambda: None, wait=lambda **kw: 0)
    monkeypatch.setattr(guard.subprocess, "Popen", lambda *a, **kw: child)
    killed = []
    monkeypatch.setattr(guard.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    assert guard.main(["--log", str(tmp_path / "log"), "--", "ignored"]) == 75
    assert killed == [(12345, guard.signal.SIGTERM), (12345, guard.signal.SIGKILL)]


def test_telemetry_denied_refuses_launch(monkeypatch, tmp_path):
    def denied():
        raise subprocess.CalledProcessError(1, "sysctl")

    monkeypatch.setattr(guard, "pressure_level", denied)
    monkeypatch.setattr(guard.subprocess, "Popen", lambda *a, **k: pytest.fail("launched"))
    assert guard.main(["--log", str(tmp_path / "log"), "--", "ignored"]) == 75
    reading = json.loads((tmp_path / "log").read_text())
    assert reading["pressure"] is None
    assert "error" in reading


@pytest.mark.parametrize("threads", ["0", "-1", "4"])
def test_thread_limit_cannot_be_raised(threads):
    with pytest.raises(SystemExit) as exc:
        guard.main(["--threads", threads, "--", "ignored"])
    assert exc.value.code == 2


@pytest.mark.parametrize("threads", [1, 2, 3])
def test_child_limits_and_offline_settings_override_inherited_values(monkeypatch, threads):
    monkeypatch.setattr(guard, "pressure_level", lambda: 1)
    keys = (
        "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS", "STUDIO_CPU_THREADS",
    )
    inherited = dict.fromkeys(keys, "12")
    inherited.update(HF_HUB_OFFLINE="0", TRANSFORMERS_OFFLINE="0",
                     STUDIO_ALLOW_MODEL_DOWNLOAD="1", TOKENIZERS_PARALLELISM="true")
    monkeypatch.setattr(guard.os, "environ", inherited)
    launches = []
    child = SimpleNamespace(pid=12345, poll=lambda: 7, returncode=7)

    def launch(command, **kwargs):
        launches.append((command, kwargs))
        return child

    monkeypatch.setattr(guard.subprocess, "Popen", launch)
    monkeypatch.setattr(guard, "stop_group", lambda proc: None)
    assert guard.main(["--threads", str(threads), "--", "ignored"]) == 7
    command, kwargs = launches[0]
    assert command == ["ignored"]
    assert kwargs["start_new_session"] is True
    env = kwargs["env"]
    assert all(env[key] == str(threads) for key in keys)
    assert env["HF_HUB_OFFLINE"] == env["TRANSFORMERS_OFFLINE"] == "1"
    assert env["STUDIO_ALLOW_MODEL_DOWNLOAD"] == "0"
    assert env["TOKENIZERS_PARALLELISM"] == "false"
    assert inherited["STUDIO_ALLOW_MODEL_DOWNLOAD"] == "1"


@pytest.mark.parametrize("failure", ["yellow", "red", "disk", "telemetry"])
def test_monitor_logs_unsafe_sample_and_stops_group(monkeypatch, tmp_path, failure):
    levels = iter([1, 1, 2 if failure == "yellow" else 4 if failure == "red" else 1])

    def pressure():
        level = next(levels)
        if failure == "telemetry" and len(sleeps) == 1:
            raise OSError("telemetry unavailable")
        return level

    monkeypatch.setattr(guard, "pressure_level", pressure)
    disk = iter([20e9, 20e9, 9_999_999_999 if failure == "disk" else 20e9])
    monkeypatch.setattr(guard.shutil, "disk_usage", lambda p: SimpleNamespace(free=next(disk)))
    child = SimpleNamespace(pid=12345, poll=lambda: None, wait=lambda **kw: 0)
    monkeypatch.setattr(guard.subprocess, "Popen", lambda *a, **kw: child)
    sleeps, killed = [], []
    monkeypatch.setattr(guard.time, "sleep", sleeps.append)
    monkeypatch.setattr(guard.os, "killpg", lambda *args: killed.append(args))
    assert guard.main(["--", "ignored"]) == 75
    assert sleeps == [1]
    assert killed == [(12345, guard.signal.SIGTERM), (12345, guard.signal.SIGKILL)]
    readings = [json.loads(line) for line in (tmp_path / "output/memory-pressure.jsonl")
                .read_text().splitlines()]
    assert len(readings) == 3
    last = readings[-1]
    if failure == "disk":
        assert last["free_bytes"] == 9_999_999_999
    elif failure == "telemetry":
        assert last["error"] == "telemetry unavailable"
    else:
        assert last["pressure"] == (2 if failure == "yellow" else 4)


@pytest.mark.parametrize("signum", [guard.signal.SIGINT, guard.signal.SIGTERM])
def test_interrupt_cleans_group_before_releasing_lock_and_restores_handlers(monkeypatch, signum):
    monkeypatch.setattr(guard, "pressure_level", lambda: 1)
    previous = {sig: guard.signal.getsignal(sig) for sig in (guard.signal.SIGINT, guard.signal.SIGTERM)}
    child = SimpleNamespace(pid=12345)
    monkeypatch.setattr(guard.subprocess, "Popen", lambda *a, **kw: child)

    def interrupted_poll():
        guard.signal.getsignal(signum)(signum, None)

    child.poll = interrupted_poll
    cleaned = []

    def cleanup(proc):
        assert proc is child
        assert all(guard.signal.getsignal(sig) == guard.signal.SIG_IGN for sig in previous)
        with (
            Path("output/.guarded-run.lock").open("w") as lock,
            pytest.raises(BlockingIOError),
        ):
            guard.fcntl.flock(lock, guard.fcntl.LOCK_EX | guard.fcntl.LOCK_NB)
        cleaned.append(proc)

    monkeypatch.setattr(guard, "stop_group", cleanup)
    assert guard.main(["--", "ignored"]) == 75
    assert cleaned == [child]
    assert all(guard.signal.getsignal(sig) == handler for sig, handler in previous.items())
    with Path("output/.guarded-run.lock").open("w") as lock:
        guard.fcntl.flock(lock, guard.fcntl.LOCK_EX | guard.fcntl.LOCK_NB)


def test_lock_refuses_concurrent_job(monkeypatch):
    Path("output").mkdir()
    with Path("output/.guarded-run.lock").open("w") as lock:
        guard.fcntl.flock(lock, guard.fcntl.LOCK_EX | guard.fcntl.LOCK_NB)
        assert guard.main(["--", "ignored"]) == 75


def test_cleanup_error_releases_lock_and_returns_failure(monkeypatch):
    monkeypatch.setattr(guard, "pressure_level", lambda: 1)
    child = SimpleNamespace(pid=12345, poll=lambda: 0, returncode=0)
    monkeypatch.setattr(guard.subprocess, "Popen", lambda *a, **kw: child)

    def denied(proc):
        raise PermissionError("cannot signal child")

    monkeypatch.setattr(guard, "stop_group", denied)
    assert guard.main(["--", "ignored"]) == 75
    with Path("output/.guarded-run.lock").open("w") as lock:
        guard.fcntl.flock(lock, guard.fcntl.LOCK_EX | guard.fcntl.LOCK_NB)


@pytest.mark.parametrize("gone,timeout", [(True, False), (False, True), (False, False)])
def test_stop_group_reaps_leader_and_escalates(monkeypatch, gone, timeout):
    events = []

    def killpg(pid, sig):
        events.append((pid, sig))
        if gone:
            raise ProcessLookupError

    def wait(**kwargs):
        events.append(kwargs)
        if timeout and kwargs:
            raise subprocess.TimeoutExpired("ignored", 5)
        return 0

    monkeypatch.setattr(guard.os, "killpg", killpg)
    guard.stop_group(SimpleNamespace(pid=12345, wait=wait))
    assert events == [(12345, guard.signal.SIGTERM), {"timeout": 5},
                      (12345, guard.signal.SIGKILL), {}]


def test_unreadable_disk_logs_failure(monkeypatch, tmp_path):
    def denied(path):
        raise OSError("disk unavailable")

    monkeypatch.setattr(guard.shutil, "disk_usage", denied)
    readings = []
    with pytest.raises(OSError, match="disk unavailable"):
        guard.safety_check(tmp_path, record=readings.append)
    assert readings[0]["error"] == "disk unavailable"
    assert readings[0]["free_gb"] is None


def test_exact_disk_floor_is_safe(monkeypatch, tmp_path):
    monkeypatch.setattr(guard, "pressure_level", lambda: 1)
    monkeypatch.setattr(guard.shutil, "disk_usage", lambda p: SimpleNamespace(free=10_000_000_000))
    assert guard.safety_check(tmp_path)["free_gb"] == 10
