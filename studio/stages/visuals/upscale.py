"""Real-ESRGAN x4plus upscaling (BSD-3, 67 MB) via spandrel on MPS, falling back to CPU.

Stills are generated small (e.g. 544x960) and upscaled x4, then lanczos-resized and
centre-cropped to exactly the frame size (doc 18 §5). The model is never downloaded here:
run `make models-dry STAGE=image` then `make models STAGE=image`. The model stays resident
between calls; `unload()` frees it.
"""

from __future__ import annotations

import gc
import os
import sys
from pathlib import Path
from typing import Any

from studio.stages.base import EngineUnavailable
from studio.stages.visuals.common import configure_cpu_threads

REPO = "Comfy-Org/Real-ESRGAN_repackaged"
FILENAME = "RealESRGAN_x4plus.safetensors"
SCALE = 4
TILE = 384  # input pixels per tile side; 384 -> 1536 output keeps MPS memory < ~2 GB
TILE_PAD = 16  # overlap so tile seams are invisible

_model: Any = None
_device: str | None = None


def hf_hub_dir() -> Path:
    return Path(os.environ.get("HF_HUB_CACHE")
                or Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub")


def hf_cached_file(repo: str, filename: str) -> Path | None:
    """Path of `filename` in the local HF cache for `repo`, or None (never touches the network)."""
    snaps = hf_hub_dir() / f"models--{repo.replace('/', '--')}" / "snapshots"
    if not snaps.is_dir():
        return None
    for snap in sorted(snaps.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        f = snap / filename
        if f.exists():  # follows the blob symlink, so a dangling link counts as missing
            return f
    return None


def model_path() -> Path:
    override = os.environ.get("STUDIO_REALESRGAN_PATH")
    path = Path(override) if override else hf_cached_file(REPO, FILENAME)
    if path is None or not path.exists():
        raise EngineUnavailable(
            f"upscale: Real-ESRGAN x4plus ({REPO}/{FILENAME}, 67 MB) is not downloaded. Run "
            "`make models-dry STAGE=image` then `make models STAGE=image` "
            "(disk-checked), or set STUDIO_REALESRGAN_PATH"
        )
    return path


def available() -> bool:
    try:
        model_path()
    except EngineUnavailable:
        return False
    return True


def cover_geometry(src_w: int, src_h: int, width: int, height: int) -> tuple[int, int, int, int]:
    """Resize-to-cover then centre-crop: returns (scaled_w, scaled_h, x0, y0) such that the
    scaled image is >= width x height with the same aspect ratio, and the crop box
    (x0, y0, x0+width, y0+height) is centred inside it."""
    if min(src_w, src_h, width, height) <= 0:
        raise ValueError("sizes must be positive")
    scale = max(width / src_w, height / src_h)
    sw = max(width, round(src_w * scale))
    sh = max(height, round(src_h * scale))
    return sw, sh, (sw - width) // 2, (sh - height) // 2


def _load() -> tuple[Any, str]:
    global _model, _device
    if _model is not None:
        return _model, _device  # type: ignore[return-value]
    path = model_path()
    try:
        import torch
        from spandrel import ModelLoader
    except ImportError as exc:  # pragma: no cover - deps are in the local-audio extra
        raise EngineUnavailable(f"upscale: needs torch + spandrel ({exc})") from exc
    configure_cpu_threads(torch=torch)
    desc = ModelLoader().load_from_file(str(path))
    if desc.scale != SCALE:
        raise EngineUnavailable(f"upscale: expected a x{SCALE} model, got x{desc.scale}")
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    net = desc.model.eval().to(device)
    if device == "mps":
        net = net.half()  # ~25% faster than fp32 on MPS and half the memory; visually identical
    _model, _device = net, device
    return _model, device


def _run_tiled(model: Any, img: Any, device: str) -> Any:
    """img: float32 tensor (1,3,H,W) on CPU in [0,1]. Returns (1,3,4H,4W) on CPU."""
    import torch

    configure_cpu_threads(torch=torch)
    _, c, h, w = img.shape
    out = torch.zeros((1, c, h * SCALE, w * SCALE), dtype=torch.float32)
    dtype = next(model.parameters()).dtype
    with torch.inference_mode():
        for y0 in range(0, h, TILE):
            for x0 in range(0, w, TILE):
                y1, x1 = min(y0 + TILE, h), min(x0 + TILE, w)
                py0, px0 = max(y0 - TILE_PAD, 0), max(x0 - TILE_PAD, 0)
                py1, px1 = min(y1 + TILE_PAD, h), min(x1 + TILE_PAD, w)
                tile = img[:, :, py0:py1, px0:px1].to(device, dtype=dtype)
                res = model(tile).float().clamp_(0, 1).cpu()
                oy, ox = (y0 - py0) * SCALE, (x0 - px0) * SCALE
                out[:, :, y0 * SCALE:y1 * SCALE, x0 * SCALE:x1 * SCALE] = res[
                    :, :, oy:oy + (y1 - y0) * SCALE, ox:ox + (x1 - x0) * SCALE]
                del tile, res
                _empty_cache(device)
    return out


def _empty_cache(device: str | None) -> None:
    # Cleanup cannot initialise a GPU runtime by importing torch or probing MPS.
    torch = sys.modules.get("torch")
    if device == "mps" and torch is not None:
        torch.mps.empty_cache()


def upscale_to(path_in: Path | str, path_out: Path | str, width: int, height: int) -> Path:
    """Upscale `path_in` x4 with Real-ESRGAN, then lanczos-resize and centre-crop to exactly
    width x height. If the source already covers the frame, the model is skipped."""
    import cv2
    import numpy as np

    configure_cpu_threads(cv2=cv2)
    path_in, path_out = Path(path_in), Path(path_out)
    src = cv2.imread(str(path_in), cv2.IMREAD_COLOR)
    if src is None:
        raise ValueError(f"upscale: cannot read {path_in}")
    h, w = src.shape[:2]
    if w < width or h < height:
        import torch

        model, device = _load()
        rgb = cv2.cvtColor(src, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        t = torch.from_numpy(rgb).permute(2, 0, 1).unsqueeze(0).contiguous()
        try:
            up = _run_tiled(model, t, device)
        finally:
            _empty_cache(device)
        arr = (up[0].permute(1, 2, 0).numpy() * 255.0 + 0.5).astype(np.uint8)
        src = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
        h, w = src.shape[:2]
    sw, sh, x0, y0 = cover_geometry(w, h, width, height)
    if (sw, sh) != (w, h):
        interp = cv2.INTER_AREA if sw < w else cv2.INTER_LANCZOS4
        src = cv2.resize(src, (sw, sh), interpolation=interp)
    out = src[y0:y0 + height, x0:x0 + width]
    path_out.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(path_out), out):
        raise ValueError(f"upscale: cannot write {path_out}")
    return path_out


def unload() -> None:
    global _model, _device
    device = _device
    _model, _device = None, None
    gc.collect()
    _empty_cache(device)
