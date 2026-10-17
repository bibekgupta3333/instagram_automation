"""2.5D parallax (studio/stages/motion.py) and the render motion mode (STUDIO_MOTION).
Offline: depth is synthetic and parallax_clip is faked in the render tests, so no model loads.
The real-model clip test runs only with STUDIO_REAL_MODEL_TESTS=1 and the depth model present."""

import itertools
import json
import os
import shutil
import subprocess
from pathlib import Path

import cv2
import numpy as np
import pytest

from studio.config import DEFAULT_BRAND_DIR, Settings, load_brand
from studio.contracts import Beat, Script, VideoJob
from studio.pipeline.mock_llm import MockChatClient
from studio.stages import motion, render
from studio.stages.base import EngineUnavailable, StageContext, StageError

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("STUDIO_MOTION", "STUDIO_PARALLAX_AMP", "STUDIO_DEPTH_MODEL_DIR"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("STUDIO_SKIP_LEDGER", "1")


def ff(*args):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def probe(path: Path) -> dict:
    return json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-print_format", "json", "-show_streams",
         "-show_format", str(path)], capture_output=True, text=True, check=True).stdout)


# ---------------------------------------------------------------- presets


def test_presets_deterministic_rotating_and_complete():
    a = motion.preset_sequence("job-a", 12)
    assert a == motion.preset_sequence("job-a", 12)
    assert all(x != y for x, y in itertools.pairwise(a))  # never the same twice in a row
    assert set(a[:6]) == set(motion.PRESETS)  # every preset within one cycle
    assert [motion.preset_for("job-a", i) for i in range(12)] == a
    seqs = {tuple(motion.preset_sequence(f"job-{i}", 6)) for i in range(20)}
    assert len(seqs) > 5  # varies across jobs (not templated)


@pytest.mark.parametrize("preset", motion.PRESETS)
def test_camera_stays_inside_margin(preset):
    amp_px = 0.04 * 1080  # above the default, still within the margin
    margin = 0.7 * amp_px
    for t in np.linspace(0, 1, 11):
        c = motion.camera(preset, float(t), amp_px)
        for d in (0.0, 0.5, 1.0):
            assert abs(c["gx"] + c["px"] * d) <= margin
            assert abs(c["gy"] + c["py"] * d) <= margin
            assert 1.0 + c["z0"] + c["z1"] * d >= 1.0  # zoom never reveals the canvas edge


def test_dolly_directions():
    z = [motion.camera("dolly-in", t, 32)["z0"] for t in (0.0, 0.5, 1.0)]
    assert z[0] < z[1] < z[2]
    z = [motion.camera("dolly-out", t, 32)["z0"] for t in (0.0, 0.5, 1.0)]
    assert z[0] > z[1] > z[2]
    with pytest.raises(ValueError):
        motion.camera("spin", 0.5, 32)


# ---------------------------------------------------------------- model presence


def test_missing_depth_model(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hub"))
    assert not motion.available()
    img = tmp_path / "a.png"
    cv2.imwrite(str(img), np.zeros((64, 36, 3), np.uint8))
    with pytest.raises(EngineUnavailable, match="--stage video"):
        motion.parallax_clip(img, tmp_path / "c.mp4", 1.0, "short")


def test_model_dir_needs_all_files(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path))
    snap = tmp_path / "models--depth-anything--Depth-Anything-V2-Small-hf" / "snapshots" / "x"
    snap.mkdir(parents=True)
    (snap / "config.json").write_text("{}")
    with pytest.raises(EngineUnavailable):
        motion.model_dir()
    for f in ("preprocessor_config.json", "model.safetensors"):
        (snap / f).write_text("{}")
    assert motion.model_dir() == snap


# ---------------------------------------------------------------- edge handling


def synthetic(h=320, w=180):
    rng = np.random.default_rng(1)
    img = rng.integers(40, 90, (h, w, 3), dtype=np.uint8)
    d = np.full((h, w), 0.1, np.float32)
    img[100:220, 50:130] = (230, 230, 230)
    d[100:220, 50:130] = 0.95  # a near box in front of a far wall
    return img, d


def test_clean_plate_fills_only_behind_foreground():
    img, d = synthetic()
    plate, alpha, grown = motion.clean_plate(img, d, 180)
    assert alpha[160, 90] > 0.99 and alpha[10, 10] == 0.0
    assert grown[160, 90] and not grown[10, 10]
    assert np.array_equal(plate[:60], img[:60])  # far from the box: untouched
    assert plate[160, 90].mean() < 150  # behind the box: filled with wall, not white


def test_clean_plate_single_layer_when_no_foreground():
    img, _ = synthetic()
    plate, alpha, _ = motion.clean_plate(img, np.full(img.shape[:2], 0.3, np.float32), 180)
    assert plate is img and alpha.max() == 0.0


def test_condition_depth_grows_near_regions():
    _, d = synthetic()
    c = motion.condition_depth(d, 180)
    assert c[98, 90] > d[98, 90]  # just outside the box: pulled towards the near value
    assert abs(c[160, 90] - 0.95) < 0.02  # inside: unchanged (rigid foreground)


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
@pytest.mark.parametrize("fmt", ["short", "long"])
def test_render_clip_with_synthetic_depth(tmp_path, monkeypatch, fmt):
    img, d = synthetic()
    path = tmp_path / "s.png"
    cv2.imwrite(str(path), img)
    monkeypatch.setattr(motion, "model_dir", lambda: tmp_path)
    monkeypatch.setattr(motion, "depth_map", lambda image, cache_dir: d)
    out = motion.parallax_clip(path, tmp_path / "c.mp4", 0.4, fmt, "pan-left")
    v = [s for s in probe(out)["streams"] if s["codec_type"] == "video"]
    assert len(v) == 1 and len(probe(out)["streams"]) == 1  # video only
    assert (v[0]["width"], v[0]["height"]) == motion.frame_size(fmt)
    assert int(v[0]["nb_read_frames"]) == 12
    with pytest.raises(ValueError):
        motion.parallax_clip(path, tmp_path / "x.mp4", 0.4, fmt, "spin")


def test_depth_is_cached_per_image_hash(tmp_path, monkeypatch):
    img, d = synthetic()
    path = tmp_path / "s.png"
    cv2.imwrite(str(path), img)
    calls = []

    class FakeModel:
        def __call__(self, pixel_values):
            calls.append(1)
            import torch

            class R:
                predicted_depth = torch.from_numpy(d)[None]
            return R()

    monkeypatch.setattr(motion, "_load", lambda: (FakeModel(), lambda images, return_tensors: {
        "pixel_values": __import__("torch").zeros(1, 3, 14, 14)}, "cpu"))
    a = motion.depth_map(path, tmp_path / "cache")
    b = motion.depth_map(path, tmp_path / "cache")
    assert len(calls) == 1 and a.shape == img.shape[:2] and np.allclose(a, b)
    assert 0.0 <= a.min() and a.max() <= 1.0 and a[160, 90] > a[10, 10]


# ---------------------------------------------------------------- render motion mode


def ctx(tmp_path, tts="kokoro"):
    return StageContext(settings=Settings(output_dir=tmp_path), chat=MockChatClient(),
                        engines={"images": "mock", "tts": tts})


def test_motion_mode_selection(tmp_path, monkeypatch):
    monkeypatch.setattr(motion, "available", lambda: True)
    assert render.motion_mode(ctx(tmp_path))[0] == "parallax"
    assert render.motion_mode(ctx(tmp_path, tts="mock"))[0] == "zoompan"  # full --mock
    monkeypatch.setenv("STUDIO_MOTION", "parallax")
    assert render.motion_mode(ctx(tmp_path, tts="mock"))[0] == "parallax"
    monkeypatch.setenv("STUDIO_MOTION", "zoompan")
    assert render.motion_mode(ctx(tmp_path))[0] == "zoompan"
    monkeypatch.setenv("STUDIO_MOTION", "warp")
    with pytest.raises(StageError, match="STUDIO_MOTION"):
        render.motion_mode(ctx(tmp_path))
    monkeypatch.setenv("STUDIO_MOTION", "parallax")
    monkeypatch.setattr(motion, "available", lambda: False)
    mode, why = render.motion_mode(ctx(tmp_path))
    assert mode == "zoompan" and "not downloaded" in why


def make_render_job(tmp_path):
    from studio.stages import captions

    brand = load_brand("gravity-what-if", DEFAULT_BRAND_DIR)
    ff("-f", "lavfi", "-i", "sine=frequency=440:duration=5", "-ar", "48000",
       str(tmp_path / "a.wav"))
    imgs = tmp_path / "images"
    imgs.mkdir()
    for i, c in enumerate(("red", "blue")):
        ff("-f", "lavfi", "-i", f"color=c={c}:s=540x960:d=1", "-frames:v", "1", "-update", "1",
           str(imgs / f"beat_0{i}.png"))
    timings = {"segments": [{"beat": 0, "start": 0.0, "end": 1.5},
                            {"beat": 1, "start": 1.5, "end": 5.0}]}
    (tmp_path / "timings.json").write_text(json.dumps(timings))
    words = captions.mock_words({"segments": [{"text": "one two three", "start": 0, "end": 5}]},
                                5.0)
    captions.write_ass(words, brand, "short", tmp_path / "captions.ass")
    beats = [Beat(text=f"Beat {i}.", visual_prompt=f"v {i}") for i in range(2)]
    job = VideoJob(job_id="job-m", channel=brand.slug, format="short", topic="t",
                   script=Script(title="T", hook="H?", beats=beats, description="d"),
                   workdir=tmp_path)
    job.audio_path = tmp_path / "a.wav"
    job.captions_path = tmp_path / "captions.ass"
    job.image_paths = [imgs / "beat_00.png", imgs / "beat_01.png"]
    return job, brand


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_render_uses_parallax_clips_of_exact_length(tmp_path, monkeypatch):
    monkeypatch.setattr(motion, "available", lambda: True)
    made = []

    def fake_clip(image, out, seconds, fmt, preset=None, **kw):
        frames = round(seconds * motion.FPS)
        made.append((Path(image).name, frames, preset))
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        ff("-f", "lavfi", "-i", "testsrc=size=1080x1920:rate=30", "-frames:v", str(frames),
           "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast", str(out))
        return Path(out)

    monkeypatch.setattr(motion, "parallax_clip", fake_clip)
    job, brand = make_render_job(tmp_path)
    job = render.run(job, brand, ctx(tmp_path))
    plan = json.loads((tmp_path / "render_plan.json").read_text())
    assert plan and all(s["kind"] == "parallax" and s["motion"] in motion.PRESETS for s in plan)
    assert [m[1] for m in made] == [s["frames"] for s in plan]  # one clip per shot, exact
    assert sum(s["frames"] for s in plan) == 150
    assert [s["motion"] for s in plan] == motion.preset_sequence("job-m", len(plan))
    assert any("motion=parallax" in line for line in job.log)
    assert not (tmp_path / "motion").exists()  # intermediates cleaned up
    p = probe(job.video_path)
    assert abs(float(p["format"]["duration"]) - 5.0) < 0.2
    assert [s["codec_type"] for s in p["streams"]].count("audio") == 1
    first_hash = job.content_hash
    assert first_hash == render.compute_content_hash(job)

    # switching motion mode changes the cache key -> re-render (and a new hash)
    monkeypatch.setenv("STUDIO_MOTION", "zoompan")
    job = render.run(job, brand, ctx(tmp_path))
    plan = json.loads((tmp_path / "render_plan.json").read_text())
    assert all("kind" not in s and s["motion"] in render.MOTIONS for s in plan)
    assert job.content_hash != first_hash


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_render_falls_back_to_zoompan_when_parallax_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(motion, "available", lambda: True)

    def broken(*a, **kw):
        raise EngineUnavailable("motion: depth model gone")

    monkeypatch.setattr(motion, "parallax_clip", broken)
    job, brand = make_render_job(tmp_path)
    job = render.run(job, brand, ctx(tmp_path))
    plan = json.loads((tmp_path / "render_plan.json").read_text())
    assert all("kind" not in s and s["motion"] in render.MOTIONS for s in plan)
    assert any("motion=zoompan (parallax failed: motion: depth model gone)" in line
               for line in job.log)
    assert job.video_path.exists() and job.content_hash


@pytest.mark.skipif(os.environ.get("STUDIO_REAL_MODEL_TESTS") != "1" or not motion.available()
                    or not HAS_FFMPEG,
                    reason="set STUDIO_REAL_MODEL_TESTS=1 with the depth model downloaded")
def test_real_parallax_clip(tmp_path):
    img, _ = synthetic()
    path = tmp_path / "s.png"
    cv2.imwrite(str(path), img)
    out = motion.parallax_clip(path, tmp_path / "c.mp4", 1.0, "short", "orbit",
                               cache_dir=tmp_path / "depth")
    motion.unload()
    assert int(probe(out)["streams"][0]["nb_read_frames"]) == 30
    assert list((tmp_path / "depth").glob("*.npy"))
