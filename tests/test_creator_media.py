"""Offline media contract tests: mock provider, tiny real CPU FFmpeg fixtures only."""
import copy
import io
import json
import os
import shutil
import signal
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from PIL import Image, ImageChops

from studio.web import creator_media as media
from studio.web.creator_models import Creation


@pytest.fixture
def creation():
    return Creation.model_validate({
        "brief": {"idea": "A fox explores the moon", "duration": 5, "audio": "silent",
                  "style": "watercolor-anime"},
        "plan": {"title": "Moon fox", "hook": "A little discovery",
                 "characters": [{"name": "Ember", "description": "Copper fox with a blue scarf"}],
                 "shots": [{"number": 1, "prompt": "Ember leaps over a moon crater",
                            "caption": "One giant leap", "narration": "Ember explores the moon."}]}
    }).model_dump(mode="json")


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    for name in ("STUDIO_CREATOR_CLOUD_ENABLED", "STUDIO_CREATOR_CLOUD_BUDGET_USD",
                 "REPLICATE_API_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(media, "_disk", lambda *args: None)
    monkeypatch.setattr(media.socket, "getaddrinfo", Mock(side_effect=AssertionError("Live DNS")))
    original = httpx.Client

    def no_network(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(
            lambda request: (_ for _ in ()).throw(AssertionError("Live HTTP")))
        return original(*args, **kwargs)

    monkeypatch.setattr(media.httpx, "Client", no_network)


def enable(monkeypatch):
    monkeypatch.setenv("STUDIO_CREATOR_CLOUD_ENABLED", "1")
    monkeypatch.setenv("STUDIO_CREATOR_CLOUD_BUDGET_USD", "5")
    monkeypatch.setenv("REPLICATE_API_TOKEN", "test-placeholder-token")


def http_provider(monkeypatch, handler):
    # The fixture replaces the entire client: credentials are synthetic, never real env files.
    client = httpx.Client(transport=httpx.MockTransport(handler))
    client._transport = httpx.MockTransport(handler)
    monkeypatch.setattr(media.httpx, "Client", lambda **kwargs: client)
    return client


def test_status_billing_defaults_and_no_io(monkeypatch):
    monkeypatch.setattr(media, "_tts_engine", lambda: None)
    status = media.cloud_status()
    assert status["enabled"] is False and status["available"] is False
    assert status["budget_usd"] == status["max_cost_usd"] == 0
    assert status["cost_per_second_usd"] * 5 == status["cost_per_shot_usd"] == .25
    assert "guarantee" in status["cost_note"]
    assert status["narration_available"] is False
    assert "token" not in json.dumps(status)


@pytest.mark.parametrize("budget", ["nan", "inf", "-1", "bad", "0"])
def test_bad_admin_budget_fail_closed(monkeypatch, budget):
    enable(monkeypatch)
    monkeypatch.setenv("STUDIO_CREATOR_CLOUD_BUDGET_USD", budget)
    assert not media.cloud_status()["available"]


@pytest.mark.parametrize("cap", [0, .24, 5.01])
def test_budget_guard_precedes_every_provider_call(creation, monkeypatch, tmp_path, cap):
    enable(monkeypatch)
    provider = Mock(side_effect=AssertionError("Must not dispatch"))
    monkeypatch.setattr(media, "_Provider", provider)
    with pytest.raises(media.MediaError, match="ceiling"):
        media.render_creation(creation, {"mode": "cloud", "max_cost_usd": cap}, tmp_path)
    provider.assert_not_called()


def test_request_cannot_enable_cloud(creation, tmp_path):
    with pytest.raises(media.MediaError, match="disabled"):
        media.render_creation(creation, {"mode": "cloud", "max_cost_usd": 2}, tmp_path)


def test_plan_duration_rejected_before_provider(creation, tmp_path):
    creation["brief"]["duration"] = 10
    with pytest.raises(media.MediaError, match="matching"):
        media.render_creation(creation, {"mode": "preview"}, tmp_path)


def test_cast_identity_and_no_ai_text_even_for_edited_prompt(creation):
    shot = creation["plan"]["shots"][0]
    shot["prompt"] = "A silver landscape moves slowly"
    prompt = media._prompt(creation, shot)
    assert "Copper fox with a blue scarf" in prompt
    assert "no text" in prompt and "watercolor" in prompt
    creation["brief"]["characters"] = creation["plan"].pop("characters")
    assert "Copper fox" in media._prompt(creation, shot)


def test_ambiguous_post_is_durable_and_never_retried(monkeypatch, tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        assert json.loads((tmp_path / "provider.json").read_text())["shots"]["0"]["state"] == "submitting"
        raise httpx.ReadTimeout("synthetic secret should not enter error")

    http_provider(monkeypatch, handler)
    provider = media._Provider(tmp_path, "cache")
    with pytest.raises(media.MediaError, match="reconcile") as exc:
        provider.clip(0, "A moving fox", [10000])
    assert "synthetic secret" not in str(exc.value)
    assert len(calls) == 1
    provider2 = media._Provider(tmp_path, "cache")
    with pytest.raises(media.MediaError, match="unknown"):
        provider2.clip(0, "A moving fox", [10000])
    assert len(calls) == 1
    assert "secret" not in (tmp_path / "provider.json").read_text()


def test_read_retry_resumes_id_without_billable_retry(monkeypatch, tmp_path):
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            return httpx.Response(201, json={"id": "abc123", "logs": "never persist"})
        if len(calls) == 2:
            return httpx.Response(503)
        return httpx.Response(200, json={"status": "succeeded", "output": "https://replicate.delivery/a.mp4"})

    http_provider(monkeypatch, handler)
    monkeypatch.setattr(media.time, "sleep", lambda _: None)
    monkeypatch.setattr(media, "_download", lambda url, path, budget: path.write_bytes(b"fixture"))
    monkeypatch.setattr(media, "_probe", lambda *args: {
        "streams": [{"codec_type": "video", "width": 480, "height": 832}],
        "format": {"duration": "5.0625"}})
    provider = media._Provider(tmp_path, "cache")
    first = provider.clip(0, "A moving fox", [10000])
    assert first.read_bytes() == b"fixture"
    assert [method for method, path in calls].count("POST") == 1
    assert provider.clip(0, "A moving fox", [10000]) == first
    assert len(calls) == 3  # cache resumes without even polling
    journal = (tmp_path / "provider.json").read_text()
    assert "abc123" in journal and "https://" not in journal and "never persist" not in journal


def test_failed_prediction_never_resubmits(monkeypatch, tmp_path):
    methods = []

    def handler(request):
        methods.append(request.method)
        return httpx.Response(200, json={"id": "abc"} if request.method == "POST"
                              else {"status": "failed", "error": "provider sensitive detail"})

    http_provider(monkeypatch, handler)
    provider = media._Provider(tmp_path, "cache")
    for _ in range(2):
        with pytest.raises(media.MediaError, match="no automatic"):
            provider.clip(0, "Fox moves", [10000])
    assert methods.count("POST") == 1
    assert "sensitive" not in (tmp_path / "provider.json").read_text()


def test_worker_term_cancels_known_prediction_and_restores_handler(monkeypatch, tmp_path):
    calls = []
    previous = signal.getsignal(signal.SIGTERM)

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/cancel"):
            return httpx.Response(200, json={"status": "canceled"})
        if request.method == "POST":
            return httpx.Response(201, json={"id": "abc"})
        os.kill(os.getpid(), signal.SIGTERM)
        raise AssertionError("Signal handler must interrupt this GET")

    http_provider(monkeypatch, handler)
    provider = media._Provider(tmp_path, "cache")
    with pytest.raises(media._Stopped), media._termination_cleanup():
        provider.clip(0, "Fox moves", [10000])
    assert calls[-1] == "/v1/predictions/abc/cancel"
    assert signal.getsignal(signal.SIGTERM) == previous
    assert "cancel_requested" in (tmp_path / "provider.json").read_text()


@pytest.mark.parametrize("url", [
    "http://replicate.delivery/a", "https://replicate.delivery.evil.test/a",
    "https://evil.test/a", "https://replicate.delivery@evil.test/a",
    "https://user:pass@replicate.delivery/a", "https://replicate.delivery:444/a",
    "https://127.0.0.1/a", "https://replicate.delivery/a#fragment",
])
def test_delivery_url_allowlist(url, tmp_path):
    with pytest.raises(media.MediaError, match="Untrusted"):
        media._download(url, tmp_path / "source.mp4", [10000])


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fe80::1"])
def test_delivery_dns_rejects_private_addresses(monkeypatch, tmp_path, address):
    monkeypatch.setattr(media.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", (address, 443))])
    with pytest.raises(media.MediaError, match="non-public"):
        media._download("https://replicate.delivery/video", tmp_path / "source.mp4", [10000])


def fake_delivery(monkeypatch, body, *, status=200, length=None):
    monkeypatch.setattr(media.socket, "getaddrinfo", lambda *a, **k: [
        (2, 1, 6, "", ("93.184.216.34", 443))])
    instances = []

    class Connection:
        def __init__(self, host, address):
            self.host, self.address = host, address
            self.body = io.BytesIO(body)
            self.closed = False
            instances.append(self)

        def request(self, method, path, headers):
            if os.environ.get("REPLICATE_API_TOKEN"):
                assert headers["Authorization"] == "Bearer test-placeholder-token"
            else:
                assert "Authorization" not in headers
            self.request_path = path

        def getresponse(self):
            return SimpleNamespace(status=status, getheader=lambda _: length, read=self.body.read)

        def close(self):
            self.closed = True

    monkeypatch.setattr(media, "_PinnedHTTPS", Connection)
    return instances


@pytest.mark.parametrize("status,length,budget", [(302, None, 100), (200, "1000", 100),
                                                   (200, None, 2), (200, "20", 100)])
def test_stream_download_bounds_redirects_and_incomplete(monkeypatch, tmp_path, status, length, budget):
    instances = fake_delivery(monkeypatch, b"video", status=status, length=length)
    with pytest.raises(media.MediaError):
        media._download("https://tjzk.replicate.delivery/a.mp4", tmp_path / "source.mp4", [budget])
    assert not (tmp_path / "source.mp4").exists()
    assert not list(tmp_path.glob("*.part"))
    assert instances[0].closed


def test_stream_download_pins_resolved_ip_and_preserves_byte_budget(monkeypatch, tmp_path):
    instances = fake_delivery(monkeypatch, b"video", length="5")
    budget = [10]
    media._download("https://tjzk.replicate.delivery/a.mp4?token=fake", tmp_path / "source.mp4", budget)
    assert (tmp_path / "source.mp4").read_bytes() == b"video"
    assert budget == [5] and instances[0].address == "93.184.216.34"
    assert instances[0].host == "tjzk.replicate.delivery"


def test_disk_guard_reserves_incoming_bytes(monkeypatch, tmp_path):
    # Restore implementation replaced by the autouse fixture without reading any secrets.
    monkeypatch.setattr(media.shutil, "disk_usage", lambda _: SimpleNamespace(free=10_000_000_005))
    # Inspect the saved function below via the module-level reference established at collection.
    with pytest.raises(media.MediaError, match="10 GB"):
        REAL_DISK(tmp_path, 6)
    REAL_DISK(tmp_path, 5)


REAL_DISK = media._disk


def test_missing_narration_engine_is_clear_and_before_dispatch(creation, tmp_path, monkeypatch):
    creation["brief"]["audio"] = "narration"
    monkeypatch.setattr(media, "_tts_engine", lambda: None)
    with pytest.raises(media.MediaError, match="System narration unavailable"):
        media.render_creation(creation, {"mode": "preview"}, tmp_path)


@pytest.mark.parametrize("engine", [("/usr/bin/say", "Samantha"), ("/usr/bin/espeak-ng", "en")])
def test_narration_uses_stdin_fixed_voice_and_safe_tempo(engine, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(media, "_run", lambda args, out, **kw: calls.append((args, kw)))
    monkeypatch.setattr(media, "_probe", lambda *a: {"format": {"duration": "7.2"}})
    monkeypatch.setattr(media, "_ffmpeg", lambda out, args: calls.append((args, {})))
    text = "Hello $(touch /tmp/not-executed) `not shell`."
    media._narration({"narration": text}, 0, tmp_path, engine)
    assert calls[0][1]["stdin"] == text
    assert text not in calls[0][0]
    assert "atempo=1.500000" in " ".join(calls[1][0])
    assert "atrim=duration=5" in " ".join(calls[1][0])
    assert calls[0][1]["timeout"] == 30


def test_narration_rejects_excessive_length(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "_run", lambda *a, **k: "")
    monkeypatch.setattr(media, "_probe", lambda *a: {"format": {"duration": "9"}})
    with pytest.raises(media.MediaError, match="speed-up"):
        media._narration({"caption": "A short caption"}, 0, tmp_path, ("espeak-ng", "en"))
    with pytest.raises(media.MediaError, match="1–25"):
        media._narration({"narration": "words " * 26}, 0, tmp_path, ("espeak-ng", "en"))


def test_subprocess_inherits_group_and_bounds_cpu_and_timeout(monkeypatch, tmp_path):
    proc = Mock()
    proc.communicate.return_value = (b"probe", None)
    proc.returncode = 0
    proc.poll.return_value = 0
    popen = Mock(return_value=proc)
    monkeypatch.setattr(media.subprocess, "Popen", popen)
    assert media._run(["ffprobe", "fixture.mp4"], tmp_path, capture=True) == "probe"
    _, kw = popen.call_args
    assert "start_new_session" not in kw and "preexec_fn" not in kw and not kw.get("shell")
    assert kw["env"]["OMP_NUM_THREADS"] == "3"
    assert proc.communicate.call_args.kwargs["timeout"] == 120
    proc.communicate.side_effect = subprocess.TimeoutExpired("fake", 120)
    with pytest.raises(media.MediaError, match="runtime"):
        media._run(["ffmpeg"], tmp_path)


HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


@pytest.fixture
def tiny_video(tmp_path):
    if not HAS_FFMPEG:
        pytest.skip("CPU FFmpeg fixture requires ffmpeg and ffprobe")
    path = tmp_path / "fixture.mp4"
    subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-y", "-threads", "3",
                    "-filter_threads", "3", "-f", "lavfi", "-i", "color=c=blue:s=480x832:r=16",
                    "-t", "5.0625", "-an", "-c:v", "libx264", "-threads", "3",
                    "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path)],
                   check=True, capture_output=True, timeout=30)
    return path.read_bytes()


@pytest.mark.skipif(not HAS_FFMPEG, reason="CPU FFmpeg fixture requires ffmpeg/ffprobe")
def test_real_tiny_preview_is_animated_draft_and_cached(creation, tmp_path, monkeypatch):
    monkeypatch.setattr(media, "SIZE", (90, 160))
    result = media.render_creation(creation, {"kind": "creation", "mode": "preview"}, tmp_path)
    assert result["draft"] and not result["publishable"] and result["estimated_cost_usd"] == 0
    assert result["render_type"] == "draft-typography-animatic"
    assert result["requested_style"] == "watercolor-anime" and "does not render" in result["preview_note"]
    assert not (tmp_path / "provider.json").exists()
    info = media._probe(tmp_path / "video.mp4", tmp_path)
    assert len(info["streams"]) == 2
    assert abs(float(info["format"]["duration"]) - 5) < .1
    assert "00:00:00,000 --> 00:00:05,000" in (tmp_path / "captions.srt").read_text()
    frames = []
    for t in (0, 1):
        output = subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-threads", "3",
                                 "-ss", str(t), "-i", str(tmp_path / "video.mp4"),
                                 "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
                                check=True, capture_output=True, timeout=15).stdout
        frames.append(Image.open(io.BytesIO(output)).convert("RGB"))
    assert ImageChops.difference(*frames).getbbox()
    monkeypatch.setattr(media, "_ffmpeg", Mock(side_effect=AssertionError("Cache must skip")))
    assert media.render_creation(creation, {"mode": "preview"}, tmp_path) == result


def test_real_tiny_cloud_http_motion_concat_captions_and_audio(
        creation, tiny_video, tmp_path, monkeypatch):
    enable(monkeypatch)
    monkeypatch.setattr(media, "SIZE", (90, 160))
    creation["brief"]["duration"] = 10
    creation["brief"]["audio"] = "ambient"
    shot = copy.deepcopy(creation["plan"]["shots"][0])
    shot["number"], shot["caption"] = 2, "A new discovery"
    creation["plan"]["shots"].append(shot)
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "POST":
            body = json.loads(request.content)
            assert body["input"]["aspect_ratio"] == "9:16"
            assert body["input"]["num_frames"] == 81
            assert body["input"]["resolution"] == "480p"
            assert "Copper fox" in body["input"]["prompt"]
            assert request.url.path == "/v1/models/wan-video/wan-2.2-t2v-fast/predictions"
            return httpx.Response(201, json={"id": f"id{len(calls)}"})
        return httpx.Response(200, json={"status": "succeeded",
                                       "output": "https://tjzk.replicate.delivery/video.mp4"})

    http_provider(monkeypatch, handler)
    fake_delivery(monkeypatch, tiny_video, length=str(len(tiny_video)))
    result = media.render_creation(creation, {"mode": "cloud", "max_cost_usd": 1}, tmp_path)
    assert result["kind"] == "creation" and result["seconds"] == 10
    assert result["source_width"] == 480 and "not native HD" in result["source_resolution"]["label"]
    assert result["estimated_cost_usd"] == .5 and result["publishable"] is False
    assert result["source_model"] == media.MODEL and "Burned-in" in result["captions"]
    assert "no speech" in result["audio_note"] and result["guide_audio"] is True
    probe = media._probe(tmp_path / "video.mp4", tmp_path)
    assert abs(float(probe["format"]["duration"]) - 10) < .1
    assert {s["codec_type"] for s in probe["streams"]} == {"audio", "video"}
    assert len(calls) == 4
    assert "00:00:05,000 --> 00:00:10,000" in (tmp_path / "captions.srt").read_text()
    # Tiny real decoder confirms overlay affected source pixels in the lower safe area.
    decoded = subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-threads", "3",
                              "-i", str(tmp_path / "video.mp4"), "-frames:v", "1",
                              "-f", "image2pipe", "-vcodec", "png", "-"],
                             check=True, capture_output=True, timeout=15).stdout
    image = Image.open(io.BytesIO(decoded)).convert("RGB")
    area = image.crop((0, 110, 90, 145))
    assert area.getextrema()[0][1] > 100  # white caption on blue video
    journal = json.loads((tmp_path / "provider.json").read_text())
    assert len(journal["shots"]) == 2 and all(s["clip_sha256"] for s in journal["shots"].values())
    assert "https" not in json.dumps(journal)
    # A completed retry must not make a provider request, even a GET.
    assert media.render_creation(creation, {"mode": "cloud", "max_cost_usd": 1}, tmp_path) == result
    assert len(calls) == 4


@pytest.mark.skipif(not HAS_FFMPEG, reason="CPU FFmpeg fixture requires ffmpeg/ffprobe")
def test_real_tiny_narration_aligns_shots_and_reports_actual_audio(
        creation, tmp_path, monkeypatch):
    import wave

    monkeypatch.setattr(media, "SIZE", (90, 160))
    monkeypatch.setattr(media, "_tts_engine", lambda: ("/fixed/espeak-ng", "en"))
    original_run = media._run
    spoken = []

    def system_speech_fixture(args, out, **kwargs):
        if args[0] != "/fixed/espeak-ng":
            return original_run(args, out, **kwargs)
        spoken.append(kwargs["stdin"])
        target = args[args.index("-w") + 1]
        with wave.open(target, "wb") as stream:
            stream.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
            stream.writeframes(b"\0\0" * 24000)
        return ""

    monkeypatch.setattr(media, "_run", system_speech_fixture)
    creation["brief"]["audio"] = "narration"
    creation["brief"]["duration"] = 10
    shot = copy.deepcopy(creation["plan"]["shots"][0])
    shot["number"] = 2
    shot["narration"], shot["caption"] = "", "Caption becomes spoken words"
    creation["plan"]["shots"].append(shot)
    result = media.render_creation(creation, {"mode": "preview"}, tmp_path)
    assert spoken == ["Ember explores the moon.", "Caption becomes spoken words"]
    assert result["audio_mode"] == "narration" and result["guide_audio"] is False
    assert result["narration_engine"] == "espeak-ng" and result["publishable"] is False
    for i in range(2):
        assert abs(float(media._probe(tmp_path / f"audio-{i:02}.wav", tmp_path)
                         ["format"]["duration"]) - 5) < .01
    assert abs(float(media._probe(tmp_path / "video.mp4", tmp_path)
                     ["format"]["duration"]) - 10) < .1


def test_full_resolution_safe_area_overlay_has_two_lines_and_no_filter_text(tmp_path):
    path = media._caption_overlay({"caption": "Welcome to a bright new world!"}, 0, tmp_path)
    with Image.open(path) as image:
        assert image.size == (1080, 1920)
        assert image.mode == "RGBA"
        box = image.getbbox()
        assert box[1] > 1920 * .65 and box[3] < 1920 * .85
    # Pathological wide text is rejected rather than made unreadably tiny.
    with pytest.raises(media.MediaError, match="two readable lines"):
        media._caption_overlay({"caption": "w" * 120}, 1, tmp_path)
