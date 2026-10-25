# Video Studio

A prompt-to-short-video creator for original YouTube Shorts, Instagram Reels and Facebook Reels, alongside the original Pond Studio nursery editor and multi-channel CLI.

**Ubuntu:** from this checkout, run `bash scripts/setup/50-ubuntu.sh --with-dev --run`. It installs the web dependencies, SQLite/FFmpeg, Claude Code (`claude`), GitHub CLI, Codex and the supported OpenCode version. See the [Ubuntu setup guide](docs/7-web-studio/35-ubuntu-setup.md) for dry-run, login, optional Claude Desktop and remote-access commands.

With Node 22+ installed, use `npm run setup:plan`, `npm run setup` (Ubuntu), then `npm start`. `npm test` runs offline checks; `npm run` lists commands. For Node-only installation on Ubuntu, use `bash scripts/setup/05-node.sh`; see the guide for prerequisites and PATH activation. The full Ubuntu setup also installs Node when needed.

```sh
make deps-web
make web
```

Open **http://127.0.0.1:8080/create** for the general creator, or **http://127.0.0.1:8080** for the nursery editor. Give the creator an idea, style, duration, audience and optional cast descriptions. Review the shot plan, generate, then download the MP4 and review package. The isolated `.venv-web` keeps the editor separate from the existing ML environment. FFmpeg is required.

The creator separates real managed AI motion from a clearly labelled local timing preview. Style presets cover hand-painted fantasy anime, anime, cartoon, cinematic, live-action and motion graphics. A style request does not guarantee visual quality or character identity across shots. Managed generation needs a server-side provider credential and an explicitly enabled budget; no paid requests or model downloads were performed during implementation. OpenCode planning uses verified free models when available and labels its template fallback. Automatic system narration is synthetic speech, not singing. The nursery editor retains its original Pillow/FFmpeg 2D renderer.

The worker requires green memory pressure and at least 10 GB free disk, limits CPU pools to three threads and runs one job at a time. A resource refusal is a reason to wait, not bypass the guard. Sampling cannot prevent every memory spike.

Exports include MP4, poster, captions, saved project, review checklist and media hashes. Review readiness is not publishing approval. The app never posts videos; kids content remains made for kids with AI disclosure, and final publishing requires the separate human-reviewed YouTube path.

```sh
make test-web
```

See the [creator workflow](docs/7-web-studio/33-universal-video-workflow.md), [generation research](docs/7-web-studio/34-generation-research.md), [app guide](docs/7-web-studio/README.md), [delivery plan](docs/7-web-studio/29-web-studio-plan.md), [API contract](docs/7-web-studio/30-api-contract.md) and [personal EC2 runbook](docs/7-web-studio/31-ec2-runbook.md). CPU container packaging is supplied; container execution, EC2 deployment and CUDA inference remain unverified. No cloud resources have been created.
