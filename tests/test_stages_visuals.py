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
    monkeypatch.setattr(render, "ENCODERS", [render.ENCODERS[-1]])
    for k in list(os.environ):
        if k.startswith(("STUDIO_IMAGES", "STUDIO_PIXABAY", "STUDIO_MFLUX_CMD",
                         "STUDIO_ALLOW_MODEL", "STUDIO_REALESRGAN", "STUDIO_STOCK_FIRST",
                         "STUDIO_ZIMAGE")):
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
    def _mflux(raw, prompt, seed, engine, fmt, cfg=None):
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


@pytest.mark.parametrize("change", ["job_id", "shot", "stock_channels"])
def test_image_cache_tracks_seed_shot_and_stock_eligibility(tmp_path, monkeypatch, change):
    calls = []
    fake_zimage(monkeypatch, calls)
    job = make_job(tmp_path, n=1)
    job.script.beats[0].shot = "medium"
    images.run(job, brand(), ctx(tmp_path, "z-image-turbo"))
    if change == "job_id":
        job.job_id = "different-seed"
    elif change == "shot":
        job.script.beats[0].shot = "medium shot"  # same prompt framing, distinct metadata
    else:
        monkeypatch.setenv("STUDIO_PIXABAY_CHANNELS", "all")
    images.run(job, brand(), ctx(tmp_path, "z-image-turbo"))
    assert len(calls) == 2
    entry = json.loads((tmp_path / "images" / "visuals.json").read_text())["beats"][0]
    assert entry["shot"] == job.script.beats[0].shot


def test_invalid_visual_config_raises_stage_error(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_ZIMAGE_STEPS", "invalid")
    with pytest.raises(StageError, match="STUDIO_ZIMAGE_STEPS"):
        images.run(make_job(tmp_path, n=1), brand(), ctx(tmp_path, "auto"))


def test_resume_reserves_stock_ids_from_later_cached_beats(tmp_path, monkeypatch):
    monkeypatch.setenv("PIXABAY_API_KEY", "test-placeholder")
    calls = []

    def fetch(spec, *, used, **kwargs):
        candidates = [100, 200] if len(calls) < 2 else [200, 300]
        picked = next(i for i in candidates if f"video:{i}" not in used)
        calls.append(picked)
        out = spec.out_stem.with_suffix(".mp4")
        out.write_bytes(PNG)
        return Visual(path=out, kind="video", engine="pixabay", model="stock",
                      source={"id": picked})

    monkeypatch.setattr(images.pixabay_src, "fetch", fetch)
    job = make_job(tmp_path, n=2)
    images.run(job, brand(), ctx(tmp_path, "pixabay"))
    job.image_paths[0].unlink()
    images.run(job, brand(), ctx(tmp_path, "pixabay"))
    assert calls == [100, 200, 300]
    entries = json.loads((tmp_path / "images" / "visuals.json").read_text())["beats"]
    assert [entry["visual"]["source"]["id"] for entry in entries] == [300, 200]


def test_stage_anchors_script_query_and_invalidates_pre_relevance_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("PIXABAY_API_KEY", "test-placeholder")
    queries = []

    def fetch(spec, **kwargs):
        queries.append(spec.stock_query)
        return fake_visual("pixabay", "video")(spec)

    monkeypatch.setattr(images.pixabay_src, "fetch", fetch)
    job = make_job(tmp_path, n=1)
    job.script.beats[0].stock_query = "fuel injector"
    current_version = images.VERSION
    assert current_version >= 10
    monkeypatch.setattr(images, "VERSION", 9)
    images.run(job, brand(), ctx(tmp_path, "pixabay"))
    monkeypatch.setattr(images, "VERSION", current_version)
    images.run(job, brand(), ctx(tmp_path, "pixabay"))
    assert queries == ["car fuel injector", "car fuel injector"]
    assert job.script.beats[0].stock_query == "fuel injector"  # producer artifact is untouched


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


# ---------------------------------------------------------------- prompt builder + seeds


def test_prompt_builder_shot_style_palette_and_no_text():
    from studio.stages.visuals.prompts import NO_TEXT, SHOT_FRAMING, build_prompt

    b = brand("wing-and-turbine")
    close = build_prompt("a turbine blade glowing hot.", b, "close-up detail")
    wide = build_prompt("an airliner climbing at dusk", b, "wide establishing")
    assert close.startswith("Tall vertical portrait composition. Close-up detail shot on an 85mm")
    assert "A turbine blade glowing hot." in close  # sentence-cased, single full stop
    assert SHOT_FRAMING["wide"].split(",")[0] in wide
    assert close.endswith(NO_TEXT) and "no logos" in NO_TEXT and "no watermark" in NO_TEXT
    assert "#" not in close  # hex codes would be printed into the image
    assert "Colour palette of dark slate blue, pale silver and vivid azure blue." in close
    style = close.split("glowing hot. ")[1].split(" Colour palette")[0]
    assert style in wide  # one consistent brand style per video
    long = build_prompt("x", b, "", fmt="long")
    assert long.startswith("Wide horizontal landscape composition. Cinematic medium-wide shot")


def test_prompt_builder_strips_text_inviting_style_words():
    from studio.stages.visuals.prompts import build_prompt, clean_style

    sq = clean_style(brand("square-foot-money").style_prompt)
    assert "labels" not in sq and "#" not in sq and "crisp shapes" in sq
    car = build_prompt("a used sedan on a dealer lot", brand("car-cost-truth"), "medium shot")
    assert "overlays" not in car and "Photorealistic" in car  # photo look -> photo boosters
    mb = build_prompt("a dam", brand("megabuild-blueprint"), "")
    assert "dimension arrows" not in mb and "physically based materials" in mb


@pytest.mark.parametrize(("shot", "cls"), [
    ("wide establishing", "wide"), ("Close-up detail", "close"), ("macro insert", "close"),
    ("aerial drone", "aerial"), ("cutaway", "cutaway"), ("cross-section", "cutaway"),
    ("animated diagram", "diagram"), ("top-down flat lay", "top"), ("", ""), ("weird", ""),
])
def test_shot_class(shot, cls):
    from studio.stages.visuals.prompts import shot_class

    assert shot_class(shot) == cls


def test_per_video_base_seed_plus_beat_offset():
    from studio.stages.visuals.common import base_seed, beat_seed

    base = base_seed("job-x")
    assert [beat_seed("job-x", i) for i in range(3)] == [base, base + 1, base + 2]
    assert base_seed("job-y") != base and 0 < base < 2**31
    assert spec(Path("/tmp"), index=2).seed == base + 2


def test_zimage_steps_and_size_from_env(monkeypatch):
    assert (VisualsConfig.from_env().zimage_steps, images.gen_size("long")) == (
        9, images.gen_size("short")[::-1])
    monkeypatch.setenv("STUDIO_ZIMAGE_STEPS", "12")
    monkeypatch.setenv("STUDIO_ZIMAGE_SIZE", "640x1152")
    cfg = VisualsConfig.from_env()
    assert cfg.zimage_steps == 12 and images.gen_size("long", cfg.zimage_size) == (1152, 640)
    monkeypatch.setenv("STUDIO_ZIMAGE_SIZE", "650x1152")
    with pytest.raises(ValueError, match="multiples of 16"):
        VisualsConfig.from_env()


# ---------------------------------------------------------------- per-brand visual order


def test_visual_order_stock_first_channels(monkeypatch):
    from studio.config import DEFAULT_AUTO_ORDER, STOCK_FIRST_ORDER

    cfg = VisualsConfig.from_env()
    for slug in ("car-cost-truth", "square-foot-money", "business-model-files"):
        assert cfg.visual_order(slug, stock_beat=True) == STOCK_FIRST_ORDER
        assert cfg.visual_order(slug, stock_beat=False) == DEFAULT_AUTO_ORDER
    assert cfg.visual_order("wing-and-turbine", stock_beat=True) == DEFAULT_AUTO_ORDER
    monkeypatch.setenv("STUDIO_STOCK_FIRST_CHANNELS", "none")
    assert VisualsConfig.from_env().visual_order("car-cost-truth") == DEFAULT_AUTO_ORDER
    monkeypatch.delenv("STUDIO_STOCK_FIRST_CHANNELS")
    monkeypatch.setenv("STUDIO_IMAGES_AUTO_ORDER", "z-image-turbo,mock")  # explicit order wins
    assert VisualsConfig.from_env().visual_order("car-cost-truth") == ("z-image-turbo", "mock")


def test_stock_beat_needs_query_and_a_non_diagram_shot(tmp_path):
    def s(shot="", q="used car lot", visual="cars on a dealer lot"):
        return BeatSpec(job_id="j", channel="car-cost-truth", index=0, fmt="short",
                        visual_prompt=visual, prompt="p", out_stem=tmp_path / "b",
                        shot=shot, stock_query=q)
    assert images.stock_beat(s("wide establishing"))
    assert not images.stock_beat(s(q=""))
    assert not images.stock_beat(s("diagram"))
    assert not images.stock_beat(s("cutaway"))
    assert not images.stock_beat(s(visual="a bar chart of depreciation by year"))


def test_stock_first_beats_try_pixabay_before_zimage(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setenv("PIXABAY_API_KEY", "k")
    fake_zimage(monkeypatch, calls)
    seen = []

    def pix(s, **kw):
        calls.append(("pixabay", s.index))
        seen.append(s.stock_query)
        return fake_visual("pixabay", "video")(s)

    monkeypatch.setattr(images.pixabay_src, "fetch", pix)
    beats = [
        Beat(text="a.", visual_prompt="cars on a dealer lot", shot="wide establishing",
             stock_query="car dealership lot"),
        Beat(text="b.", visual_prompt="depreciation curve", shot="diagram",
             stock_query="car value"),
        Beat(text="c.", visual_prompt="a sedan on a highway", shot="medium"),  # no query
    ]
    job = VideoJob(job_id="job-s", channel="square-foot-money", format="short", topic="t",
                   script=Script(title="T", hook="H", beats=beats, description="d"),
                   workdir=tmp_path)
    job = images.run(job, brand("square-foot-money"), ctx(tmp_path, "auto"))
    # beat 0: stock first (the brand's flat-vector style doesn't block it); beats 1-2: Z-Image
    assert calls == [("pixabay", 0), ("z-image-turbo", 1), ("z-image-turbo", 2)]
    assert seen == ["car dealership lot"]
    manifest = json.loads((tmp_path / "images" / "visuals.json").read_text())
    assert [b["shot"] for b in manifest["beats"]] == ["wide establishing", "diagram", "medium"]
