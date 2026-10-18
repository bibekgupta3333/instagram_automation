"""Offline safety checks for the standalone Wan preview runner; never load models."""

import argparse
import builtins
import hashlib
import importlib.util
import socket
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, call

import pytest


@pytest.fixture
def pilot(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail("Offline test attempted a download, network connection, or subprocess")

    hub = ModuleType("huggingface_hub")
    hub.try_to_load_from_cache = Mock()
    hub.hf_hub_download = Mock(side_effect=forbidden)
    hub.snapshot_download = Mock(side_effect=forbidden)
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)

    real_import = builtins.__import__
    heavy_imports = Mock()

    def guarded_import(name, *args, **kwargs):
        if name.split(".")[0] in {
            "mlx", "mflux", "torch", "diffusers", "transformers", "numpy", "cv2",
        }:
            heavy_imports(name)
            pytest.fail(f"Unexpected heavy import: {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    path = Path(__file__).resolve().parents[1] / "scripts" / "generate_wan_video.py"
    spec = importlib.util.spec_from_file_location("wan_video_pilot_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    process = Mock(side_effect=forbidden)
    monkeypatch.setattr(module.subprocess, "run", process)
    # Isolate guard settings entirely from the machine's real environment.
    monkeypatch.setattr(module, "os", SimpleNamespace(environ={
        "HF_HUB_OFFLINE": "1", "STUDIO_CPU_THREADS": "3",
    }))

    original = module.COMPONENTS
    components, paths = [], []
    for index, (repo, filename, revision, _) in enumerate(original):
        payload = f"tiny offline component {index}".encode()
        cached = tmp_path / f"component-{index}.bin"
        cached.write_bytes(payload)
        paths.append(cached)
        components.append((repo, filename, revision, hashlib.sha256(payload).hexdigest()))
    monkeypatch.setattr(module, "COMPONENTS", tuple(components))
    hub.try_to_load_from_cache.side_effect = lambda repo, filename, **kwargs: str(
        paths[next(i for i, c in enumerate(components) if c[:2] == (repo, filename))]
    )
    runtime, reference = tmp_path / "fake-runtime", tmp_path / "reference.png"
    runtime.touch()
    reference.touch()
    args = argparse.Namespace(
        runtime=runtime, reference=reference, output_dir=tmp_path / "result",
        width=320, height=384, frames=17, steps=20, seed=18042,
        prompt="Original fictional presenter; no text",
    )
    monkeypatch.setattr(sys, "argv", [str(path), "--runtime", str(runtime),
                        "--reference", str(reference), "--output-dir", str(args.output_dir)])
    yield SimpleNamespace(
        module=module, hub=hub, paths=paths, args=args, process=process,
        heavy_imports=heavy_imports,
    )
    hub.hf_hub_download.assert_not_called()
    hub.snapshot_download.assert_not_called()
    process.assert_not_called()
    heavy_imports.assert_not_called()


def test_cached_components_verify_all_files_and_pin_revisions(pilot):
    assert pilot.module.cached_components() == pilot.paths
    assert pilot.hub.try_to_load_from_cache.call_args_list == [
        call(repo, filename, revision=revision)
        for repo, filename, revision, _ in pilot.module.COMPONENTS
    ]


def test_tiny_decoder_replaces_full_vae_and_is_checksum_verified(pilot, monkeypatch, tmp_path):
    payload = b"offline tiny decoder fixture"
    path = tmp_path / "tiny.safetensors"
    path.write_bytes(payload)
    tiny = ("fixture/tiny", "tiny.safetensors", "pinned-tiny-revision",
            hashlib.sha256(payload).hexdigest())
    monkeypatch.setattr(pilot.module, "TINY_DECODER", tiny)
    lookup = pilot.hub.try_to_load_from_cache.side_effect

    def with_tiny(repo, filename, **kwargs):
        if repo == tiny[0]:
            assert filename == tiny[1] and kwargs["revision"] == tiny[2]
            return str(path)
        return lookup(repo, filename, **kwargs)

    pilot.hub.try_to_load_from_cache.side_effect = with_tiny
    assert pilot.module.cached_components("tiny") == [*pilot.paths[:2], path]
    assert call(*pilot.module.COMPONENTS[2][:2],
                revision=pilot.module.COMPONENTS[2][2]) not in (
        pilot.hub.try_to_load_from_cache.call_args_list
    )
    path.write_bytes(b"corrupt tiny decoder")
    with pytest.raises(RuntimeError, match="Checksum mismatch"):
        pilot.module.cached_components("tiny")


def test_tiny_command_selects_taesd_instead_of_full_vae(pilot):
    pilot.args.decoder = "tiny"
    cmd = pilot.module.command(pilot.args, pilot.paths)
    assert "--vae" not in cmd
    assert cmd[cmd.index("--taesd")+1] == str(pilot.paths[2])
    assert cmd[cmd.index("--threads")+1] == "3"


@pytest.mark.parametrize("component_index", [0, 1, 2])
@pytest.mark.parametrize("missing_kind", ["absent", "negative-cache", "missing-file", "directory"])
def test_missing_component_stops_before_download_or_process(
    pilot, tmp_path, component_index, missing_kind,
):
    lookup = pilot.hub.try_to_load_from_cache.side_effect
    replacement = {
        "absent": None, "negative-cache": object(),
        "missing-file": str(tmp_path / "not-present"), "directory": str(tmp_path),
    }[missing_kind]
    calls = 0

    def missing(repo, filename, **kwargs):
        nonlocal calls
        current = calls
        calls += 1
        return replacement if current == component_index else lookup(repo, filename, **kwargs)

    pilot.hub.try_to_load_from_cache.side_effect = missing
    with pytest.raises(RuntimeError, match="Missing cached component"):
        pilot.module.main()
    assert calls == component_index + 1
    assert not (pilot.args.output_dir / "run.json").exists()


@pytest.mark.parametrize("component_index", [0, 1, 2])
def test_corrupt_cached_component_stops_before_runtime(pilot, component_index):
    pilot.paths[component_index].write_bytes(b"corrupt cached bytes")
    with pytest.raises(RuntimeError, match="Checksum mismatch"):
        pilot.module.main()
    assert pilot.hub.try_to_load_from_cache.call_count == component_index + 1
    assert not (pilot.args.output_dir / "run.json").exists()


@pytest.mark.parametrize("frames,width,height", [(5, 16, 16), (17, 320, 384), (33, 512, 512)])
def test_command_uses_reference_video_and_bounded_memory(pilot, frames, width, height):
    args = pilot.args
    args.frames, args.width, args.height = frames, width, height
    command = pilot.module.command(args, pilot.paths)
    assert command[0] == str(args.runtime.resolve())
    expected = {
        "-M": "vid_gen", "-i": str(args.reference.resolve()), "-p": args.prompt,
        "--diffusion-model": str(pilot.paths[0]), "--t5xxl": str(pilot.paths[1]),
        "--vae": str(pilot.paths[2]), "--video-frames": str(frames),
        "-W": str(width), "-H": str(height), "--threads": "3", "--fps": "16",
        "--flash-attn": "on", "--batched-cfg": "off", "--vae-tile-size": "128x128",
        "--max-vram": "6", "--steps": str(args.steps), "-s": str(args.seed),
        "-o": str((args.output_dir / "raw.avi").resolve()),
    }
    for flag, value in expected.items():
        assert command.count(flag) == 1
        assert command[command.index(flag) + 1] == value
    for flag in ("--mmap", "--offload-to-cpu", "--vae-tiling",
                 "--temporal-tiling", "--disable-prefetch"):
        assert flag in command
    assert not any("download" in token or "quantiz" in token for token in command)
    assert "-q" not in command
    pilot.hub.try_to_load_from_cache.assert_not_called()


@pytest.mark.parametrize("flag,value,message", [
    ("--frames", "0", "Pilot frames"),
    ("--frames", "4", "Pilot frames"),
    ("--frames", "6", "Pilot frames"),
    ("--frames", "37", "Pilot frames"),
    ("--width", "0", "Pilot dimensions"),
    ("--height", "-16", "Pilot dimensions"),
    ("--width", "528", "Pilot dimensions"),
    ("--height", "528", "Pilot dimensions"),
    ("--width", "319", "multiples of 16"),
    ("--height", "383", "multiples of 16"),
    ("--steps", "0", "Steps must"),
    ("--steps", "41", "Steps must"),
])
def test_invalid_parameters_fail_before_cache_or_heavy_imports(
    pilot, monkeypatch, capsys, flag, value, message,
):
    monkeypatch.setattr(sys, "argv", [*sys.argv, flag, value])
    with pytest.raises(SystemExit) as exc:
        pilot.module.main()
    assert exc.value.code == 2
    assert message in capsys.readouterr().err
    pilot.hub.try_to_load_from_cache.assert_not_called()
    assert not pilot.args.output_dir.exists()


@pytest.mark.parametrize("key,value", [
    ("HF_HUB_OFFLINE", None), ("HF_HUB_OFFLINE", "0"),
    ("STUDIO_CPU_THREADS", None), ("STUDIO_CPU_THREADS", "4"),
])
def test_guard_settings_required_before_cache_lookup(pilot, capsys, key, value):
    if value is None:
        pilot.module.os.environ.pop(key)
    else:
        pilot.module.os.environ[key] = value
    with pytest.raises(SystemExit) as exc:
        pilot.module.main()
    assert exc.value.code == 2
    assert "run_guarded.py" in capsys.readouterr().err
    pilot.hub.try_to_load_from_cache.assert_not_called()
