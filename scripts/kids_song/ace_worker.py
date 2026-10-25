"""Worker that runs INSIDE the ACE-Step venv (Python 3.12), never in the studio venv.

  <ace>/.venv/bin/python -I scripts/kids_song/ace_worker.py sing JOB.json
  <ace>/.venv/bin/python -I scripts/kids_song/ace_worker.py analyze JOB.json

Launched by scripts/kids_song/sing.py through scripts/run_guarded.py, so memory pressure
is sampled every second. It imports nothing from the studio package. Every model loads
from local files: HF_HUB_OFFLINE=1 and the ACE-Step auto-download hooks are replaced by
a refusal, so a missing checkpoint fails instead of fetching gigabytes.

sing    : one ACE-Step 1.5 turbo take (8 steps, 0.6B 5Hz LM on MLX), written as WAV, with
          wall time, peak RSS and the MPS driver allocation in the result JSON.
analyze : for each take, Demucs htdemucs vocal stem, wav2vec2-base-960h CTC log-posteriors
          (.npz, aligned later by align.py in the studio venv), LUFS, sample peak and the
          vocal energy share, plus a spectrogram PNG of the mix and the vocal stem.
"""

from __future__ import annotations

import json
import os
import resource
import sys
import time
from pathlib import Path

LM_SMALL = "acestep-5Hz-lm-0.6B"
DIT = "acestep-v15-turbo"


def _peak_rss_mb() -> float:
    # ru_maxrss is bytes on macOS, KiB on Linux.
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(raw / (1024 * 1024 if sys.platform == "darwin" else 1024), 1)


class _PeakSampler:
    """Track the peak MPS driver allocation (Metal memory is not all visible in RSS)."""

    def __init__(self, torch, every_s: float = 0.5):
        import threading
        self._torch, self._every = torch, every_s
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.mps_mb = None

    def _read(self):
        t = self._torch
        if hasattr(t, "mps") and t.backends.mps.is_available():
            mb = round(t.mps.driver_allocated_memory() / 2**20, 1)
            self.mps_mb = mb if self.mps_mb is None else max(self.mps_mb, mb)

    def _run(self):
        last = None
        n = 0
        while not self._stop.wait(self._every):
            self._read()
            n += 1
            if n % 2 == 0:  # free-memory trace every ~1 s, printed on >=3 point moves
                pct = _free_pct()
                if pct is not None and (last is None or abs(pct - last) >= 3):
                    print(f"[worker] trace: free {pct}%, mps {self.mps_mb} MB peak",
                          flush=True)
                    last = pct

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=2)
        self._read()


def _mlx_peak_mb():
    try:
        import mlx.core as mx
        fn = getattr(mx, "get_peak_memory", None) or mx.metal.get_peak_memory
        return round(fn() / 2**20, 1)
    except Exception:  # noqa: BLE001 - informational only
        return None


def _free_pct() -> int | None:
    import subprocess
    try:
        out = subprocess.run(["/usr/bin/memory_pressure", "-Q"], capture_output=True,
                             text=True, timeout=10, check=False).stdout.strip().splitlines()
        return int(out[-1].rsplit(":", 1)[1].strip().rstrip("%"))
    except (OSError, subprocess.SubprocessError, IndexError, ValueError):
        return None


def _mem(tag: str) -> None:
    """Progress line with system free memory, to see where a load peaks."""
    import subprocess
    try:
        out = subprocess.run(["/usr/bin/memory_pressure", "-Q"], capture_output=True,
                             text=True, timeout=10, check=False).stdout.strip().splitlines()
        pct = out[-1].rsplit(":", 1)[1].strip() if out else "?"
    except (OSError, subprocess.SubprocessError, IndexError):
        pct = "?"
    print(f"[worker] {tag}: free {pct}, rss {_peak_rss_mb()} MB", flush=True)


_ST_DTYPES = {"BF16": "bfloat16", "F16": "float16", "F32": "float32", "F64": "float64",
              "I64": "int64", "I32": "int32", "I16": "int16", "I8": "int8", "U8": "uint8",
              "BOOL": "bool"}


def _nocache_tensors(file: Path):
    """Yield (name, cpu tensor) from a .safetensors file, read with F_NOCACHE so the 4-5 GB
    checkpoint does not also sit in the page cache while it is copied to MPS."""
    import fcntl
    import struct

    import torch

    fd = os.open(file, os.O_RDONLY)
    try:
        if hasattr(fcntl, "F_NOCACHE"):
            fcntl.fcntl(fd, fcntl.F_NOCACHE, 1)
        (n,) = struct.unpack("<Q", os.pread(fd, 8, 0))
        header = json.loads(os.pread(fd, n, 8))
        header.pop("__metadata__", None)
        base = 8 + n
        for name, meta in header.items():
            start, end = meta["data_offsets"]
            buf = bytearray(end - start)
            view = memoryview(buf)
            got = 0
            while got < len(buf):
                chunk = os.preadv(fd, [view[got:]], base + start + got)
                if chunk <= 0:
                    raise OSError(f"short read in {file} at {name}")
                got += chunk
            dtype = getattr(torch, _ST_DTYPES[meta["dtype"]])
            t = (torch.frombuffer(buf, dtype=dtype) if buf else torch.empty(0, dtype=dtype))
            yield name, t.reshape(meta["shape"])
            del t, view, buf
    finally:
        os.close(fd)


def _refuse_downloads() -> None:
    """Only the 0.6B LM is installed, and nothing may be fetched at run time."""
    import acestep.model_downloader as md
    from acestep.core.generation.handler import init_service_downloads as isd

    # The main-model check also wants the 1.7B LM folder, which we deliberately skip.
    md.MAIN_MODEL_COMPONENTS[:] = [c for c in md.MAIN_MODEL_COMPONENTS if "lm-1.7B" not in c]

    def refuse(*_a, **_k):
        return False, "model download disabled in the studio worker (install offline first)"

    for name in ("ensure_main_model", "ensure_dit_model", "ensure_vae_model", "ensure_lm_model"):
        for mod in (md, isd):
            if hasattr(mod, name):
                setattr(mod, name, refuse)


def sing(job: dict) -> dict:
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    root = Path(job["ace_root"])
    ckpt = root / "checkpoints"
    os.environ["ACESTEP_CHECKPOINTS_DIR"] = str(ckpt)
    t0 = time.time()
    _refuse_downloads()
    import torch
    from acestep.handler import AceStepHandler
    from acestep.inference import GenerationConfig, GenerationParams, generate_music
    from acestep.llm_inference import LLMHandler

    lean_dtype = getattr(torch, job.get("dtype", "bfloat16"))

    import transformers

    original = transformers.AutoModel.from_pretrained.__func__

    def streamed_from_pretrained(cls, path, *args, **kw):
        """Meta-init the model, then stream bf16 tensors from the safetensors mmap straight
        to MPS one at a time. Upstream materialises a full CPU copy first, which pushed
        macOS memory pressure to yellow within seconds even with 17 GB free."""
        file = Path(path) / "model.safetensors"
        if not (file.exists() and torch.backends.mps.is_available()):
            return original(cls, path, *args, **kw)
        from accelerate import init_empty_weights
        from accelerate.utils import set_module_tensor_to_device

        dtype = kw.get("dtype") or kw.get("torch_dtype") or lean_dtype
        config = transformers.AutoConfig.from_pretrained(
            path, trust_remote_code=kw.get("trust_remote_code", False))
        with init_empty_weights():  # buffers stay real (include_buffers=False)
            model = cls.from_config(config, trust_remote_code=kw.get("trust_remote_code", False),
                                    attn_implementation=kw.get("attn_implementation"),
                                    torch_dtype=dtype)
        for name, t in _nocache_tensors(file):
            set_module_tensor_to_device(model, name, "mps",
                                        value=t.to(dtype) if t.is_floating_point() else t)
            del t
        torch.mps.synchronize()
        torch.mps.empty_cache()
        tie = getattr(model, "tie_weights", None)
        if tie is not None:
            tie()
        missing = [n for n, p in model.named_parameters() if p.is_meta]
        if missing:
            raise RuntimeError(f"streamed load left meta params: {missing[:5]}")
        for n, b in list(model.named_buffers()):
            if b.is_meta:
                raise RuntimeError(f"streamed load left meta buffer: {n}")
        _mem(f"streamed {Path(path).name} (mps current "
             f"{torch.mps.current_allocated_memory() / 2**30:.2f} GiB, driver "
             f"{torch.mps.driver_allocated_memory() / 2**30:.2f} GiB)")
        return model.eval()

    class LeanHandler(AceStepHandler):
        """Upstream loads the bf16 checkpoint as fp32 on MPS (about 9.6 GB) and then makes
        a second fp32 MLX copy of the decoder. That does not fit a 24 GB Mac next to other
        work, so keep the checkpoint's native bf16 and leave the MLX DiT copy off."""

        def _load_main_model_from_checkpoint(self, **kw):
            if str(self.device).startswith("mps"):
                self.dtype = lean_dtype
            return super()._load_main_model_from_checkpoint(**kw)

    peak = _PeakSampler(torch)
    peak.start()
    _mem("start")
    dit = LeanHandler()
    if job.get("stream_load", True):
        transformers.AutoModel.from_pretrained = classmethod(streamed_from_pretrained)
    try:
        msg, ok = dit.initialize_service(project_root=str(root), config_path=DIT,
                                         device="auto",
                                         use_mlx_dit=bool(job.get("mlx_dit", False)))
    finally:
        transformers.AutoModel.from_pretrained = classmethod(original)
    if not ok:
        raise SystemExit(f"DiT init failed: {msg}")
    _mem("dit+vae+text encoder loaded")
    thinking = bool(job.get("thinking", False))
    llm = None  # no-LM path: DiT turbo + text encoder only (~6.3 GB)
    if thinking:
        llm = LLMHandler()
        msg, ok = llm.initialize(checkpoint_dir=str(ckpt), lm_model_path=LM_SMALL,
                                 backend=job.get("lm_backend", "mlx"), device="auto")
        if not ok:
            raise SystemExit(f"LM init failed: {msg}")
        import mlx.core as mx
        mx.set_cache_limit(256 * 2**20)  # bound unused MLX buffers (LM KV cache etc.)
        lm_generate = llm.generate_with_stop_condition

        def lm_then_release(*a, **k):
            out = lm_generate(*a, **k)
            mx.clear_cache()
            torch.mps.empty_cache()
            _mem("lm codes done, caches cleared")
            return out

        llm.generate_with_stop_condition = lm_then_release
    t_loaded = time.time()
    _mem("lm loaded")

    params = GenerationParams(
        task_type="text2music",
        caption=job["caption"],
        lyrics=job["lyrics"],
        instrumental=False,
        vocal_language=job.get("vocal_language", "en"),
        bpm=int(job["bpm"]),
        keyscale=job["key"],
        timesignature=str(job.get("timesignature", "4")),
        duration=float(job["duration"]),
        inference_steps=int(job.get("steps", 8)),
        seed=int(job["seed"]),
        shift=float(job.get("shift", 3.0)),  # turbo recommendation (docs/en/INFERENCE.md)
        thinking=thinking,  # True: the 5Hz LM writes semantic codes (LM-DiT path)
        use_cot_caption=False,  # keep our caption verbatim (British vocal, instruments)
        use_cot_language=False,  # we pin vocal_language
        use_cot_metas=False,  # bpm, key, time signature and duration are all given
    )
    config = GenerationConfig(batch_size=1, use_random_seed=False, seeds=[int(job["seed"])],
                              audio_format="wav")
    out = Path(job["out_wav"])
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.parent / f".{out.stem}.tmp"
    tmp.mkdir(exist_ok=True)
    result = generate_music(dit, llm, params, config, save_dir=str(tmp))
    t_done = time.time()
    _mem("generated")
    if not result.success or not result.audios:
        raise SystemExit(f"generation failed: {result.error or result.status_message}")
    src = Path(result.audios[0]["path"])
    if not src.exists():
        import soundfile as sf
        audio = result.audios[0]
        sf.write(out, audio["tensor"].numpy().T, int(audio["sample_rate"]))
    else:
        src.replace(out)
    for leftover in tmp.iterdir():
        leftover.unlink()
    tmp.rmdir()
    peak.stop()
    costs = (result.extra_outputs or {}).get("time_costs", {})
    return {
        "seed": int(job["seed"]), "out_wav": str(out),
        "load_s": round(t_loaded - t0, 2), "generate_s": round(t_done - t_loaded, 2),
        "wall_s": round(t_done - t0, 2), "peak_rss_mb": _peak_rss_mb(),
        "peak_mps_driver_mb": peak.mps_mb, "peak_mlx_mb": _mlx_peak_mb(),
        "thinking": thinking, "dtype": str(dit.dtype), "mlx_dit": bool(getattr(dit, "use_mlx_dit", False)),
        "time_costs": {k: round(float(v), 2) for k, v in costs.items()
                       if isinstance(v, int | float)},
    }


def _spectrogram(mix, vocals, sr: int, png: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(2, 1, figsize=(12, 6), sharex=True)
    for ax, sig, title in ((axes[0], mix, "mix"), (axes[1], vocals, "vocal stem (htdemucs)")):
        ax.specgram(sig, NFFT=2048, Fs=sr, noverlap=1536, cmap="magma",
                    vmin=-120, scale="dB")
        ax.set_ylim(0, 8000)
        ax.set_ylabel("Hz")
        ax.set_title(title)
    axes[1].set_xlabel("seconds")
    fig.tight_layout()
    fig.savefig(png, dpi=80)
    plt.close(fig)


def analyze(job: dict) -> dict:
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    os.environ["TORCH_HOME"] = job["torch_home"]  # htdemucs weights, pre-fetched
    import numpy as np
    import pyloudnorm
    import soundfile as sf
    import torch
    import torchaudio.functional as AF
    from demucs.apply import apply_model
    from demucs.pretrained import get_model
    from transformers import Wav2Vec2ForCTC

    torch.set_num_threads(int(os.environ.get("OMP_NUM_THREADS", "3")))
    t0 = time.time()
    sep = get_model("htdemucs").eval()
    w2v_dir = job["wav2vec2_dir"]
    w2v = Wav2Vec2ForCTC.from_pretrained(w2v_dir, local_files_only=True).eval()
    vocab = json.loads(Path(w2v_dir, "vocab.json").read_text())
    dev = "mps" if torch.backends.mps.is_available() else "cpu"
    results = []
    for take in job["takes"]:
        wav = Path(take["wav"])
        audio, sr = sf.read(wav, dtype="float32", always_2d=True)  # (n, ch)
        peak = float(np.max(np.abs(audio)))
        clipped = float(np.mean(np.abs(audio) >= 0.999))
        lufs = float(pyloudnorm.Meter(sr).integrated_loudness(audio))
        x = torch.from_numpy(audio.T.copy())
        if x.shape[0] == 1:
            x = x.repeat(2, 1)
        x44 = AF.resample(x, sr, sep.samplerate)
        ref = x44.mean(0)
        mean, std = ref.mean(), ref.std() + 1e-8
        with torch.no_grad():
            stems = apply_model(sep, ((x44 - mean) / std)[None], device=dev, split=True,
                                overlap=0.25, progress=False)[0]
        stems = stems * std + mean
        vocals = stems[sep.sources.index("vocals")]  # (2, n)
        vpath = wav.with_name(wav.stem + ".vocals.wav")
        sf.write(vpath, vocals.T.numpy(), sep.samplerate)
        vmono = vocals.mean(0)
        mix_mono = x44.mean(0)
        frame = sep.samplerate // 10
        n = (vmono.numel() // frame) * frame
        v_rms = vmono[:n].reshape(-1, frame).pow(2).mean(1).sqrt()
        m_rms = mix_mono[:n].reshape(-1, frame).pow(2).mean(1).sqrt()
        active = float((20 * torch.log10(v_rms + 1e-9) > -40).float().mean())
        share = float(vmono.pow(2).sum() / (mix_mono.pow(2).sum() + 1e-9))
        v16 = AF.resample(vmono[None], sep.samplerate, 16000)[0]
        v16 = (v16 - v16.mean()) / (v16.std() + 1e-7)
        with torch.no_grad():
            logits = w2v(v16[None]).logits[0]
        logp = torch.log_softmax(logits.float(), -1).numpy()
        npz = wav.with_name(wav.stem + ".ctc.npz")
        np.savez_compressed(npz, logp=logp.astype(np.float32),
                            vocab=json.dumps(vocab), frame_s=320 / 16000)
        png = wav.with_name(wav.stem + ".spectrogram.png")
        _spectrogram(mix_mono.numpy(), vmono.numpy(), sep.samplerate, png)
        results.append({
            "wav": str(wav), "vocals_wav": str(vpath), "ctc_npz": str(npz),
            "spectrogram_png": str(png), "duration_s": round(audio.shape[0] / sr, 2),
            "sample_rate": sr, "lufs": round(lufs, 2), "peak_dbfs":
                round(20 * float(np.log10(peak + 1e-12)), 2),
            "clipped_fraction": clipped, "vocal_energy_share": round(share, 4),
            "vocal_active_fraction": round(active, 3),
            "mix_rms_db_median": round(float(20 * torch.log10(m_rms.median() + 1e-9)), 1),
        })
    return {"takes": results, "wall_s": round(time.time() - t0, 2),
            "peak_rss_mb": _peak_rss_mb(), "device": dev}


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] not in ("sing", "analyze"):
        print(__doc__, file=sys.stderr)
        return 2
    job = json.loads(Path(argv[1]).read_text())
    out = sing(job) if argv[0] == "sing" else analyze(job)
    Path(job["result_json"]).write_text(json.dumps(out, indent=2))
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
