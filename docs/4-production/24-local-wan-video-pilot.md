# 24 · Local Wan video pilot on M4 Pro 24 GB

> **Current status · 2026-10-18:** All project-installed local model weights were
> removed at the user's request; verification is recorded in
> `output/model-removal/result.json`. Source, generated media, executable runtimes
> and historical benchmarks below are preserved. Rhubarb's executable remains,
> but its bundled Sphinx speech resources and release ZIP were removed.
> Installation commands below are retained for reference: reinstall weights only
> after an explicit future user request. Generation requires those weights.

**Use the tiny-codec command below for the smaller, faster pilot.** A real neural
reference-to-video test completed on this Mac using Wan2.1 VACE 1.3B v3 Q4, a UMT5
Q3 text encoder and TAEW2.1. The fictional reference face came from the installed
Z-Image q4 model. No SVG animation, parallax, cloud video or stock footage was used.

## 1. Measured results · 2026-10-18

Both runs used the same reference, prompt, seed 18042, 320 × 384 pixels, 17 requested
frames, 16 fps and 20 Euler steps. Each output is **1.0625 seconds**, silent and
unapproved. Every frame decoded successfully; all 17 decoded frames are distinct.
Spaced-frame inspection shows a blink, slight head turn and smile. Facial detail is
soft and jacket texture changes: these are low-resolution experiments, not finished
YouTube episodes or audio-driven talking heads.

| Codec | Active model files | Generation time | Native child maximum RSS | Pressure samples |
|---|---:|---:|---:|---|
| **TAEW2.1 tiny — recommended pilot** | **4.227 GB** | **264.98 s / 4.4 min** | **5.82 GB** | **268, all green** |
| Full Wan2.1 VAE — comparison baseline | 4.459 GB | 1,693.05 s / 28.2 min | 5.91 GB | 1,686, all green |

The tiny run was **6.39× faster**. Its inspected frames are slightly softer, but
retain the same visible action and fictional face. The primary measured benefit
was speed; process-RSS reduction was small. These observations are from one seeded
pair, not a quality benchmark across prompts or proof that future workloads stay green.

RSS uses macOS `resource.getrusage(RUSAGE_CHILDREN).ru_maxrss` around the native
subprocess. It is **not total shared CPU/GPU memory or dedicated VRAM**. The external
guard sampled system pressure once per second and can miss brief spikes. Minimum
free disk was 17.79 GB for tiny and 16.59 GB for full. No OS GPU limits were raised.

Artifacts, under the git-ignored `output/local-video-pilot/` directory:

- `reference.png` and `reference.metadata.json`: Z-Image 512 × 640 fictional face;
  39 green samples, reported peak MLX allocations 4.61 GB, not whole-machine RAM.
- `wan-tiny/video.mp4`: recommended neural motion preview.
- `wan-v3/video.mp4`: full-VAE comparison preview.
- Each video directory contains `raw.avi`, `run.json`, `generation.log`,
  `validation.json` and an inspected `contact-sheet.jpg`.
- `memory-video-tiny.jsonl` / `memory-video-v3.jsonl`: complete generation pressure logs.
- `runtime/release.json`: native runtime URL and verified release checksum.

## 2. Install only the selected components

This optional pilot was explicitly requested after the earlier exhausted model-budget
restriction. It is separate from default Configuration A and does not enable downloads
inside renders. Bedrock remains text-only. Keep **at least 10 GB disk free**, with
additional room for runtime, temporary downloads and generated media.

For a fresh **tiny-codec** setup, inspect the exact dry-run selection first:

```sh
make models-dry STAGE=video OPTIONAL=1 MODEL='wan2.1-vace-1.3b-q4 wan-umt5-q3 wan2.1-tiny-vae'
make models STAGE=video OPTIONAL=1 MODEL='wan2.1-vace-1.3b-q4 wan-umt5-q3 wan2.1-tiny-vae'
```

| Component | Exact file | Bytes |
|---|---|---:|
| VACE transformer, v3 Q4 | `calcuis/wan-1.3b-gguf` → `wan2.1-v3-vace-1.3b-q4_0.gguf` | 1,346,314,016 |
| UMT5 encoder, Q3 | `city96/umt5-xxl-encoder-gguf` → `umt5-xxl-encoder-Q3_K_S.gguf` | 2,858,489,696 |
| Tiny video codec | `Kijai/WanVideo_comfy` → `taew2_1.safetensors` | 22,642,926 |
| **Active tiny bundle** | | **4,227,446,638** |

The full-VAE comparison adds `wan2.1-vae`, which selects only
`Comfy-Org/Wan_2.1_ComfyUI_repackaged/split_files/vae/wan_2.1_vae.safetensors`
(253,815,318 bytes). Both codecs share the transformer and text encoder: total
installed model files for both experiments are **4.481 GB**, not the sum of both
bundle totals. Runtime files occupy additional space.

Never download an entire optional video repository: it may contain dozens of
quantizations and tens of GB. The original non-v3 VACE Q4 file omits
`vace_patch_embedding.weight` and fails metadata validation before inference. It
was removed from this task's cache. Use the exact **v3** filename above.

The transformer, UMT5 and full VAE have Apache-2.0 model cards. The tiny codec comes
from the MIT-licensed [madebyollin/taehv project](https://github.com/madebyollin/taehv),
mirrored by Kijai. Preserve applicable licenses/notices if redistributing weights.
Upstream describes lower time/memory at some quality cost; its hardware benchmarks
are not Mac measurements. Only the paired results in section 1 were measured here.

## 3. Native Metal runtime

The installed [official arm64 release](https://github.com/leejet/stable-diffusion.cpp/releases/download/master-973-f0aac52/sd-master-f0aac52-bin-Darwin-macOS-26.6.2-arm64.zip)
is revision **f0aac52**: 36,476,923 bytes downloaded, approximately 94 MB for its
executable and library. The archive SHA-256 matched the GitHub release digest:

```text
 e5abc3b8a0ff81bd848b32b46d10a07fe33767a54240848ab53c8bc394c5ef95
```

Executable: `output/local-video-pilot/runtime/sd-cli`. Keep the sibling
`libstable-diffusion.dylib`. Device enumeration detected `MTL0 Apple M4 Pro`.
Although the release filename names macOS 26.6.2, both tests actually executed on
this Mac running macOS 26.4. Other OS versions were not tested.

## 4. Generate offline under the resource guard

The installed runner verifies pinned cached-file SHA-256 digests before starting
inference. It never downloads models and refuses to overwrite existing media.
Use a fresh output directory for another run:

```sh
.venv/bin/python scripts/run_guarded.py \
  --log output/local-video-pilot/memory-next.jsonl \
  -- .venv/bin/python scripts/generate_wan_video.py \
  --runtime output/local-video-pilot/runtime/sd-cli \
  --reference output/local-video-pilot/reference.png \
  --output-dir output/local-video-pilot/next-tiny \
  --decoder tiny --width 320 --height 384 --frames 17 --steps 20
```

Use `--decoder full` only for the slower comparison, after explicitly installing
`wan2.1-vae`. The script retains `full` as its argument default, so **include
`--decoder tiny`** in the recommended command above.

The runner uses memory mapping, CPU weight offload, unbatched guidance, flash
attention, spatial/temporal codec tiling and **three CPU threads**. The native
`--max-vram 6` limit budgets managed runtime allocations in GiB; it does not cap all
unified memory. Spatial tiles are 128 × 128. Wan's full VAE does not support temporal
tiling during encoding, which contributed to the slow baseline.

Only one heavy model may run at once. The guard refuses non-green starts and stops
its process group at yellow/red pressure, unreadable telemetry or disk below 10 GB.
Never override a resource stop, run outside the guard, or raise OS GPU limits to
force a result. Download models only through the dry-run-then-download workflow.
No credentials, paid services or cloud inference are needed for this experiment.

## 5. Limits and verification

This is genuine neural reference-to-video, not audio-driven lip-sync. It cannot
synchronize mouth shapes to narration, promise identity consistency, or turn this
short benchmark into a finished presenter episode. Keep references original and
fictional; disclose AI use. Nothing was approved or published.

The [upstream Wan guide](https://github.com/leejet/stable-diffusion.cpp/blob/master/docs/wan.md)
distinguishes plain T2V 1.3B from VACE reference-to-video. The
[tiny-codec guide](https://github.com/leejet/stable-diffusion.cpp/blob/master/docs/taesd.md)
documents Wan support. Cache revisions and checksums are pinned in
`scripts/generate_wan_video.py`; manifest selections live in
`scripts/setup/models.manifest.yaml`.

Offline verification: **678 passed, 3 skipped**, Ruff and shell syntax checks passed.
Tests cover unknown/optional model selection, partial-cache handling, missing or
corrupt full/tiny weights, offline guard settings and bounded native arguments.
No tests load models, access real credentials or make network requests. No core
pipeline stage, approval gate, publishing setting or brand default changed.
