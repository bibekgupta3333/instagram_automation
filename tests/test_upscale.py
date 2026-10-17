"""Real-ESRGAN upscaler (studio/stages/visuals/upscale.py). Offline: the model is faked with a
nearest-neighbour x4 net, so the tiling, resize and crop maths run without the weights. The
real-model test runs only with STUDIO_REAL_MODEL_TESTS=1 and the weights downloaded."""

import os

import cv2
import numpy as np
import pytest
import torch

from studio.stages.base import EngineUnavailable
from studio.stages.visuals import upscale


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.delenv("STUDIO_REALESRGAN_PATH", raising=False)
    upscale.unload()
    yield
    upscale.unload()


@pytest.mark.parametrize(("src", "dst", "expect"), [
    ((544, 960), (1080, 1920), (1088, 1920, 4, 0)),       # Z-Image 9:16 -> Shorts
    ((2176, 3840), (1080, 1920), (1088, 1920, 4, 0)),     # after x4: downscale then crop
    ((1920, 1080), (1080, 1920), (3413, 1920, 1166, 0)),  # landscape -> portrait, centre crop
    ((1024, 1024), (1920, 1080), (1920, 1920, 0, 420)),
    ((1080, 1920), (1080, 1920), (1080, 1920, 0, 0)),
])
def test_cover_geometry(src, dst, expect):
    sw, sh, x0, y0 = upscale.cover_geometry(*src, *dst)
    assert (sw, sh, x0, y0) == expect
    assert sw >= dst[0] and sh >= dst[1]
    assert x0 + dst[0] <= sw and y0 + dst[1] <= sh
    assert abs(sw / sh - src[0] / src[1]) < 0.01  # aspect preserved


def test_cover_geometry_rejects_zero():
    with pytest.raises(ValueError):
        upscale.cover_geometry(0, 10, 10, 10)


def test_missing_model_raises_clear_error(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "empty-hub"))
    assert not upscale.available()
    img = tmp_path / "a.png"
    cv2.imwrite(str(img), np.zeros((96, 54, 3), np.uint8))
    with pytest.raises(EngineUnavailable, match="not downloaded"):
        upscale.upscale_to(img, tmp_path / "b.png", 1080, 1920)


def test_hf_cache_lookup(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path))
    snap = tmp_path / "models--Comfy-Org--Real-ESRGAN_repackaged" / "snapshots" / "abc"
    snap.mkdir(parents=True)
    (snap / "README.md").write_text("x")
    assert upscale.hf_cached_file(upscale.REPO, upscale.FILENAME) is None
    (snap / upscale.FILENAME).write_bytes(b"w")
    assert upscale.model_path() == snap / upscale.FILENAME


def test_large_source_skips_model(tmp_path, monkeypatch):
    def boom():
        raise AssertionError("model must not load when the source already covers the frame")

    monkeypatch.setattr(upscale, "_load", boom)
    img = tmp_path / "big.png"
    cv2.imwrite(str(img), np.full((2000, 1500, 3), 128, np.uint8))
    out = upscale.upscale_to(img, tmp_path / "o.png", 1080, 1920)
    assert cv2.imread(str(out)).shape == (1920, 1080, 3)


class _NearestX4(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.w = torch.nn.Parameter(torch.ones(1))

    def forward(self, x):
        return torch.nn.functional.interpolate(x, scale_factor=4, mode="nearest") * self.w


def test_tiled_upscale_is_seamless_and_exact_size(tmp_path, monkeypatch):
    monkeypatch.setattr(upscale, "_load", lambda: (_NearestX4(), "cpu"))
    monkeypatch.setattr(upscale, "TILE", 64)  # many tiles, odd remainders
    rng = np.random.default_rng(0)
    src = rng.integers(0, 255, (150, 90, 3), dtype=np.uint8)
    t = torch.from_numpy(src.astype(np.float32) / 255).permute(2, 0, 1)[None]
    up = upscale._run_tiled(_NearestX4(), t, "cpu")[0].permute(1, 2, 0).numpy()
    ref = cv2.resize(src, (360, 600), interpolation=cv2.INTER_NEAREST).astype(np.float32) / 255
    assert np.abs(up - ref).max() < 1e-6  # tiles stitch exactly

    img = tmp_path / "s.png"
    cv2.imwrite(str(img), src)
    out = upscale.upscale_to(img, tmp_path / "o.png", 1080, 1920)
    assert cv2.imread(str(out)).shape == (1920, 1080, 3)


@pytest.mark.skipif(os.environ.get("STUDIO_REAL_MODEL_TESTS") != "1" or not upscale.available(),
                    reason="set STUDIO_REAL_MODEL_TESTS=1 with Real-ESRGAN downloaded")
def test_real_model_upscale(tmp_path):
    img = tmp_path / "s.png"
    grad = np.tile(np.linspace(0, 255, 136, dtype=np.uint8), (240, 1))
    cv2.imwrite(str(img), cv2.merge([grad, grad, grad]))
    out = upscale.upscale_to(img, tmp_path / "o.png", 1080, 1920)
    assert cv2.imread(str(out)).shape == (1920, 1080, 3)
