"""Visual sources for the images stage (doc 14 §6-7, doc 04 §4).

- ``pixabay``: free stock video/photos through the Pixabay API (24 h cache, credits logged).
- ``upscale``: Real-ESRGAN x4 for local mflux stills (Z-Image-Turbo / FLUX.2 klein).

Bedrock is for text models only: the Bedrock Stability / Luma sources were removed 2026-10-16.
Each source returns a ``Visual`` that the images stage stores in ``images/visuals.json``.
"""

from studio.stages.visuals.common import VIDEO_SUFFIXES, BeatSpec, Visual, is_video

__all__ = ["VIDEO_SUFFIXES", "BeatSpec", "Visual", "is_video"]
