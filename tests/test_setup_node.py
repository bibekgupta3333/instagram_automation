"""Node bootstrap remains explicit; npm commands preserve launcher arguments."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")


def test_node_dry_run_has_no_download_or_install_side_effects(tmp_path):
    sentinel = tmp_path / "unexpected-command"
    commands = tmp_path / "bin"
    commands.mkdir()
    for command in ("curl", "sudo", "npm", "tar"):
        path = commands / command
        path.write_text(f'#!/bin/sh\ntouch "{sentinel}"\nexit 99\n')
        path.chmod(0o755)
    result = subprocess.run([BASH, str(REPO / "scripts/setup/05-node.sh"), "--dry-run"],
                            check=False, capture_output=True, text=True, timeout=10,
                            env={**os.environ, "PATH": f"{commands}:/usr/bin:/bin"})
    assert result.returncode == 0, result.stderr
    assert "SHA-256" in result.stdout and "no sudo" in result.stdout
    assert not sentinel.exists()


def test_node_bootstrap_rejects_unknown_arguments():
    result = subprocess.run([BASH, str(REPO / "scripts/setup/05-node.sh"), "--unknown"],
                            check=False, capture_output=True, text=True, timeout=10)
    assert result.returncode == 2 and "Unknown option" in result.stderr


def test_package_install_does_not_run_system_setup():
    package = json.loads((REPO / "package.json").read_text())
    assert package["private"] is True
    assert not {"preinstall", "install", "postinstall", "prepare"} & package["scripts"].keys()
    assert not package.get("dependencies") and not package.get("devDependencies")


@pytest.mark.skipif(not shutil.which("npm"), reason="Node/npm not installed")
def test_npm_start_forwards_port_to_python_app(tmp_path):
    shutil.copy(REPO / "package.json", tmp_path / "package.json")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy(REPO / "scripts/run-web.sh", scripts / "run-web.sh")
    environment = tmp_path / ".venv-web/bin"
    environment.mkdir(parents=True)
    interpreter = environment / "python"
    interpreter.write_text('#!/bin/sh\nprintf "APP_ARG:%s\\n" "$@"\n')
    interpreter.chmod(0o755)
    result = subprocess.run([shutil.which("npm"), "--cache", str(tmp_path / "cache"),
                             "start", "--", "--port", "8092"],
                            check=False, cwd=tmp_path, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert "APP_ARG:studio.web" in result.stdout
    assert result.stdout.endswith("APP_ARG:--port\nAPP_ARG:8092\n")
