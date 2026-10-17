"""Visual sources for the images stage (doc 14 §6-7, doc 15 §2, doc 04 §4).

- ``stability``: Stability AI text-to-image on Bedrock (paid per image, us-west-2).
- ``pixabay``: free stock video/photos through the Pixabay API (24 h cache, credits logged).
- ``luma``: Luma Ray2 hero clip on Bedrock (async, S3 output). Gated: STUDIO_ALLOW_PAID_VIDEO=1.

Each source returns a ``Visual`` that the images stage stores in ``images/visuals.json``.
"""

from studio.stages.visuals.common import VIDEO_SUFFIXES, BeatSpec, Visual, is_video

__all__ = ["VIDEO_SUFFIXES", "BeatSpec", "Visual", "is_video"]
