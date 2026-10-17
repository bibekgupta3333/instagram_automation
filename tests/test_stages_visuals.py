"""Images stage engines (Bedrock Stability, auto fallback with Pixabay stock, Luma gate) and
mixed stills + clips render. Offline: boto3 and the stock fetch are faked. The Pixabay engine
itself is tested in tests/test_pixabay.py."""

import base64
import io
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from studio.config import DEFAULT_BRAND_DIR, Settings, VisualsConfig, load_brand
from studio.contracts import Beat, Script, VideoJob
from studio.pipeline.mock_llm import MockChatClient
from studio.stages import images, render
from studio.stages.base import EngineUnavailable, StageContext, StageError
from studio.stages.visuals import BeatSpec, Visual, luma, stability

# 1x1 PNG
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path_factory):
    for k in list(os.environ):
        if k.startswith(("STUDIO_IMAGES", "STUDIO_PIXABAY", "STUDIO_STABILITY", "STUDIO_LUMA",
                         "STUDIO_ALLOW_PAID", "STUDIO_VIDEO_MAX", "STUDIO_BEDROCK_IMAGE",
                         "STUDIO_MFLUX_CMD", "STUDIO_ALLOW_MODEL", "STUDIO_REALESRGAN")):
            monkeypatch.delenv(k, raising=False)
    # empty HF cache: `auto` tries local Z-Image first, and must never run real mflux in tests
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path_factory.mktemp("hf-hub")))
    for k in ("PIXABAY_API_KEY", "AWS_BEARER_TOKEN_BEDROCK", "AWS_PROFILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("STUDIO_SKIP_LEDGER", "1")


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


# ---------------------------------------------------------------- Bedrock Stability


class FakeRuntime:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def invoke_model(self, **kw):
        self.calls.append(kw)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return {"body": io.BytesIO(json.dumps(r).encode())}


class FakeClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code, "Message": code}}


def ok_reply():
    return {"seeds": [1], "finish_reasons": [None], "images": [base64.b64encode(PNG).decode()]}


@pytest.mark.parametrize(("fmt", "ratio"), [("short", "9:16"), ("long", "16:9")])
def test_stability_body_aspect_seed_style(tmp_path, fmt, ratio):
    s = spec(tmp_path, fmt)
    body = stability.build_body(s, negative_prompt="text, watermark")
    assert body["aspect_ratio"] == ratio
    assert body["output_format"] == "png"
    assert body["negative_prompt"] == "text, watermark"
    assert body["prompt"].startswith("a red pickup truck") and brand().style_prompt in body["prompt"]
    assert 1 <= body["seed"] <= 4294967294
    assert body["seed"] == spec(tmp_path, fmt).seed  # reproducible per job + beat
    assert body["seed"] != spec(tmp_path, fmt, index=1).seed


def test_stability_decodes_png_and_retries_throttling(tmp_path):
    rt = FakeRuntime([FakeClientError("ThrottlingException"), ok_reply()])
    sleeps = []
    vis = stability.generate(spec(tmp_path), client=rt, sleep=sleeps.append)
    assert vis.path == tmp_path / "beat_00.png" and vis.path.read_bytes() == PNG
    assert vis.kind == "image" and vis.usd == pytest.approx(0.04)
    assert rt.calls[0]["modelId"] == "stability.stable-image-core-v1:1"
    assert json.loads(rt.calls[0]["body"])["aspect_ratio"] == "9:16"
    assert len(sleeps) == 1 and sleeps[0] > 0


def test_stability_filtered_and_access_denied(tmp_path):
    rt = FakeRuntime([{"finish_reasons": ["Filter reason: prompt"]}])
    with pytest.raises(StageError, match="Filter reason"):
        stability.generate(spec(tmp_path), client=rt, sleep=lambda _: None)
    rt = FakeRuntime([FakeClientError("AccessDeniedException")])
    with pytest.raises(EngineUnavailable):
        stability.generate(spec(tmp_path), client=rt, sleep=lambda _: None)


def test_stability_refuses_default_aws_chain(tmp_path):
    with pytest.raises(EngineUnavailable, match="AWS_BEARER_TOKEN_BEDROCK"):
        stability.runtime_client("us-west-2")


def test_stability_client_uses_api_key_in_us_west_2(monkeypatch):
    pytest.importorskip("boto3")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "fake-key")
    client = stability.runtime_client("us-west-2")  # builds a client; makes no request
    assert client.meta.region_name == "us-west-2"
    assert client.meta.service_model.service_name == "bedrock-runtime"


def test_stability_model_aliases():
    assert stability.resolve_model("ultra") == ("stability.stable-image-ultra-v1:1", 0.14)
    assert stability.resolve_model("sd3.5-large")[0] == "stability.sd3-5-large-v1:0"


# ---------------------------------------------------------------- auto order + stage


def fake_visual(engine, kind="image", usd=0.0):
    def _fn(s, *a, **kw):
        out = s.out_stem.with_suffix(".mp4" if kind == "video" else ".png")
        out.write_bytes(PNG)
        src = ({"id": s.index, "credit": f"Video by X on Pixabay (id {s.index}): u{s.index}"}
               if engine == "pixabay" else {})
        return Visual(path=out, kind=kind, engine=engine, model=engine, usd=usd, source=src)
    return _fn


def test_auto_order_pixabay_then_stability_then_mock(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setenv("PIXABAY_API_KEY", "k")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "fake")

    def pex(s, **kw):
        calls.append(("pixabay", s.index))
        if s.index == 1:
            raise StageError("pixabay: nothing found")
        return fake_visual("pixabay", "video")(s)

    def stab(s, **kw):
        calls.append(("stability", s.index))
        return fake_visual("bedrock-stability", usd=0.04)(s)

    recorded = []
    monkeypatch.setattr(images.pixabay_src, "fetch", pex)
    monkeypatch.setattr(images.stability_src, "generate", stab)
    monkeypatch.setattr(images, "record_cost", lambda **kw: recorded.append(kw))
    job = make_job(tmp_path, n=2)
    job = images.run(job, brand(), ctx(tmp_path, "auto"))
    assert calls == [("pixabay", 0), ("pixabay", 1), ("stability", 1)]
    assert [p.suffix for p in job.image_paths] == [".mp4", ".png"]
    assert recorded == [{"channel": "car-cost-truth", "job_id": "job-x", "step": "images",
                         "model": "stability.stable-image-core-v1:1", "usd": 0.04}]
    manifest = json.loads((tmp_path / "images" / "visuals.json").read_text())
    assert [b["visual"]["engine"] for b in manifest["beats"]] == ["pixabay", "bedrock-stability"]
    assert "on Pixabay" in (tmp_path / "images" / "attribution.txt").read_text()

    # resume: nothing is re-fetched or re-paid
    monkeypatch.setattr(images.stability_src, "generate", lambda *a, **k: pytest.fail("re-paid"))
    monkeypatch.setattr(images.pixabay_src, "fetch", lambda *a, **k: pytest.fail("re-fetched"))
    job = images.run(job, brand(), ctx(tmp_path, "auto"))
    assert job.log[-1].endswith("images: cached, skipped")


def test_auto_skips_stock_for_unsuitable_brand_and_respects_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("PIXABAY_API_KEY", "k")
    monkeypatch.setenv("STUDIO_IMAGES_MAX_USD_PER_VIDEO", "0.05")
    monkeypatch.setattr(images.pixabay_src, "fetch", lambda *a, **k: pytest.fail("stock used"))
    monkeypatch.setattr(images.stability_src, "generate", fake_visual("bedrock-stability",
                                                                      usd=0.04))
    monkeypatch.setattr(images, "record_cost", lambda **kw: None)
    monkeypatch.setattr(images, "_mock_image", lambda out, *a: out.write_bytes(PNG))
    job = make_job(tmp_path, n=2)
    job.channel = "furnace-to-faucet"
    with pytest.warns(UserWarning, match="MOCK PLACEHOLDER"):
        job = images.run(job, brand("furnace-to-faucet"), ctx(tmp_path, "auto"))
    engines = [b["visual"]["engine"] for b in
               json.loads((tmp_path / "images" / "visuals.json").read_text())["beats"]]
    assert engines == ["bedrock-stability", "mock"]  # 2nd still would exceed the $0.05 cap


def test_auto_without_keys_is_loud_mock(tmp_path, monkeypatch):
    monkeypatch.setattr(images, "_mock_image", lambda out, *a: out.write_bytes(PNG))
    job = make_job(tmp_path, n=1)
    with pytest.warns(UserWarning, match="MOCK PLACEHOLDER"):
        job = images.run(job, brand(), ctx(tmp_path, "auto"))
    assert any("WARNING" in line and "AWS_BEARER_TOKEN_BEDROCK" in line for line in job.log)
    # a later run with credentials must retry instead of reusing the placeholder
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "fake")
    monkeypatch.setattr(images.stability_src, "generate", fake_visual("bedrock-stability",
                                                                      usd=0.04))
    monkeypatch.setattr(images, "record_cost", lambda **kw: None)
    job = images.run(job, brand(), ctx(tmp_path, "auto"))
    assert "bedrock-stability" in job.log[-1]


def test_explicit_engine_does_not_fall_back(tmp_path):
    with pytest.raises(EngineUnavailable):
        images.run(make_job(tmp_path, n=1), brand(), ctx(tmp_path, "bedrock-stability"))
    with pytest.raises(StageError, match="unknown engine"):
        images.run(make_job(tmp_path, n=1), brand(), ctx(tmp_path, "dalle"))


def test_default_engine_is_auto(monkeypatch):
    assert Settings.from_env().engines["images"] == "auto"
    assert VisualsConfig.from_env().auto_order == ("z-image-turbo", "pixabay",
                                                   "bedrock-stability", "mock")
    monkeypatch.setenv("STUDIO_IMAGES_ENGINE", "z-image-turbo")
    assert Settings.from_env().engines["images"] == "z-image-turbo"
    monkeypatch.setenv("STUDIO_IMAGES_AUTO_ORDER", "bedrock-stability, mock")
    assert VisualsConfig.from_env().auto_order == ("bedrock-stability", "mock")


# ---------------------------------------------------------------- Luma paid-video gate


def test_luma_gate_blocks_by_default_and_caps(tmp_path, monkeypatch):
    s = spec(tmp_path)
    with pytest.raises(luma.PaidVideoBlocked, match="STUDIO_ALLOW_PAID_VIDEO"):
        luma.generate(s, VisualsConfig.from_env())
    monkeypatch.setenv("STUDIO_ALLOW_PAID_VIDEO", "1")
    monkeypatch.setenv("STUDIO_VIDEO_MAX_USD_PER_VIDEO", "5")
    with pytest.raises(luma.PaidVideoBlocked, match="cap"):  # 5 s x $1.50 = $7.50
        luma.generate(s, VisualsConfig.from_env())
    assert luma.check_gate(VisualsConfig.from_env(), "5s", "540p") == pytest.approx(3.75)
    with pytest.raises(NotImplementedError, match="STUDIO_LUMA_S3_URI"):
        luma.generate(s, VisualsConfig.from_env(), resolution="540p")
    req = luma.build_request(s, "s3://b/p/")
    assert req["modelId"] == "luma.ray-v2:0"
    assert req["modelInput"]["aspect_ratio"] == "9:16" and req["modelInput"]["duration"] == "5s"
    assert req["outputDataConfig"]["s3OutputDataConfig"]["s3Uri"] == "s3://b/p/"


def test_luma_engine_falls_back_to_auto(tmp_path, monkeypatch):
    monkeypatch.setattr(images, "_mock_image", lambda out, *a: out.write_bytes(PNG))
    job = make_job(tmp_path, n=1)
    with pytest.warns(UserWarning):
        job = images.run(job, brand(), ctx(tmp_path, "bedrock-luma"))
    assert any("bedrock-luma hero skipped" in line for line in job.log)


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
