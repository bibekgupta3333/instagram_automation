"""Offline installer/launcher checks: no apt, downloads, credentials or models."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
INSTALLER = REPO / "scripts/setup/50-ubuntu.sh"
BASH = shutil.which("bash")


@pytest.mark.parametrize("arguments", [[], ["--skip-clis"], ["--with-dev"],
                                      ["--with-claude-desktop", "--run"]])
def test_dry_run_never_invokes_installers(tmp_path, arguments):
    sentinel = tmp_path / "unexpected-command"
    commands = tmp_path / "bin"
    commands.mkdir()
    for command in ("curl", "sudo", "npm", "uv", "apt-get"):
        path = commands / command
        path.write_text(f'#!/bin/sh\ntouch "{sentinel}"\nexit 99\n')
        path.chmod(0o755)
    result = subprocess.run([BASH, str(INSTALLER), "--dry-run", *arguments],
                            check=False, env={**os.environ, "PATH": f"{commands}:/usr/bin:/bin"},
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "no commands, downloads or writes" in result.stdout
    assert not sentinel.exists()
    assert ("opencode-ai@1.19.4" in result.stdout) == ("--skip-clis" not in arguments)


def test_bad_option_and_help_do_not_install():
    result = subprocess.run([BASH, str(INSTALLER), "--unknown"], check=False, capture_output=True,
                            text=True, timeout=10)
    assert result.returncode == 2 and "Unknown option" in result.stderr
    result = subprocess.run([BASH, str(INSTALLER), "--help"], check=False, capture_output=True,
                            text=True, timeout=10)
    assert result.returncode == 0 and "--with-claude-desktop" in result.stdout


def test_launcher_uses_isolated_venv_and_passes_arguments(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy(REPO / "scripts/run-web.sh", scripts / "run-web.sh")
    environment = tmp_path / ".venv-web/bin"
    environment.mkdir(parents=True)
    interpreter = environment / "python"
    interpreter.write_text('#!/bin/sh\nprintf "%s\\n" "$PWD" "$@"\n')
    interpreter.chmod(0o755)
    result = subprocess.run([BASH, str(scripts / "run-web.sh"), "--port", "8090"],
                            check=False, cwd="/", capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [str(tmp_path), "-m", "studio.web", "--host",
                                          "127.0.0.1", "--port", "8080", "--port", "8090"]


def test_launcher_explains_missing_environment(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy(REPO / "scripts/run-web.sh", scripts / "run-web.sh")
    result = subprocess.run([BASH, str(scripts / "run-web.sh")], check=False, capture_output=True,
                            text=True, timeout=10)
    assert result.returncode == 1 and "Missing .venv-web" in result.stderr
