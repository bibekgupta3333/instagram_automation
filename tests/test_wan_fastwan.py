"""Offline checks for the FastWan T2V preset of scripts/generate_wan_video.py; no model loads."""

import hashlib
import importlib.util
import json
import socket
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "generate_wan_video.py"


@pytest.fixture
def fw(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail("Offline test attempted a download, network connection, or subprocess")

    hub = ModuleType("huggingface_hub")
    hub.try_to_load_from_cache = Mock()
    hub.hf_hub_download = Mock(side_effect=forbidden)
    hub.snapshot_download = Mock(side_effect=forbidden)
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    spec = importlib.util.spec_from_file_location("wan_fastwan_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    process = Mock(side_effect=forbidden)
    monkeypatch.setattr(module.subprocess, "run", process)
    monkeypatch.setattr(module, "os", SimpleNamespace(environ={
        "HF_HUB_OFFLINE": "1", "STUDIO_CPU_THREADS": "3",
    }))

    # Replace every pinned component with a tiny local fixture and its real digest.
    fixtures = {}
    for name in ("FASTWAN_T2V", "TINY_DECODER"):
        repo, filename, revision, _ = getattr(module, name)
        payload = f"offline {name}".encode()
        path = tmp_path / f"{name}.bin"
        path.write_bytes(payload)
        monkeypatch.setattr(module, name, (repo, filename, revision,
                                           hashlib.sha256(payload).hexdigest()))
        fixtures[repo, filename] = path
    components = []
    for index, (repo, filename, revision, _) in enumerate(module.COMPONENTS):
        payload = f"offline component {index}".encode()
        path = tmp_path / f"component-{index}.bin"
        path.write_bytes(payload)
        components.append((repo, filename, revision, hashlib.sha256(payload).hexdigest()))
        fixtures[repo, filename] = path
    monkeypatch.setattr(module, "COMPONENTS", tuple(components))
    hub.try_to_load_from_cache.side_effect = (
        lambda repo, filename, **kwargs: str(fixtures[repo, filename])
    )
    runtime = tmp_path / "sd-cli"
    runtime.touch()
    out = tmp_path / "result"
    base_argv = [str(SCRIPT), "--preset", "fastwan-t2v", "--runtime", str(runtime),
                 "--output-dir", str(out)]
    monkeypatch.setattr(sys, "argv", base_argv)
    yield SimpleNamespace(module=module, hub=hub, fixtures=fixtures, runtime=runtime,
                          out=out, process=process, argv=base_argv, tmp=tmp_path)
    hub.hf_hub_download.assert_not_called()
    hub.snapshot_download.assert_not_called()


def _args(fw, **overrides):
    return SimpleNamespace(**{
        "preset": "fastwan-t2v", "runtime": fw.runtime, "reference": None,
        "output_dir": fw.out, "width": 320, "height": 576, "frames": 33, "steps": 3,
        "seed": 18042, "decoder": "tiny", "prompt": "aerial dam, no text", **overrides,
    })


def test_fastwan_components_use_distill_with_shared_encoder_and_tiny_codec(fw):
    m = fw.module
    paths = m.cached_components("tiny", "fastwan-t2v")
    assert paths == [fw.fixtures[m.FASTWAN_T2V[:2]], fw.fixtures[m.COMPONENTS[1][:2]],
                     fw.fixtures[m.TINY_DECODER[:2]]]
    revisions = [c.kwargs["revision"] for c in fw.hub.try_to_load_from_cache.call_args_list]
    assert revisions == [m.FASTWAN_T2V[2], m.COMPONENTS[1][2], m.TINY_DECODER[2]]


def test_fastwan_pinned_digest_mismatch_stops(fw):
    m = fw.module
    fw.fixtures[m.FASTWAN_T2V[:2]].write_bytes(b"tampered community file")
    with pytest.raises(RuntimeError, match="Checksum mismatch"):
        m.cached_components("tiny", "fastwan-t2v")


def test_real_pins_match_manifest():
    import yaml

    spec = importlib.util.spec_from_file_location("wan_pins", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    manifest = yaml.safe_load((SCRIPT.parent / "setup" / "models.manifest.yaml").read_text())
    entry = next(m for m in manifest["models"] if m["name"] == "fastwan2.1-t2v-1.3b-gguf")
    repo, filename, revision, digest = module.FASTWAN_T2V
    assert (entry["source"], entry["include"]) == (repo, [filename])
    assert (entry["revision"], entry["sha256"]) == (revision, digest)
    assert entry["optional"] is True and entry["licence"] == "apache-2.0"


@pytest.mark.parametrize("width,height,frames", [(320, 576, 33), (320, 576, 49), (480, 832, 33)])
def test_fastwan_argv_is_three_step_lcm_without_reference(fw, width, height, frames):
    m = fw.module
    paths = [Path("/m/fastwan.gguf"), Path("/m/umt5.gguf"), Path("/m/taew.safetensors")]
    cmd = m.command(_args(fw, width=width, height=height, frames=frames), paths)
    expected = {
        "-M": "vid_gen", "--diffusion-model": "/m/fastwan.gguf", "--t5xxl": "/m/umt5.gguf",
        "--taesd": "/m/taew.safetensors", "-W": str(width), "-H": str(height),
        "--video-frames": str(frames), "--steps": "3", "--cfg-scale": "1",
        "--sampling-method": "lcm", "--sigmas": "1.0,0.757,0.522,0.0", "-s": "18042",
        "--threads": "3", "--flash-attn": "on", "--batched-cfg": "off",
        "--vae-tile-size": "128x128", "--max-vram": "6",
        "-o": str((fw.out / "raw.avi").resolve()),
    }
    for flag, value in expected.items():
        assert cmd.count(flag) == 1, flag
        assert cmd[cmd.index(flag) + 1] == value
    for flag in ("--mmap", "--offload-to-cpu", "--vae-tiling", "--temporal-tiling",
                 "--disable-prefetch"):
        assert flag in cmd
    for absent in ("-i", "--vae", "-n", "--flow-shift", "-q"):
        assert absent not in cmd
    assert not any("download" in token for token in cmd)


@pytest.mark.parametrize("extra,message", [
    (["--width", "512", "--height", "512"], "320x576 or 480x832"),
    (["--width", "576", "--height", "320"], "320x576 or 480x832"),
    (["--width", "720", "--height", "1280"], "320x576 or 480x832"),
    (["--frames", "53"], "FastWan frames"),
    (["--frames", "34"], "FastWan frames"),
    (["--frames", "1"], "FastWan frames"),
    (["--steps", "20"], "Steps must be 3"),
    (["--reference", "ref.png"], "text-to-video"),
])
def test_fastwan_invalid_parameters_fail_before_cache(fw, monkeypatch, capsys, extra, message):
    monkeypatch.setattr(sys, "argv", [*fw.argv, *extra])
    with pytest.raises(SystemExit) as exc:
        fw.module.main()
    assert exc.value.code == 2
    assert message in capsys.readouterr().err
    fw.hub.try_to_load_from_cache.assert_not_called()
    assert not fw.out.exists()


@pytest.mark.parametrize("key,value", [("HF_HUB_OFFLINE", "0"), ("STUDIO_CPU_THREADS", "8")])
def test_fastwan_requires_guard_settings(fw, capsys, key, value):
    fw.module.os.environ[key] = value
    with pytest.raises(SystemExit):
        fw.module.main()
    assert "run_guarded.py" in capsys.readouterr().err
    fw.hub.try_to_load_from_cache.assert_not_called()


def test_fastwan_refuses_to_overwrite_existing_media(fw, capsys):
    fw.out.mkdir()
    (fw.out / "video.mp4").write_bytes(b"existing")
    with pytest.raises(SystemExit):
        fw.module.main()
    assert "fresh output directory" in capsys.readouterr().err
    fw.hub.try_to_load_from_cache.assert_not_called()


def test_fastwan_main_defaults_tiny_and_writes_unapproved_report(fw, monkeypatch):
    def fake_run(cmd, **kwargs):
        if cmd[0] == "ffmpeg":
            Path(cmd[-1]).write_bytes(b"mp4")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(fw.module.subprocess, "run", fake_run)
    assert fw.module.main() == 0
    report = json.loads((fw.out / "run.json").read_text())
    assert report["preset"] == "fastwan-t2v" and report["decoder"] == "tiny"
    assert report["approved"] is False and report["published"] is False
    cmd = report["command"]
    assert cmd[cmd.index("-H") + 1] == "576" and cmd[cmd.index("--steps") + 1] == "3"
    assert cmd[cmd.index("--taesd") + 1] == str(fw.fixtures[fw.module.TINY_DECODER[:2]])
