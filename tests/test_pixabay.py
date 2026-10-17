"""Pixabay stock engine (studio/stages/visuals/pixabay.py). Offline: httpx.MockTransport."""

import json
import logging
import os
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from studio.config import DEFAULT_BRAND_DIR, Settings, load_brand
from studio.contracts import Beat, Script, VideoJob
from studio.pipeline.mock_llm import MockChatClient
from studio.stages import images
from studio.stages.base import EngineUnavailable, StageContext, StageError
from studio.stages.visuals import BeatSpec, pixabay

KEY = "test-secret-key-123"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path_factory):
    for k in list(os.environ):
        if k.startswith(("STUDIO_IMAGES", "STUDIO_PIXABAY", "STUDIO_MFLUX_CMD",
                         "STUDIO_ALLOW_MODEL")):
            monkeypatch.delenv(k, raising=False)
    for k in ("PIXABAY_API_KEY", "AWS_BEARER_TOKEN_BEDROCK", "AWS_PROFILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path_factory.mktemp("hf-hub")))
    monkeypatch.setenv("STUDIO_SKIP_LEDGER", "1")


def brand(slug="car-cost-truth"):
    return load_brand(slug, DEFAULT_BRAND_DIR)


def spec(tmp_path, fmt="short", index=0, visual="a red pickup truck on a desert highway"):
    b = brand()
    return BeatSpec(job_id="job-x", channel=b.slug, index=index, fmt=fmt, visual_prompt=visual,
                    prompt=images.image_prompt(visual, b), out_stem=tmp_path / f"beat_{index:02d}",
                    style_prompt=b.style_prompt)


def rend(w, h, name):
    return {"url": f"https://cdn.pixabay.com/video/{name}.mp4", "width": w, "height": h,
            "size": 1000, "thumbnail": ""}


def vhit(vid, w, h, duration=10, user="Ana"):
    return {"id": vid, "pageURL": f"https://pixabay.com/videos/x-{vid}/", "tags": "truck, desert",
            "duration": duration, "user": user, "user_id": 7,
            "videos": {"large": {"url": "", "width": 0, "height": 0, "size": 0},
                       "medium": rend(w, h, f"{vid}-medium"),
                       "small": rend(w * 2 // 3, h * 2 // 3, f"{vid}-small"),
                       "tiny": rend(w // 2, h // 2, f"{vid}-tiny")}}


def phit(pid, user="Cy"):
    return {"id": pid, "pageURL": f"https://pixabay.com/photos/x-{pid}/", "tags": "truck",
            "user": user, "user_id": 9, "imageWidth": 3000, "imageHeight": 4500,
            "largeImageURL": f"https://pixabay.com/get/{pid}_1280.jpg"}


class Api:
    """A fake Pixabay: answers searches and downloads, records every request."""

    def __init__(self, videos=(), photos=(), statuses=(), headers=None):
        self.videos, self.photos = list(videos), list(photos)
        self.statuses = list(statuses)  # forced statuses for the next search calls
        self.headers = headers or {"X-RateLimit-Limit": "100", "X-RateLimit-Remaining": "99",
                                   "X-RateLimit-Reset": "60"}
        self.searches, self.downloads = [], []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.url.host == "pixabay.com" and request.url.path.startswith("/api/"):
            params = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
            self.searches.append((request.url.path, params))
            if self.statuses:
                return httpx.Response(self.statuses.pop(0), headers={**self.headers,
                                      "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1"},
                                      text="API rate limit exceeded")
            hits = self.videos if "videos" in request.url.path else self.photos
            return httpx.Response(200, headers=self.headers,
                                  json={"total": len(hits), "totalHits": len(hits), "hits": hits})
        self.downloads.append(url)
        return httpx.Response(200, content=b"\x00\x00\x00\x18ftypmp42data")

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def test_request_params_and_vertical_selection(tmp_path):
    api = Api(videos=[vhit(1, 1920, 1080), vhit(2, 1080, 1920, user="Bo"),
                      vhit(3, 1080, 1920, duration=2)])
    vis = pixabay.fetch(spec(tmp_path), api_key=KEY, http=api.client(), cache_dir=None)
    path, params = api.searches[0]
    assert path == "/api/videos/"
    assert params["key"] == KEY and params["q"] == "red pickup truck desert"
    assert params["safesearch"] == "true" and params["per_page"] == "20"
    assert params["video_type"] == "film" and "orientation" not in params  # no video filter
    assert api.downloads == ["https://cdn.pixabay.com/video/2-medium.mp4"]  # vertical, >=4 s
    assert vis.kind == "video" and vis.engine == "pixabay" and vis.path == tmp_path / "beat_00.mp4"
    assert vis.path.exists() and (vis.source["width"], vis.source["height"]) == (1080, 1920)
    assert vis.source["creator"] == "Bo" and vis.source["id"] == 2
    assert vis.source["source_url"] == "https://pixabay.com/videos/x-2/"
    assert KEY not in json.dumps(vis.to_json())


def test_long_form_prefers_horizontal_and_no_reuse(tmp_path):
    api = Api(videos=[vhit(1, 1080, 1920), vhit(2, 1920, 1080), vhit(4, 1920, 1080)])
    used: set[str] = set()
    a = pixabay.fetch(spec(tmp_path, fmt="long"), api_key=KEY, http=api.client(), used=used)
    b = pixabay.fetch(spec(tmp_path, fmt="long", index=1), api_key=KEY, http=api.client(),
                      used=used)
    assert (a.source["id"], b.source["id"]) == (2, 4)  # never the same clip twice
    assert used == {"video:2", "video:4"}


def test_rendition_short_side_1080_when_available():
    hit = vhit(1, 1920, 1080)
    hit["videos"]["large"] = rend(3840, 2160, "1-large")
    assert pixabay.pick_rendition(hit)["width"] == 1920  # smallest with short side >= 1080
    small = vhit(2, 1280, 720)
    assert pixabay.pick_rendition(small)["width"] == 1280  # else the largest


def test_photo_fallback(tmp_path):
    api = Api(videos=[], photos=[phit(9)])
    vis = pixabay.fetch(spec(tmp_path), api_key=KEY, http=api.client())
    photo_params = next(p for path, p in api.searches if path == "/api/")
    assert photo_params["image_type"] == "photo" and photo_params["orientation"] == "vertical"
    assert photo_params["safesearch"] == "true"
    assert (photo_params["min_width"], photo_params["min_height"]) == ("1080", "1920")
    assert vis.kind == "image" and vis.path.suffix == ".jpg" and vis.source["creator"] == "Cy"
    assert (vis.source["width"], vis.source["height"]) == (853, 1280)  # largeImageURL cap
    assert api.downloads == ["https://pixabay.com/get/9_1280.jpg"]


def test_relaxes_query_then_gives_up(tmp_path):
    api = Api()
    with pytest.raises(StageError, match="nothing found"):
        pixabay.fetch(spec(tmp_path), api_key=KEY, http=api.client())
    qs = [p["q"] for _, p in api.searches]
    assert qs == ["red pickup truck desert", "red pickup"] * 2  # videos, then photos


def test_cache_hit_within_24h_and_expiry(tmp_path):
    api = Api(videos=[vhit(2, 1080, 1920)])
    cache = tmp_path / "cache"
    clock = [1_000_000.0]
    kw = {"api_key": KEY, "cache_dir": cache, "now": lambda: clock[0]}
    params = {"q": "truck", "safesearch": "true", "per_page": 20}
    with api.client() as c:
        first = pixabay.search(c, pixabay.VIDEO_SEARCH, params, **kw)
        clock[0] += 23 * 3600
        again = pixabay.search(c, pixabay.VIDEO_SEARCH, params, **kw)
        assert first == again and len(api.searches) == 1  # served from disk
        files = list(cache.glob("*.json"))
        assert len(files) == 1 and KEY not in files[0].read_text()  # key never cached
        clock[0] += 2 * 3600  # > 24 h after the fetch
        pixabay.search(c, pixabay.VIDEO_SEARCH, params, **kw)
        assert len(api.searches) == 2


def test_429_backs_off_then_succeeds_and_gives_up(tmp_path):
    api = Api(videos=[vhit(2, 1080, 1920)], statuses=[429, 429])
    sleeps = []
    lim = pixabay.RateLimiter(sleep=sleeps.append)
    with api.client() as c:
        data = pixabay.search(c, pixabay.VIDEO_SEARCH, {"q": "truck"}, api_key=KEY,
                              limiter=lim, sleep=sleeps.append)
    assert data["hits"] and len(api.searches) == 3
    assert len(sleeps) >= 2 and all(0 < s <= 61 for s in sleeps)
    assert lim.last_headers["X-RateLimit-Limit"] == "100"
    api = Api(statuses=[429] * 10)
    with api.client() as c, pytest.raises(StageError, match="429"):
        pixabay.search(c, pixabay.VIDEO_SEARCH, {"q": "truck"}, api_key=KEY,
                       limiter=pixabay.RateLimiter(sleep=lambda s: None), sleep=lambda s: None)


def test_rate_limiter_waits_when_window_used_up():
    sleeps = []
    lim = pixabay.RateLimiter(sleep=sleeps.append)
    lim.after({"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "5"})
    lim.before()
    assert sleeps and 0 < sleeps[0] <= 5


def test_key_redacted_in_logs_and_errors(tmp_path, caplog):
    assert pixabay.redact(f"https://pixabay.com/api/?key={KEY}&q=x") == \
        "https://pixabay.com/api/?key=***&q=x"
    api = Api(statuses=[429, 500])
    caplog.set_level(logging.DEBUG)
    with api.client() as c, pytest.raises(StageError) as err:
        pixabay.search(c, pixabay.VIDEO_SEARCH, {"q": "truck"}, api_key=KEY,
                       limiter=pixabay.RateLimiter(sleep=lambda s: None), sleep=lambda s: None)
    assert KEY not in str(err.value) and "key=***" in str(err.value)
    assert KEY not in caplog.text  # httpx's own "HTTP Request: GET ...?key=" line too
    assert "key=***" in caplog.text

    def boom(request):
        raise httpx.ConnectError(f"cannot reach {request.url}")

    with httpx.Client(transport=httpx.MockTransport(boom)) as c, \
            pytest.raises(StageError) as err:
        pixabay.search(c, pixabay.VIDEO_SEARCH, {"q": "truck"}, api_key=KEY)
    assert KEY not in str(err.value)


def test_needs_key_and_bad_key(tmp_path):
    with pytest.raises(EngineUnavailable):
        pixabay.fetch(spec(tmp_path), api_key="")

    def bad(request):
        return httpx.Response(400, text="[ERROR 400] Invalid or missing API key")

    with httpx.Client(transport=httpx.MockTransport(bad)) as c, \
            pytest.raises(EngineUnavailable, match="PIXABAY_API_KEY"):
        pixabay.fetch(spec(tmp_path), api_key=KEY, http=c)


def test_suits_stock(tmp_path):
    assert pixabay.suits_stock(spec(tmp_path), brand("car-cost-truth"))
    assert not pixabay.suits_stock(spec(tmp_path, visual="isometric cutaway of an engine"),
                                   brand("car-cost-truth"))
    assert not pixabay.suits_stock(spec(tmp_path), brand("furnace-to-faucet"))  # 3D cutaways
    assert pixabay.suits_stock(spec(tmp_path), brand("furnace-to-faucet"), ("furnace-to-faucet",))


def test_stage_writes_attribution_and_manifest(tmp_path, monkeypatch):
    api = Api(videos=[vhit(2, 1080, 1920, user="Bo"), vhit(5, 1080, 1920, user="Di")])
    real_client = pixabay._client
    monkeypatch.setattr(pixabay, "_client", lambda key, http: real_client(key, api.client()))
    monkeypatch.setenv("PIXABAY_API_KEY", KEY)
    monkeypatch.setenv("STUDIO_IMAGES_AUTO_ORDER", "pixabay,mock")
    beats = [Beat(text=f"Beat {i}.", visual_prompt=f"a red pickup truck on a desert highway {i}")
             for i in range(2)]
    job = VideoJob(job_id="job-x", channel="car-cost-truth", format="short", topic="t",
                   script=Script(title="T", hook="H?", beats=beats, description="d"),
                   workdir=tmp_path / "job")
    ctx = StageContext(settings=Settings(output_dir=tmp_path / "out"), chat=MockChatClient(),
                       engines={"images": "auto"})
    job = images.run(job, brand(), ctx)
    img = tmp_path / "job" / "images"
    credits = (img / "attribution.txt").read_text().splitlines()
    assert credits == ["Video by Bo on Pixabay (id 2): https://pixabay.com/videos/x-2/",
                       "Video by Di on Pixabay (id 5): https://pixabay.com/videos/x-5/"]
    manifest = (img / "visuals.json").read_text()
    beats_json = json.loads(manifest)["beats"]
    assert [b["visual"]["source"]["id"] for b in beats_json] == [2, 5]
    assert all(b["visual"]["source"]["creator"] for b in beats_json)
    assert KEY not in manifest
    assert list((tmp_path / "out" / ".cache" / "pixabay").glob("*.json"))  # 24 h cache on disk
