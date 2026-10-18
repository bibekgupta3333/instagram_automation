"""Offline model selection and shell validation propagation; no downloader is invoked."""

import importlib.util
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SETUP = Path(__file__).resolve().parents[1] / "scripts" / "setup"
SPEC = importlib.util.spec_from_file_location("setup_manifest", SETUP / "manifest.py")
manifest = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manifest)


def run_manifest(*args):
    return subprocess.run(
        [sys.executable, str(SETUP / "manifest.py"), *args],
        capture_output=True, text=True, check=False,
    )


def test_defaults_unchanged():
    models = manifest.load()["models"]
    assert manifest.selected(models, "all", False) == [m for m in models if not m.get("optional")]
    assert manifest.selected(models, "all", True) == models


def test_exact_repeatable_names_preserve_manifest_order_and_deduplicate():
    result = run_manifest(
        "list", "--name", "realesrgan-x4plus", "--name", "kokoro-82m",
        "--name", "kokoro-82m",
    )
    assert result.returncode == 0, result.stderr
    rows = [row.split("\t") for row in result.stdout.splitlines()]
    assert [row[0] for row in rows] == ["kokoro-82m", "realesrgan-x4plus"]
    assert all(len(row) == 8 for row in rows)


def test_named_total_and_stage_selection():
    result = run_manifest("total", "--stage", "tts", "--name", "kokoro-82m")
    assert result.returncode == 0
    assert result.stdout.strip() == "0.4"


@pytest.mark.parametrize("name", ["missing", "kokoro", "Kokoro-82m", ""])
@pytest.mark.parametrize("cmd", ["list", "total"])
def test_unknown_names_fail_without_partial_output(name, cmd):
    result = run_manifest(cmd, "--name", "kokoro-82m", "--name", name)
    assert result.returncode == 2
    assert "unknown model name" in result.stderr
    assert result.stdout == ""


def test_explicit_optional_requires_opt_in():
    result = run_manifest("list", "--name", "flux2-klein-4b-q4")
    assert result.returncode == 2
    assert "require --include-optional" in result.stderr
    assert result.stdout == ""


def test_opt_in_with_name_never_selects_other_optional_models():
    result = run_manifest("list", "--include-optional", "--name", "flux2-klein-4b-q4")
    assert result.returncode == 0
    assert [row.split("\t")[0] for row in result.stdout.splitlines()] == ["flux2-klein-4b-q4"]


def test_stage_mismatch_fails_explicitly():
    result = run_manifest("list", "--stage", "asr", "--name", "kokoro-82m")
    assert result.returncode == 2
    assert "outside --stage asr" in result.stderr
    assert result.stdout == ""


@pytest.fixture
def shell_setup(tmp_path):
    for filename in ("20-pull-models.sh", "manifest.py", "models.manifest.yaml"):
        shutil.copyfile(SETUP / filename, tmp_path / filename)
    # Isolate every presence/network/download helper from the real host.
    # Any unexpected helper call is recorded; download functions only fail locally.
    helpers = f'''REPO_ROOT={shlex.quote(str(tmp_path))}
N_FAIL=0
manifest() {{ {shlex.quote(sys.executable)} "$REPO_ROOT/manifest.py" "$@"; }}
free_gb() {{ echo disk >> "$REPO_ROOT/events"; echo 100; }}
hf_has() {{ echo presence >> "$REPO_ROOT/events"; return 1; }}
ollama_running() {{ echo presence >> "$REPO_ROOT/events"; return 1; }}
hf() {{ echo download >> "$REPO_ROOT/events"; return 99; }}
ollama() {{ echo download >> "$REPO_ROOT/events"; return 99; }}
have() {{ return 0; }}
gb_add() {{ echo 20; }}
gb_lt() {{ return 1; }}
pass() {{ :; }}
info() {{ echo "$*"; }}
fail() {{ N_FAIL=$((N_FAIL + 1)); }}
skip() {{ :; }}
'''
    (tmp_path / "lib.sh").write_text(helpers)
    return tmp_path


def run_shell(root, *args):
    return subprocess.run(
        ["bash", str(root / "20-pull-models.sh"), *args],
        capture_output=True, text=True, check=False,
    )


@pytest.mark.parametrize("args, message", [
    (["--name", "missing"], "unknown model name"),
    (["--name", "kokoro-82m", "--name", "missing"], "unknown model name"),
    (["--name", "flux2-klein-4b-q4"], "require --include-optional"),
    (["--name"], "requires a model name"),
    (["--name", "--include-optional"], "requires a model name"),
    (["--name="], "unknown model name"),
])
def test_downloader_rejects_invalid_selection_before_any_side_effect(shell_setup, args, message):
    result = run_shell(shell_setup, *args)
    assert result.returncode == 2
    assert message in result.stderr
    assert not (shell_setup / "events").exists()


def test_list_failure_propagates_even_with_partial_manifest_output(shell_setup):
    with (shell_setup / "lib.sh").open("a") as stream:
        stream.write('''
manifest() {
  if [[ "$1" == list ]]; then
    printf 'partial\\ttts\\thf\\tfake/repo\\t0.4\\tno\\t-\\tyes\\n'
    echo 'list validation failed' >&2
    return 7
  fi
  echo 10
}
''')
    result = run_shell(shell_setup)
    assert result.returncode == 7
    assert "list validation failed" in result.stderr
    assert not (shell_setup / "events").exists()


def test_downloader_forwards_repeatable_names_with_optional_opt_in(shell_setup):
    result = run_shell(
        shell_setup, "--dry-run", "--include-optional",
        "--name=flux2-klein-4b-q4", "--name", "kokoro-82m",
    )
    assert result.returncode == 0, result.stderr
    assert "Models: 2 planned, 0 pulled" in result.stdout
    assert "Selected download size: 5.0 GB" in result.stdout
    assert "mflux-community/flux2-klein-4b-mflux-q4" in result.stdout
    assert "mlx-community/Kokoro-82M-bf16" in result.stdout
    assert "z-image" not in result.stdout
    assert "download" not in (shell_setup / "events").read_text()


def test_downloader_bash_syntax():
    result = subprocess.run(
        ["bash", "-n", str(SETUP / "20-pull-models.sh")],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_include_files_are_planned_even_when_repo_is_partially_cached(shell_setup):
    # Model a repo containing unrelated cached files: repo-level presence is true,
    # but it cannot prove the selected voices/weights are present.
    with (shell_setup / "lib.sh").open("a") as stream:
        stream.write('hf_has() { echo repo-presence >> "$REPO_ROOT/events"; return 0; }\n')
    result = run_shell(shell_setup, "--dry-run", "--name", "kokoro-82m")
    assert result.returncode == 0, result.stderr
    assert "Models: 1 planned, 0 pulled, 0 already present" in result.stdout
    assert "voices/*.safetensors" in result.stdout
    assert "repo-presence" not in (shell_setup / "events").read_text()
    assert "download" not in (shell_setup / "events").read_text()


def test_repo_presence_still_skips_hf_entries_without_include_patterns(shell_setup):
    with (shell_setup / "lib.sh").open("a") as stream:
        stream.write('hf_has() { echo repo-presence >> "$REPO_ROOT/events"; return 0; }\n')
    result = run_shell(shell_setup, "--dry-run", "--name", "parakeet-tdt-0.6b-v2")
    assert result.returncode == 0, result.stderr
    assert "Models: 0 planned, 0 pulled, 1 already present" in result.stdout
    assert "repo-presence" in (shell_setup / "events").read_text()
    assert "download" not in (shell_setup / "events").read_text()
