"""Local mflux engines must never trigger an implicit multi-GB download (doc 18 §3)."""

import sys
import types
from pathlib import Path

import pytest

from studio.stages import images
from studio.stages.base import EngineUnavailable


def test_mflux_models_use_prequantized_repos_without_q_flag():
    for _name, cmd in images.MFLUX_MODELS.values():
        repo = images._mflux_repo(cmd)
        assert repo and repo.startswith("mflux-community/") and repo.endswith("-q4")
        assert "-q" not in cmd


def test_hf_cached_detects_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    repo = "mflux-community/flux2-klein-4b-mflux-q4"
    assert not images._hf_cached(repo)
    snap = tmp_path / "hub" / "models--mflux-community--flux2-klein-4b-mflux-q4" / "snapshots" / "x"
    snap.mkdir(parents=True)
    (snap / "transformer.safetensors").write_bytes(b"0")
    assert images._hf_cached(repo)


def test_missing_model_raises_instead_of_downloading(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "mflux", types.ModuleType("mflux"))
    monkeypatch.setattr(images.shutil, "which", lambda _c: "/usr/local/bin/mflux-generate-flux2")
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    monkeypatch.delenv("STUDIO_ALLOW_MODEL_DOWNLOAD", raising=False)
    with pytest.raises(EngineUnavailable, match="not downloaded"):
        images._mflux_cmd("flux2-klein-4b")


def test_z_image_turbo_is_default_and_sizes_are_multiples_of_16():
    from studio.config import DEFAULT_AUTO_ORDER

    assert DEFAULT_AUTO_ORDER[0] == "z-image-turbo"
    cmd = images.MFLUX_MODELS["z-image-turbo"][1]
    assert cmd[0] == "mflux-generate-z-image-turbo"
    assert cmd[cmd.index("--model") + 1] == "mflux-community/z-image-turbo-mflux-q4"
    assert cmd[cmd.index("--steps") + 1] == "9"
    for fmt in ("short", "long"):
        w, h = images.gen_size(fmt)
        assert w % 16 == 0 and h % 16 == 0
    assert images.gen_size("short") == (576, 1024)


def test_cli_prefers_the_venv_bin(tmp_path, monkeypatch):
    exe = tmp_path / "mflux-generate-z-image-turbo"
    exe.write_text("")
    monkeypatch.setattr(images.sys, "executable", str(tmp_path / "python"))
    assert images._resolve_cli("mflux-generate-z-image-turbo") == str(exe)
    monkeypatch.setattr(images.shutil, "which", lambda _c: None)
    assert images._resolve_cli("mflux-generate-missing") is None


def _fake_mflux(monkeypatch, seen):
    def fake_run(cmd, **kw):
        seen.update(cmd=cmd, env=kw["env"])
        out = cmd[cmd.index("--output") + 1]
        from PIL import Image

        Image.new("RGB", (576, 1024), "blue").save(out)
        return types.SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(images, "_mflux_cmd", lambda e: ["mflux-generate-z-image-turbo"])
    monkeypatch.setattr(images.subprocess, "run", fake_run)


def _spec(tmp_path):
    from studio.stages.visuals import BeatSpec

    return BeatSpec(job_id="j", channel="c", index=0, fmt="short", visual_prompt="dam",
                    prompt="dam, blueprint", out_stem=tmp_path / "beat_00", style_prompt="")


def test_mflux_runs_offline_and_upscales_raw_still(tmp_path, monkeypatch):
    seen: dict = {}
    _fake_mflux(monkeypatch, seen)
    monkeypatch.delenv("STUDIO_ALLOW_MODEL_DOWNLOAD", raising=False)
    calls = []
    fake_up = types.ModuleType("studio.stages.visuals.upscale")

    def upscale_to(src, dst, w, h):
        calls.append((Path(src).name, Path(dst).name, w, h))
        Path(dst).write_bytes(Path(src).read_bytes())
        return Path(dst)

    fake_up.upscale_to = upscale_to
    monkeypatch.setitem(sys.modules, "studio.stages.visuals.upscale", fake_up)
    vis = images._produce("z-image-turbo", _spec(tmp_path), None)
    assert seen["env"]["HF_HUB_OFFLINE"] == "1"
    cmd = seen["cmd"]
    assert (cmd[cmd.index("--width") + 1], cmd[cmd.index("--height") + 1]) == ("576", "1024")
    assert (tmp_path / "raw" / "beat_00.png").exists() and vis.path == tmp_path / "beat_00.png"
    assert calls == [("beat_00.png", "beat_00.png", 1080, 1920)]
    assert vis.model.endswith(images.UPSCALED_SUFFIX)


def test_upscale_unavailable_falls_back_to_raw_copy(tmp_path, monkeypatch):
    seen: dict = {}
    _fake_mflux(monkeypatch, seen)
    fake_up = types.ModuleType("studio.stages.visuals.upscale")

    def upscale_to(*a):
        raise EngineUnavailable("upscale: Real-ESRGAN not downloaded")

    fake_up.upscale_to = upscale_to
    monkeypatch.setitem(sys.modules, "studio.stages.visuals.upscale", fake_up)
    vis = images._produce("z-image-turbo", _spec(tmp_path), None)
    assert vis.path.read_bytes() == (tmp_path / "raw" / "beat_00.png").read_bytes()
    assert vis.model == images.MFLUX_MODELS["z-image-turbo"][0]
