"""Images stage engines (auto fallback: local Z-Image -> Pixabay stock -> mock; removed Bedrock
engine names) and mixed stills + clips render. Offline: mflux and the stock fetch are faked. The
Pixabay engine itself is tested in tests/test_pixabay.py; local mflux in test_images_local.py."""

import base64
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from studio.config import (
    DEFAULT_BRAND_DIR,
    RemovedEngineError,
    Settings,
    VisualsConfig,
    load_brand,
)
from studio.contracts import Beat, Script, VideoJob
from studio.pipeline.mock_llm import MockChatClient
from studio.stages import images, render
from studio.stages.base import EngineUnavailable, StageContext, StageError
from studio.stages.visuals import BeatSpec, Visual

# 1x1 PNG
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path_factory):
    for k in list(os.environ):
        if k.startswith(("STUDIO_IMAGES", "STUDIO_PIXABAY", "STUDIO_MFLUX_CMD",
                         "STUDIO_ALLOW_MODEL", "STUDIO_REALESRGAN")):
            monkeypatch.delenv(k, raising=False)
    # empty HF cache: `auto` tries local Z-Image first, and must never run real mflux in tests
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path_factory.mktemp("hf-hub")))
    monkeypatch.delenv("PIXABAY_API_KEY", raising=False)


def brand(slug="car-cost-truth"):
    return load_brand(slug, DEFAULT_BRAND_DIR)


def spec(tmp_path, fmt="short", index=0, visual="a red pickup truck on a desert highway"):
    b = brand()
    return BeatSpec(job_id="job-x", channel=b.slug, index=index, fmt=fmt, visual_prompt=visual,
                    prompt=images.image_prompt(visual, b), out_stem=tmp_path / f"beat_{index:02d}",
                    style_prompt=b.style_prompt)


def make_job(tmp_path, n=2, fmt="short", visual="a red pickup truck on a desert highway"):
    beats = [Beat(text=f"Beat {i} text.", visual_prompt=f"{visual} {i}") for i in range(n)]
    script = Script(title="T", hook="Hook?", beats=beats, description="d")
    return VideoJob(job_id="job-x", channel="car-cost-truth", format=fmt, topic="t",
                    script=script, workdir=tmp_path)


def ctx(tmp_path, engine):
    return StageContext(settings=Settings(output_dir=tmp_path), chat=MockChatClient(),
                        engines={"images": engine})


# ---------------------------------------------------------------- auto order + stage


def fake_visual(engine, kind="image"):
    def _fn(s, *a, **kw):
        out = s.out_stem.with_suffix(".mp4" if kind == "video" else ".png")
        out.write_bytes(PNG)
        src = ({"id": s.index, "credit": f"Video by X on Pixabay (id {s.index}): u{s.index}"}
               if engine == "pixabay" else {})
        return Visual(path=out, kind=kind, engine=engine, model=engine, source=src)
    return _fn


def fake_zimage(monkeypatch, calls, fail_beats=()):
    """Fake local mflux: writes the raw still; beats in `fail_beats` raise like a failed run."""
    def _mflux(raw, prompt, seed, engine, fmt):
        index = int(raw.stem.split("_")[1])
        calls.append((engine, index))
        if index in fail_beats:
            raise StageError(f"mflux failed for {raw.name}")
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_bytes(PNG)
    monkeypatch.setattr(images, "_mflux_image", _mflux)
    monkeypatch.setattr(images, "_to_frame",
                        lambda raw, out, fmt: (shutil.copyfile(raw, out), False)[1])


def test_auto_order_zimage_then_pixabay(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setenv("PIXABAY_API_KEY", "k")
    fake_zimage(monkeypatch, calls, fail_beats={1})

    def pix(s, **kw):
        calls.append(("pixabay", s.index))
        return fake_visual("pixabay", "video")(s)

    monkeypatch.setattr(images.pixabay_src, "fetch", pix)
    job = make_job(tmp_path, n=2)
    job = images.run(job, brand(), ctx(tmp_path, "auto"))
    assert calls == [("z-image-turbo", 0), ("z-image-turbo", 1), ("pixabay", 1)]
    assert [p.suffix for p in job.image_paths] == [".png", ".mp4"]
    manifest = json.loads((tmp_path / "images" / "visuals.json").read_text())
    assert [b["visual"]["engine"] for b in manifest["beats"]] == ["z-image-turbo", "pixabay"]
    assert all(b["visual"]["usd"] == 0 for b in manifest["beats"])
    assert "on Pixabay" in (tmp_path / "images" / "attribution.txt").read_text()
    assert "$" not in job.log[-2]  # no image spend is tracked any more

    # resume: nothing is regenerated or re-fetched
    monkeypatch.setattr(images, "_mflux_image", lambda *a, **k: pytest.fail("regenerated"))
    monkeypatch.setattr(images.pixabay_src, "fetch", lambda *a, **k: pytest.fail("re-fetched"))
    job = images.run(job, brand(), ctx(tmp_path, "auto"))
    assert job.log[-1].endswith("images: cached, skipped")


def test_auto_skips_stock_for_unsuitable_brand(tmp_path, monkeypatch):
    monkeypatch.setenv("PIXABAY_API_KEY", "k")
    monkeypatch.setattr(images.pixabay_src, "fetch", lambda *a, **k: pytest.fail("stock used"))
    monkeypatch.setattr(images, "_mock_image", lambda out, *a: out.write_bytes(PNG))
    job = make_job(tmp_path, n=2)
    job.channel = "furnace-to-faucet"
    with pytest.warns(UserWarning, match="MOCK PLACEHOLDER"):
        job = images.run(job, brand("furnace-to-faucet"), ctx(tmp_path, "auto"))
    engines = [b["visual"]["engine"] for b in
               json.loads((tmp_path / "images" / "visuals.json").read_text())["beats"]]
    assert engines == ["mock", "mock"]  # Z-Image not downloaded, stock unsuitable


def test_auto_without_keys_is_loud_mock(tmp_path, monkeypatch):
    monkeypatch.setattr(images, "_mock_image", lambda out, *a: out.write_bytes(PNG))
    job = make_job(tmp_path, n=1)
    with pytest.warns(UserWarning, match="MOCK PLACEHOLDER"):
        job = images.run(job, brand(), ctx(tmp_path, "auto"))
    assert any("WARNING" in line and "no PIXABAY_API_KEY" in line for line in job.log)
    # a later run with a key must retry instead of reusing the placeholder
    monkeypatch.setenv("PIXABAY_API_KEY", "k")
    monkeypatch.setattr(images.pixabay_src, "fetch", fake_visual("pixabay", "video"))
    job = images.run(job, brand(), ctx(tmp_path, "auto"))
    assert "pixabay/video=1" in job.log[-2]


def test_explicit_engine_does_not_fall_back(tmp_path):
    with pytest.raises(EngineUnavailable):
        images.run(make_job(tmp_path, n=1), brand(), ctx(tmp_path, "pixabay"))
    with pytest.raises(StageError, match="unknown engine"):
        images.run(make_job(tmp_path, n=1), brand(), ctx(tmp_path, "dalle"))


def test_default_engine_is_auto(monkeypatch):
    assert Settings.from_env().engines["images"] == "auto"
    assert VisualsConfig.from_env().auto_order == ("z-image-turbo", "pixabay", "mock")
    assert not any(e.startswith("bedrock") for e in images.ENGINES)
    monkeypatch.setenv("STUDIO_IMAGES_ENGINE", "z-image-turbo")
    assert Settings.from_env().engines["images"] == "z-image-turbo"
    monkeypatch.setenv("STUDIO_IMAGES_AUTO_ORDER", "pixabay, mock")
    assert VisualsConfig.from_env().auto_order == ("pixabay", "mock")


REMOVED_MSG = "removed 2026-10-16: Bedrock is text-only; use z-image-turbo or pixabay"


@pytest.mark.parametrize("name", ["bedrock-stability", "bedrock-luma"])
def test_removed_bedrock_engines_raise_clear_error(tmp_path, monkeypatch, name):
    with pytest.raises(StageError, match=REMOVED_MSG):  # --engine images=NAME
        images.run(make_job(tmp_path, n=1), brand(), ctx(tmp_path, name))
    monkeypatch.setenv("STUDIO_IMAGES_AUTO_ORDER", f"z-image-turbo,{name},mock")
    with pytest.raises(RemovedEngineError, match=REMOVED_MSG):
        VisualsConfig.from_env()
    with pytest.raises(StageError, match=REMOVED_MSG):  # never silently skipped by `auto`
        images.run(make_job(tmp_path, n=1), brand(), ctx(tmp_path, "auto"))


# ---------------------------------------------------------------- mixed render


def ff(*args, cwd=None):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True,
                   cwd=cwd)


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_render_mixes_stills_and_muted_clips(tmp_path):
    from studio.stages import captions

    ff("-f", "lavfi", "-i", "sine=frequency=440:duration=5", "-ar", "48000", str(tmp_path / "a.wav"))
    imgs = tmp_path / "images"
    imgs.mkdir()
    ff("-f", "lavfi", "-i", "color=c=red:s=720x1280:d=1", "-frames:v", "1", "-update", "1",
       str(imgs / "beat_00.png"))
    # 1.5 s landscape clip WITH a loud audio track: must be cropped to 9:16, looped and muted
    ff("-f", "lavfi", "-i", "testsrc=size=640x360:rate=25:duration=1.5", "-f", "lavfi",
       "-i", "sine=frequency=3000:duration=1.5", "-shortest", "-c:v", "libx264",
       "-pix_fmt", "yuv420p", "-c:a", "aac", str(imgs / "beat_01.mp4"))
    timings = {"segments": [{"beat": 0, "start": 0.0, "end": 1.0},
                            {"beat": 1, "start": 1.0, "end": 5.0}]}
    (tmp_path / "timings.json").write_text(json.dumps(timings))
    words = captions.mock_words({"segments": [{"text": "one two three", "start": 0, "end": 5}]},
                                5.0)
    captions.write_ass(words, brand(), "short", tmp_path / "captions.ass")
    job = make_job(tmp_path, n=2)
    job.audio_path = tmp_path / "a.wav"
    job.captions_path = tmp_path / "captions.ass"
    job.image_paths = [imgs / "beat_00.png", imgs / "beat_01.mp4"]
    job = render.run(job, brand(), ctx(tmp_path, "mock"))

    plan = json.loads((tmp_path / "render_plan.json").read_text())
    clip_shots = [s for s in plan if s.get("kind") == "clip"]
    still_shots = [s for s in plan if s.get("kind") != "clip"]
    assert still_shots and clip_shots and all(s["image"] == 1 for s in clip_shots)
    assert len(clip_shots) >= 2 and clip_shots[0]["offset"] == 0.0  # 4 s split into <=3.5 s
    probe = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format",
         str(job.video_path)], capture_output=True, text=True, check=True).stdout)
    v = [s for s in probe["streams"] if s["codec_type"] == "video"]
    a = [s for s in probe["streams"] if s["codec_type"] == "audio"]
    assert len(v) == 1 and len(a) == 1
    assert (v[0]["width"], v[0]["height"]) == (1080, 1920)
    assert abs(float(probe["format"]["duration"]) - 5.0) < 0.2
    assert job.content_hash and (tmp_path / "content_hash.txt").read_text().strip() == \
        job.content_hash


def test_mark_clips_offsets_continue_and_wrap():
    shots = [{"image": 0, "frames": 30}, {"image": 1, "frames": 60}, {"image": 1, "frames": 60},
             {"image": 1, "frames": 60}]
    imgs = [Path("a.png"), Path("b.mp4")]
    render.mark_clips(shots, imgs, {1: 5.0})
    assert "kind" not in shots[0]
    assert [s["offset"] for s in shots[1:]] == [0.0, 2.0, 4.0 % 5.0]
