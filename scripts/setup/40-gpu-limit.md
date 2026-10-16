# Optional: raise the GPU wired-memory limit (you run this yourself)

Source: docs/4-production/14-local-models-mac-m4.md §2 and §10. The setup scripts never run
this. It needs `sudo`, so run it yourself if you want it.

## Why

By default macOS lets the GPU wire roughly 2/3–3/4 of RAM. On a 24 GB Mac that is about
16–18 GB. Apple doesn't document the exact fraction. Raising the limit to about 19 GB lets
gpt-oss-20b (~12–13 GB) or Z-Image-Turbo (~10 GB) run with room left for the KV cache and
MLX buffers. If a model exceeds the limit it swaps or slows to a crawl, and MPS throws
`MPS backend out of memory`.

## Check the current value (read-only, no sudo)

```bash
sysctl iogpu.wired_limit_mb      # 0 = system default
```

`make preflight` prints this too.

## Raise it (until the next reboot)

```bash
sudo sysctl iogpu.wired_limit_mb=19456     # ~19 GB on a 24 GB machine
sysctl iogpu.wired_limit_mb                # confirm
```

- **Don't go above about 20480 (20 GB)** on a 24 GB Mac. macOS needs ≥ 4–5 GB, and a higher
  value can freeze or kernel-panic the machine under memory pressure.
- Even with the limit raised, keep the model budget at **≤ 14 GB** and run **one heavy model
  at a time**: `ollama stop <model>` before the image stage.
- Close heavy apps (browsers, Docker) before an overnight batch.

## Revert

```bash
sudo sysctl iogpu.wired_limit_mb=0         # back to the system default
```

A reboot also reverts it, because the setting is not persistent.

## Make it persistent (optional, not recommended to start)

Keep it manual until the week-1 smoke test shows it's needed. If you do want it at boot,
a LaunchDaemon that runs the `sysctl` command is the usual approach (see
https://github.com/ivanopcode/devnote-override-macos-metal-vram-cap). To undo that, remove
the plist with `sudo launchctl bootout` and set the value back to `0`.

## Verify it helped

```bash
make ollama                                # in another terminal
ollama run gpt-oss:20b --verbose "hello"   # note eval rate (tokens/s)
ollama ps                                  # SIZE and PROCESSOR (should be 100% GPU)
```

Compare tokens/s before and after. If `ollama ps` shows a CPU/GPU split, the model doesn't fit.
