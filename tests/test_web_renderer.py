"""Offline renderer/provider contract tests; no model loads, GPU or network."""
import copy
import io
import json
import shutil
import subprocess
import sys
import wave
from unittest.mock import Mock

import pytest
from PIL import Image, ImageChops

from studio.web import providers, renderer, worker


@pytest.fixture
def project():
    return {"revision": 4, "format": "landscape", "resolution": "draft",
            "characters": [{"id": "pebble", "species": "otter", "color": "#AD8050",
                            "accessory": "scarf"}],
            "scenes": [{"id": "hello", "title": "Hello", "duration": 1,
                        "narration": "A little ripple", "caption": "Hello, pond!",
                        "background": "pond", "motion": "bounce", "camera": "wide",
                        "character_ids": ["pebble"]}],
            "audio": {"mode": "guide", "asset_id": None, "volume": .7},
            "review": dict.fromkeys(("originality", "rights", "child_safety", "lyrics", "visuals"),
                                    False)}


@pytest.mark.parametrize("species", ["otter", "rabbit", "bear", "frog"])
@pytest.mark.parametrize("accessory", ["none", "bow", "scarf"])
def test_preview_original_square_png(species, accessory):
    data = renderer.character_preview({"species": species, "color": "#65AD96",
                                       "accessory": accessory})
    with Image.open(io.BytesIO(data)) as image:
        assert image.format == "PNG"
        assert image.size == (512, 512)
        assert image.getextrema()[0][0] != image.getextrema()[0][1]
    assert data == renderer.character_preview({"species": species, "color": "#65AD96",
                                               "accessory": accessory})


def test_species_color_and_accessory_change_drawing():
    base = {"species": "otter", "color": "#AA8855", "accessory": "none"}
    variants = [base, {**base, "species": "rabbit"}, {**base, "species": "bear"},
                {**base, "species": "frog"}, {**base, "color": "#9955BB"},
                {**base, "accessory": "bow"}, {**base, "accessory": "scarf"}]
    assert len({renderer.character_preview(c) for c in variants}) == len(variants)


@pytest.mark.parametrize("motion", ["bounce", "sway", "float"])
def test_frames_animate_without_retaining_movie(project, motion):
    project["scenes"][0]["motion"] = motion
    frames = renderer.frame_generator(project, (160, 90), {})
    first = next(frames)
    later = next(frames)
    for _ in range(10):
        later = next(frames)
    assert ImageChops.difference(first, later).getbbox()
    frames.close()
    assert ImageChops.difference(renderer._character(project["characters"][0], 0),
                                renderer._character(project["characters"][0], 3)).getbbox()


def test_only_current_scene_assets_loaded(project, tmp_path, monkeypatch):
    asset = tmp_path / "sprite.png"
    Image.new("RGBA", (80, 60), "#BBCCAA").save(asset)
    project["characters"][0]["asset_id"] = "asset"
    scene = copy.deepcopy(project["scenes"][0])
    scene["id"] = "later"
    project["scenes"].append(scene)
    opened = Mock(wraps=Image.open)
    monkeypatch.setattr(renderer.Image, "open", opened)
    frames = renderer.frame_generator(project, (160, 90), {"asset": str(asset)})
    next(frames)
    assert opened.call_count == 1
    for _ in range(30):
        next(frames)
    assert opened.call_count == 2
    frames.close()


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
                    reason="ffmpeg/ffprobe required for CPU-only media test")
@pytest.mark.parametrize("uploaded", [False, True])
def test_real_cpu_mp4_playable_animated_with_audio(project, tmp_path, monkeypatch, uploaded):
    monkeypatch.setattr(renderer.platform, "system", lambda: "Linux")
    request = {"kind": "render", "format": "portrait" if uploaded else "landscape"}
    if uploaded:
        audio = tmp_path / "owned.wav"
        # Shorter than the animation: renderer pads, trims and fades it.
        with wave.open(str(audio), "wb") as stream:
            stream.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
            stream.writeframes(b"\x01\x10" * 2000)
        project["audio"].update(mode="uploaded", asset_id="audio")
        project["review"] = dict.fromkeys(project["review"], True)
        request["asset_paths"] = {"audio": str(audio)}
    commands = []
    popen = subprocess.Popen

    def capture(command, **kwargs):
        commands.append((command, kwargs))
        return popen(command, **kwargs)

    monkeypatch.setattr(renderer.subprocess, "Popen", capture)
    result = worker.run(project, request, tmp_path)
    encoder_command, options = commands[0]
    assert "libx264" in encoder_command and "h264_videotoolbox" not in encoder_command
    for flag in ("-threads", "-filter_threads", "-filter_complex_threads"):
        assert encoder_command[encoder_command.index(flag) + 1] == "3"
    assert not options.get("start_new_session") and not options.get("process_group")
    assert options.get("preexec_fn") is None
    probe = subprocess.run([shutil.which("ffprobe"), "-v", "error", "-show_streams", "-of", "json",
                            str(tmp_path / "video.mp4")], capture_output=True, check=True)
    streams = json.loads(probe.stdout)["streams"]
    video = next(s for s in streams if s["codec_type"] == "video")
    audio = next(s for s in streams if s["codec_type"] == "audio")
    assert video["codec_name"] == "h264" and audio["codec_name"] == "aac"
    assert video["r_frame_rate"] == "30/1" and int(video["nb_frames"]) == 30
    assert (video["width"], video["height"]) == ((360, 640) if uploaded else (640, 360))
    assert abs(float(video["duration"]) - 1) < .05
    decoded = subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-threads", "3",
                              "-filter_threads", "3", "-i", str(tmp_path / "video.mp4"),
                              "-vf", "select=eq(n\\,0)+eq(n\\,15),scale=64:64", "-vsync", "0",
                              "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
                             capture_output=True, check=True).stdout
    assert len(decoded) == 64 * 64 * 3 * 2
    assert decoded[:len(decoded)//2] != decoded[len(decoded)//2:]
    assert result["guide_audio"] is not uploaded
    assert result["review_ready"] is uploaded and result["draft"] is not uploaded
    assert result["made_for_kids"] and result["ai_disclosure"]
    assert not result["approved"] and not result["published"]
    assert json.loads((tmp_path / "result.json").read_text()) == result
    assert json.loads((tmp_path / "metadata.json").read_text()) == result
    assert json.loads((tmp_path / "progress.json").read_text())["progress"] == 100
    assert not list(tmp_path.glob("*.tmp"))
    assert "00:00:00,000 --> 00:00:01,000" in (tmp_path / "captions.srt").read_text()
    assert Image.open(tmp_path / "poster.png").size == (video["width"], video["height"])


def test_videotoolbox_failure_restarts_generator(project, tmp_path, monkeypatch):
    monkeypatch.setattr(renderer.platform, "system", lambda: "Darwin")
    calls = []

    def encode(project, request, out, size, audio, seconds, encoder, progress):
        frames = renderer.frame_generator(project, size, {})
        calls.append((encoder, next(frames).tobytes()))
        frames.close()
        if encoder == "h264_videotoolbox":
            raise renderer.EncoderError("encoder init failed")

    monkeypatch.setattr(renderer, "_encode", encode)
    renderer.render_project(project, {}, tmp_path)
    assert [c[0] for c in calls] == ["h264_videotoolbox", "libx264"]
    assert calls[0][1] == calls[1][1]


def test_frame_exception_terminates_encoder_and_never_falls_back(project, tmp_path, monkeypatch):
    process = Mock()
    process.poll.return_value = None
    monkeypatch.setattr(renderer.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(renderer.platform, "system", lambda: "Darwin")

    def broken(*args):
        raise ValueError("bad raster")
        yield  # make a closable frame iterator

    monkeypatch.setattr(renderer, "frame_generator", broken)
    with pytest.raises(ValueError, match="bad raster"):
        worker.run(project, {"kind": "render"}, tmp_path)
    process.terminate.assert_called_once()
    process.wait.assert_called_once()
    assert renderer.subprocess.Popen.call_count == 1
    assert not (tmp_path / "result.json").exists()


@pytest.mark.parametrize("orientation,size", [("landscape", (1920, 1080)),
                                               ("portrait", (1080, 1920))])
def test_final_dimensions_without_heavy_render(project, tmp_path, monkeypatch, orientation, size):
    encode = Mock()
    monkeypatch.setattr(renderer, "_encode", encode)
    result = renderer.render_project(project, {"format": orientation, "resolution": "final"}, tmp_path)
    assert encode.call_args.args[3] == size
    assert (result["width"], result["height"]) == size
    assert result["draft"]  # final resolution does not grant review readiness


@pytest.mark.parametrize("mutation", ["empty", "long", "nan", "negative", "unknown", "duplicate"])
def test_invalid_snapshots_never_spawn(project, tmp_path, monkeypatch, mutation):
    if mutation == "empty":
        project["scenes"] = []
    elif mutation == "long":
        project["scenes"] = [{**project["scenes"][0], "id": f"s{i}", "duration": 15}
                             for i in range(9)]
    elif mutation == "nan":
        project["scenes"][0]["duration"] = float("nan")
    elif mutation == "negative":
        project["audio"]["volume"] = -1
    elif mutation == "unknown":
        project["scenes"][0]["character_ids"] = ["unknown"]
    else:
        project["characters"] *= 2
    spawn = Mock()
    monkeypatch.setattr(renderer.subprocess, "Popen", spawn)
    with pytest.raises(ValueError):
        worker.run(project, {"kind": "render"}, tmp_path)
    spawn.assert_not_called()
    assert not (tmp_path / "result.json").exists()


def test_missing_uploaded_audio_never_becomes_guide(project, tmp_path):
    project["audio"].update(mode="uploaded", asset_id="absent")
    with pytest.raises(ValueError, match="unavailable"):
        worker.run(project, {"kind": "render"}, tmp_path)
    assert not (tmp_path / "guide.wav").exists()


def test_worker_character_cli_and_failure_remove_stale_result(project, tmp_path):
    p, r = tmp_path / "project.json", tmp_path / "request.json"
    p.write_text(json.dumps(project))
    r.write_text(json.dumps({"kind": "character", "character_id": "pebble",
                             "provider": "illustration"}))
    assert worker.main(["--project", str(p), "--request", str(r), "--out", str(tmp_path)]) == 0
    assert Image.open(tmp_path / "character.png").size == (512, 512)
    assert json.loads((tmp_path / "result.json").read_text())["kind"] == "character"
    r.write_text(json.dumps({"kind": "bad"}))
    assert worker.main(["--project", str(p), "--request", str(r), "--out", str(tmp_path)]) == 1
    assert not (tmp_path / "result.json").exists()


def test_mflux_command_offline_q4_small_memory(project, tmp_path, monkeypatch):
    monkeypatch.setattr(providers.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(providers, "cached_klein", lambda: tmp_path)
    monkeypatch.setattr(providers, "_mflux_executable", lambda: "/installed/mflux-generate-flux2")
    character = {**project["characters"][0], "description": "$(do not execute); mimic franchise"}
    command = providers.mflux_command(character, tmp_path / "character.png")
    assert "-q" not in command and "--quantize" not in command
    assert command[command.index("--width") + 1] == "512"
    assert command[command.index("--height") + 1] == "512"
    assert "--low-ram" in command
    assert command[command.index("--mlx-cache-limit-gb") + 1] == "0.25"
    assert "no text" in " ".join(command).lower()
    assert "$(" not in " ".join(command)
    env = providers.offline_environment()
    assert env["HF_HUB_OFFLINE"] == "1" and env["STUDIO_ALLOW_MODEL_DOWNLOAD"] == "0"
    process = Mock()
    process.wait.return_value = 0
    process.poll.return_value = 0
    popen = Mock(return_value=process)
    monkeypatch.setattr(providers.subprocess, "Popen", popen)
    providers._run(command, tmp_path)
    options = popen.call_args.kwargs
    assert not options.get("shell") and not options.get("start_new_session")
    assert not options.get("process_group")
    assert options["env"]["HF_HUB_OFFLINE"] == "1"


def test_missing_mflux_cache_is_explicit_no_spawn(project, tmp_path, monkeypatch):
    monkeypatch.setattr(providers.platform, "system", lambda: "Darwin")
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "empty-cache"))
    spawn = Mock()
    monkeypatch.setattr(providers.subprocess, "Popen", spawn)
    with pytest.raises(RuntimeError, match="cached"):
        providers.generate_character(project["characters"][0], "mflux", tmp_path)
    spawn.assert_not_called()
    assert not next(p for p in providers.capabilities() if p["id"] == "mflux")["available"]


def test_cached_klein_rejects_broken_weights(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path))
    root = tmp_path / ("models--" + providers.KLEIN_REPO.replace("/", "--"))
    (root / "refs").mkdir(parents=True)
    (root / "refs/main").write_text("abc123")
    snapshot = root / "snapshots/abc123"
    snapshot.mkdir(parents=True)
    for component in ("transformer", "text_encoder", "vae"):
        directory = snapshot / component
        directory.mkdir()
        (directory / "model.safetensors.index.json").write_text(
            json.dumps({"weight_map": {"fake": "0.safetensors"}}))
        (directory / "0.safetensors").write_bytes(b"fake-weight-never-loaded")
    (snapshot / "tokenizer").mkdir()
    for filename in ("tokenizer.json", "tokenizer_config.json"):
        (snapshot / "tokenizer" / filename).write_text("{}")
    assert not (snapshot / "config.json").exists()
    assert providers.cached_klein() == snapshot
    weight = snapshot / "transformer/0.safetensors"
    weight.unlink()
    assert providers.cached_klein() is None  # index references a missing shard
    weight.symlink_to(snapshot / "missing-blob")
    assert providers.cached_klein() is None
    weight.unlink()
    weight.write_bytes(b"fake-weight-never-loaded")
    assert providers.cached_klein() == snapshot
    (snapshot / "vae/model.safetensors.index.json").write_text("broken json")
    assert providers.cached_klein() is None


def test_linux_capability_is_explicit_unverified(tmp_path, monkeypatch):
    monkeypatch.setattr(providers.platform, "system", lambda: "Linux")
    monkeypatch.setenv("STUDIO_WEB_DIFFUSERS_MODEL", str(tmp_path))
    (tmp_path / "model_index.json").write_text("{}")
    monkeypatch.setattr(providers.importlib.util, "find_spec", lambda _: object())
    info = next(p for p in providers.capabilities() if p["id"] == "diffusers")
    assert info["available"] and "unverified" in info["reason"]
    monkeypatch.setenv("STUDIO_WEB_DIFFUSERS_MODEL", "remote/model")
    assert providers._diffusers_model() is None


def test_diffusers_only_local_and_lazy_mocked(project, tmp_path, monkeypatch):
    monkeypatch.setattr(providers.platform, "system", lambda: "Linux")
    monkeypatch.setenv("STUDIO_WEB_DIFFUSERS_MODEL", str(tmp_path))
    (tmp_path / "model_index.json").write_text("{}")
    torch = Mock()
    torch.cuda.is_available.return_value = True
    torch.inference_mode.return_value.__enter__ = Mock()
    torch.inference_mode.return_value.__exit__ = Mock(return_value=False)
    pipeline = Mock()
    pipeline.return_value.images = [Image.new("RGB", (512, 512), "#BB9966")]
    cls = Mock()
    cls.from_pretrained.return_value = pipeline
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "diffusers", Mock(Flux2KleinPipeline=cls))
    providers._diffusers_generate(project["characters"][0], tmp_path / "character.png")
    assert cls.from_pretrained.call_args.args == (str(tmp_path),)
    assert cls.from_pretrained.call_args.kwargs["local_files_only"] is True
    pipeline.enable_model_cpu_offload.assert_called_once()
    torch.cuda.empty_cache.assert_called_once()


def test_mflux_runtime_fallback_from_web_venv(tmp_path, monkeypatch):
    monkeypatch.delenv("STUDIO_WEB_MFLUX_BIN", raising=False)
    monkeypatch.setattr(providers, "__file__", str(tmp_path / "studio/web/providers.py"))
    runtime = tmp_path / ".venv/bin/mflux-generate-flux2"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("#!/bin/sh\nexit 0\n")
    runtime.chmod(0o700)
    monkeypatch.setattr(providers.sys, "executable", str(tmp_path / ".venv-web/bin/python"))
    monkeypatch.setattr(providers.shutil, "which", lambda _: None)
    executable = providers._mflux_executable()
    assert executable is not None and executable.endswith("/.venv/bin/mflux-generate-flux2")


def test_trusted_runtime_override(tmp_path, monkeypatch):
    executable = tmp_path / "trusted-mflux"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    monkeypatch.setenv("STUDIO_WEB_MFLUX_BIN", str(executable))
    assert providers._mflux_executable() == str(executable)
    monkeypatch.setenv("STUDIO_WEB_MFLUX_BIN", "relative/mflux")
    assert providers._mflux_executable() is None


def test_public_provider_symbols_stable():
    assert providers.available_providers is providers.capabilities
