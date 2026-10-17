"""Local mflux engines must never trigger an implicit multi-GB download (doc 18 §3)."""

import sys
import types
from pathlib import Path

import pytest

from studio.stages import images
from studio.stages.base import EngineUnavailable


@pytest.fixture(autouse=True)
def isolate_local_engines(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "mflux", types.ModuleType("mflux"))
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hub"))
    for name in ("STUDIO_MFLUX_CMD_Z_IMAGE_TURBO", "STUDIO_MFLUX_CMD_FLUX2_KLEIN_4B"):
        monkeypatch.delenv(name, raising=False)


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


def test_legacy_download_override_cannot_enable_downloads(monkeypatch):
    monkeypatch.setenv("STUDIO_ALLOW_MODEL_DOWNLOAD", "1")
    with pytest.raises(EngineUnavailable, match="not downloaded"):
        images._mflux_cmd("z-image-turbo")


@pytest.mark.parametrize("args", [
    "--model z-image-turbo -q 4", "--model Tongyi-MAI/Z-Image-Turbo",
    "--model mflux-community/z-image-turbo-mflux-q4 -q4", "--model", "",
])
def test_unsafe_mflux_override_rejected(monkeypatch, args):
    monkeypatch.setenv("STUDIO_MFLUX_CMD_Z_IMAGE_TURBO", f"mflux {args}")
    with pytest.raises(EngineUnavailable, match="installed"):
        images._mflux_cmd("z-image-turbo")


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
    assert images.gen_size("short") == (640, 1152)


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

    monkeypatch.setattr(images, "_mflux_cmd", lambda e, steps=None: ["mflux-generate-z-image-turbo"])
    monkeypatch.setattr(images.subprocess, "run", fake_run)


@pytest.fixture
def cached_mflux(monkeypatch, tmp_path):
    """Exercise the real command builder with fake cached weights and subprocess only."""
    seen = {}
    command_builder = images._mflux_cmd
    _fake_mflux(monkeypatch, seen)
    fake_run = images.subprocess.run
    seen["calls"] = []

    def record_run(cmd, **kwargs):
        seen["calls"].append(list(cmd))
        return fake_run(cmd, **kwargs)

    monkeypatch.setattr(images.subprocess, "run", record_run)
    monkeypatch.setattr(images, "_mflux_cmd", command_builder)
    monkeypatch.setattr(images, "_resolve_cli", lambda name: str(tmp_path / name))
    for repo in (
        "mflux-community/z-image-turbo-mflux-q4",
        "mflux-community/flux2-klein-4b-mflux-q4",
    ):
        snapshot = tmp_path / "hub" / f"models--{repo.replace('/', '--')}" / "snapshots" / "test"
        snapshot.mkdir(parents=True)
        (snapshot / "transformer.safetensors").write_bytes(b"fake weights; never loaded")
    return seen


@pytest.mark.parametrize(("flag", "value"), [
    ("--low-ram", None),
    ("--vae-tile-size", "256"),
    ("--mlx-cache-limit-gb", "0.25"),
])
def test_zimage_default_command_has_low_memory_limits(cached_mflux, flag, value):
    cmd = images._mflux_cmd("z-image-turbo")
    assert cmd.count(flag) == 1
    if value is not None:
        assert cmd[cmd.index(flag) + 1] == value


def test_flux_default_command_is_unchanged(cached_mflux):
    cmd = images._mflux_cmd("flux2-klein-4b", steps=12)
    assert Path(cmd[0]).name == "mflux-generate-flux2"
    assert cmd[1:] == [
        "--model", "mflux-community/flux2-klein-4b-mflux-q4",
        "--base-model", "flux2-klein-4b", "--steps", "4", "--guidance", "1.0",
    ]


@pytest.mark.parametrize(("engine", "repo"), [
    ("z-image-turbo", "mflux-community/z-image-turbo-mflux-q4"),
    ("flux2-klein-4b", "mflux-community/flux2-klein-4b-mflux-q4"),
])
def test_cached_mflux_invocations_keep_one_seed_q4_and_offline(
    tmp_path, monkeypatch, cached_mflux, engine, repo,
):
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    monkeypatch.setenv("STUDIO_ALLOW_MODEL_DOWNLOAD", "1")
    for invocation, seed in enumerate((123, 124), start=1):
        out = tmp_path / f"seed_{seed}.png"
        images._mflux_image(out, "a dam, no text", seed, engine, "short")
        cmd = cached_mflux["cmd"]
        assert len(cached_mflux["calls"]) == invocation
        assert cmd.count("--seed") == 1
        # A second positional seed would be consumed by mflux's multi-seed parser.
        seed_index = cmd.index("--seed")
        assert cmd[seed_index + 1] == str(seed)
        assert cmd[seed_index + 2].startswith("--")
        assert cmd.count("--model") == 1
        assert cmd[cmd.index("--model") + 1] == repo
        assert not any(arg.startswith(("-q", "--quantiz")) for arg in cmd[1:])
        assert cached_mflux["env"]["HF_HUB_OFFLINE"] == "1"
        assert cmd[cmd.index("--output") + 1] == str(out)
        assert out.exists()


@pytest.mark.parametrize("engine", ["z-image-turbo", "flux2-klein-4b"])
def test_mflux_missing_cache_never_starts_subprocess(tmp_path, monkeypatch, engine):
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    monkeypatch.setenv("STUDIO_ALLOW_MODEL_DOWNLOAD", "1")
    monkeypatch.setattr(images.subprocess, "run", lambda *a, **kw: pytest.fail("model launched"))
    out = tmp_path / "missing.png"
    with pytest.raises(EngineUnavailable, match="not downloaded"):
        images._mflux_image(out, "a dam, no text", 123, engine, "short")
    assert not out.exists()


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
    assert (cmd[cmd.index("--width") + 1], cmd[cmd.index("--height") + 1]) == ("640", "1152")
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


def test_mflux_subprocess_always_offline(tmp_path, monkeypatch):
    seen = {}
    _fake_mflux(monkeypatch, seen)
    monkeypatch.setenv("STUDIO_ALLOW_MODEL_DOWNLOAD", "1")
    images._mflux_image(tmp_path / "raw.png", "no text", 1, "z-image-turbo", "short")
    assert seen["env"]["HF_HUB_OFFLINE"] == "1"


@pytest.mark.parametrize("fail", [False, True])
def test_upscaler_unloads_before_generation_and_after_upscale(tmp_path, monkeypatch, fail):
    calls = []
    monkeypatch.setattr(images, "unload", lambda: calls.append("unload"))
    monkeypatch.setattr(images, "_mflux_image", lambda *a: calls.append("generate"))

    def upscale(*args):
        calls.append("upscale")
        if fail:
            raise RuntimeError("upscale failed")
        return True

    monkeypatch.setattr(images, "_to_frame", upscale)
    if fail:
        with pytest.raises(RuntimeError, match="upscale failed"):
            images._produce("z-image-turbo", _spec(tmp_path), None)
    else:
        images._produce("z-image-turbo", _spec(tmp_path), None)
    assert calls == ["unload", "generate", "upscale", "unload"]


def test_cleanup_never_imports_gpu_runtime(monkeypatch):
    import builtins

    from studio.stages import motion
    from studio.stages.visuals import upscale

    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name.split(".")[0] in {"torch", "mlx", "mflux"}:
            pytest.fail(f"cleanup imported {name}")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    for module in (motion, upscale):
        monkeypatch.setattr(module, "_device", "mps")
        module.unload()
        assert module._model is None and module._device is None
    images.unload()


@pytest.mark.parametrize(("value", "expected"), [(None, 3), ("2", 2), ("0", 1), ("bad", 3)])
def test_cpu_thread_limits_use_only_supplied_modules(monkeypatch, value, expected):
    from studio.stages.visuals.common import configure_cpu_threads

    if value is None:
        monkeypatch.delenv("STUDIO_CPU_THREADS", raising=False)
    else:
        monkeypatch.setenv("STUDIO_CPU_THREADS", value)
    calls = []
    configure_cpu_threads(cv2=types.SimpleNamespace(setNumThreads=lambda n: calls.append(n)),
                          torch=types.SimpleNamespace(set_num_threads=lambda n: calls.append(n)))
    assert calls == [expected, expected]
