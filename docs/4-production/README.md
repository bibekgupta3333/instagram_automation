# 4 — Production

Before storyboarding: [6 — Script library](../6-script-library/README.md) provides 220 original treatments, with [22 — Directing and rights](../6-script-library/22-directing-and-rights-playbook.md) explaining the conversion to verified production scripts.

Proposed pilot: [21 — Kids production requirements](21-kids-pilot-production.md): supported tools, animation gaps, accurate audience settings and original asset/music rights.

How videos get made. Reading order:

1. [04 — Content pipeline architecture](04-content-pipeline-architecture.md): reference architecture, repos, vendors, cost per video, QA gate.
2. [14 — Local models on M4 Pro](14-local-models-mac-m4.md): what runs on the Mac (LLM, TTS, ASR, image, music) and the nightly batch.
3. [18 — Lightweight local image and video models](18-local-image-video-models.md): small open-source image and video models that fit the disk (klein 4-bit, Wan Turbo GGUF, parallax), runtimes and disk traps. **Supersedes 14 §6–7.**
4. [24 — Local Wan video pilot](24-local-wan-video-pilot.md): optional quantized reference-to-video setup, exact component downloads and guarded testing on the 24 GB Mac.
5. [25 — Script workflow](25-script-workflow.md): topic intake, the automated and `/youtube-script` paths, the reviewer checklist before `studio approve`, and the 24 h/72 h iteration loop.

Archived: [09 — AI avatar presenters](../archive/09-ai-avatar-presenters.md) moved to `archive/` on 2026-10-16, when HeyGen and the avatars were removed.
