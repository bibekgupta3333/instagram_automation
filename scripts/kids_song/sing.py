"""ACE-Step 1.5 sung vocals for a nursery-song episode, run from the studio venv.

  uv run --no-sync python -m scripts.kids_song.sing \
      --episode docs/2-channels/27-plinkety-pond-episode-01.json \
      --out output/kids/plinkety-pond-ep01/song [--takes 6] [--duration 60] [--dry-run]

ACE-Step lives in its own Python 3.12 venv (output/runtimes/ace-step/repo/.venv, see
INSTALL.json next to it). Each take is one guarded child process
(scripts/run_guarded.py: pressure sampled every second, 3 CPU threads, offline), so the
models unload between takes and the known auto-scoring leak (#1081) never applies; this
wrapper never calls scoring. PYTORCH_MPS_HIGH_WATERMARK_RATIO=0.0 is set only on that
guarded child. If another guarded job holds output/.guarded-run.lock, we wait and retry.

Then one guarded `analyze` pass separates vocals (htdemucs), computes wav2vec2 CTC
posteriors, LUFS and peak, and align.py force-aligns the known lyrics. The take with the
best alignment score that also has sane loudness and no clipping becomes best.wav, with
words.json (word timings) and takes.json (all takes). Nothing is approved or published.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np

from . import align

REPO = Path(__file__).resolve().parents[2]
ACE_HOME = REPO / "output" / "runtimes" / "ace-step"
ACE_ROOT = ACE_HOME / "repo"
ACE_PY = ACE_ROOT / ".venv" / "bin" / "python"
WORKER = Path(__file__).resolve().with_name("ace_worker.py")
LOCK = REPO / "output" / ".guarded-run.lock"
GUARD_BUSY = 75  # run_guarded.py exit code for lock busy / non-green start / stop

CAPTION = ("nursery rhyme, children's song for toddlers, warm bright clear female vocal, "
           "slow clear diction, British English, ukulele, glockenspiel, soft claps, gentle, "
           "bouncy, happy, 96 bpm, C major")

# Song structure for "Bubbles on My Tummy" (doc 27 §Lyrics), as indices into lyrics_lines.
BUBBLES_LAYOUT: list[tuple[str, list[int]]] = [
    ("Intro", [0]), ("Verse", [1, 2]), ("Chorus", [3, 4]), ("Verse", [5, 6]),
    ("Chorus", [7, 8]), ("Verse", [9, 10]), ("Outro", [11]),
]
CALL_RESPONSE = {0, 5}  # "Is it bath time? (Yes, it's bath time!)", "Who's got bubbles? (Me...)"

Line = str | tuple[str, str]


def split_call_response(line: str) -> tuple[str, str]:
    call, _, response = line.partition("? ")
    return f"{call}?", response.strip()


def sections_from_lines(lines: list[str], layout=BUBBLES_LAYOUT,
                        call_response=CALL_RESPONSE) -> list[tuple[str, list[Line]]]:
    out: list[tuple[str, list[Line]]] = []
    for tag, idx in layout:
        body: list[Line] = []
        for i in idx:
            body.append(split_call_response(lines[i]) if i in call_response else lines[i])
        out.append((tag, body))
    return out


def format_lyrics(sections: list[tuple[str, list[Line]]]) -> str:
    """ACE-Step lyric text: a [Tag] line per section, call-and-response in parentheses."""
    blocks = []
    for tag, body in sections:
        rows = [f"[{tag}]"]
        for line in body:
            rows.append(f"{line[0]} ({line[1]})" if isinstance(line, tuple) else line)
        blocks.append("\n".join(rows))
    return "\n\n".join(blocks) + "\n"


def sung_text(lyrics: str) -> str:
    """Words as sung, for alignment: drop tags, keep parenthesised responses."""
    rows = [r for r in lyrics.splitlines() if r.strip() and not r.strip().startswith("[")]
    return " ".join(rows).replace("(", " ").replace(")", " ")


def worker_argv(mode: str, job_json: Path) -> list[str]:
    return [str(ACE_PY), "-I", str(WORKER), mode, str(job_json)]


def guarded_argv(cmd: list[str], log: Path, threads: int = 3) -> list[str]:
    return [sys.executable, str(REPO / "scripts" / "run_guarded.py"), "--threads",
            str(threads), "--log", str(log), "--", *cmd]


def guard_env(base: dict | None = None) -> dict:
    env = dict(os.environ if base is None else base)
    env["PYTORCH_MPS_HIGH_WATERMARK_RATIO"] = "0.0"  # only ever on the guarded child
    env["HF_HUB_OFFLINE"] = "1"
    env["TRANSFORMERS_OFFLINE"] = "1"
    env.pop("PYTHONPATH", None)
    return env


def slot_free(lock: Path = LOCK) -> bool:
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    finally:
        os.close(fd)


def free_memory_pct() -> int | None:
    """macOS 'System-wide memory free percentage' (read-only `memory_pressure` query)."""
    try:
        out = subprocess.run(["/usr/bin/memory_pressure", "-Q"], capture_output=True,
                             text=True, timeout=10, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for row in out.splitlines():
        if "free percentage" in row:
            return int(row.rsplit(":", 1)[1].strip().rstrip("%"))
    return None


MIN_FREE_PCT = 50  # extra margin on top of a green pressure reading
HEAVY = ("mflux", "sd-cli", "ace_worker.py")  # other local GPU jobs we never overlap


def pressure_green() -> bool:
    try:
        out = subprocess.run(["/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
                             capture_output=True, text=True, timeout=3, check=False).stdout
        return out.strip() == "1"
    except (OSError, subprocess.SubprocessError):
        return False


def heavy_running() -> bool:
    for name in HEAVY:
        r = subprocess.run(["/usr/bin/pgrep", "-f", name], capture_output=True, check=False)
        if r.returncode == 0:
            return True
    return False


def ready(lock: Path = LOCK, checks: int = 4, gap_s: float = 10,
          sleep: Callable = time.sleep, mem: Callable[[], int | None] = free_memory_pct,
          green: Callable[[], bool] = pressure_green,
          heavy: Callable[[], bool] = heavy_running) -> bool:
    """Slot free, no other heavy job, pressure green and memory free for 30 s (4 checks)."""
    for i in range(checks):
        pct = mem()
        if (not slot_free(lock) or heavy() or not green() or pct is None
                or pct < MIN_FREE_PCT):
            return False
        if i < checks - 1:
            sleep(gap_s)
    return True


class PressureStop(RuntimeError):
    """The guard stopped the job (pressure left green or disk floor). Not retried."""


def run_guarded(cmd: list[str], log: Path, *, runner: Callable = subprocess.run,
                is_free: Callable[[], bool] = ready, sleep: Callable = time.sleep,
                wait_s: float = 20, max_wait_s: float = 6 * 3600, retries: int = 6) -> None:
    """Run cmd under the guard. Waits for a quiet machine; retries only a lost lock race.

    A resource stop raises PressureStop at once (no loop), so the caller reports numbers.
    """
    waited = 0.0
    attempt = 0
    while True:
        while not is_free():
            if waited >= max_wait_s:
                raise RuntimeError("Guarded slot or memory never became quiet; giving up")
            sleep(wait_s)
            waited += wait_s
        result = runner(guarded_argv(cmd, log), cwd=REPO, env=guard_env(), check=False,
                        stderr=subprocess.PIPE, text=True)
        err = result.stderr or ""
        if err:
            sys.stderr.write(err)
        if result.returncode == 0:
            return
        attempt += 1
        if result.returncode == GUARD_BUSY and "Another guarded job" in err and attempt <= retries:
            sleep(wait_s)
            waited += wait_s
            continue
        if result.returncode == GUARD_BUSY or "Resource guard" in err:
            raise PressureStop(err.strip().splitlines()[-1] if err.strip() else "guard stop")
        raise RuntimeError(f"Guarded step failed (exit {result.returncode}): {cmd[2:4]}")


def pressure_summary(log: Path, since: float = 0.0) -> dict:
    if not log.exists():
        return {"samples": 0}
    rows = [json.loads(r) for r in log.read_text().splitlines() if r.strip()]
    rows = [r for r in rows if r.get("time", 0) >= since]
    levels = [r.get("pressure") for r in rows]
    free = [r["free_gb"] for r in rows if r.get("free_gb") is not None]
    names = {1: "green", 2: "yellow", 4: "red", None: "error"}
    counts: dict[str, int] = {}
    for lv in levels:
        counts[names.get(lv, str(lv))] = counts.get(names.get(lv, str(lv)), 0) + 1
    return {"samples": len(rows), "levels": counts, "min_free_gb": min(free) if free else None}


def sing(lyrics: str, caption: str, bpm: int, key: str, duration: float, seed: int,
         out_wav: Path, *, thinking: bool = False, runner: Callable = subprocess.run,
         **guard_kw) -> dict:
    """Generate one ACE-Step take into out_wav; returns timing and memory figures."""
    out_wav = Path(out_wav)
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    job = {
        "ace_root": str(ACE_ROOT), "caption": caption, "lyrics": lyrics, "bpm": int(bpm),
        "key": key, "timesignature": "4", "duration": float(duration), "seed": int(seed),
        "steps": 8, "shift": 3.0, "vocal_language": "en", "lm_backend": "mlx",
        "dtype": "bfloat16", "mlx_dit": False,  # lean load, see ace_worker.LeanHandler
        "thinking": bool(thinking),  # False: no 5Hz LM, DiT turbo + text encoder only
        "out_wav": str(out_wav), "result_json": str(out_wav.with_suffix(".result.json")),
    }
    job_json = out_wav.with_suffix(".job.json")
    job_json.write_text(json.dumps(job, indent=2))
    log = out_wav.with_suffix(".pressure.jsonl")
    start = time.time()
    run_guarded(worker_argv("sing", job_json), log, runner=runner, **guard_kw)
    result = json.loads(Path(job["result_json"]).read_text())
    result["guard_wall_s"] = round(time.time() - start, 2)
    result["pressure"] = pressure_summary(log, since=start)
    return result


def ace_key(key: str) -> str:
    note, _, mode = key.partition(" ")
    return f"{note} {mode.capitalize() or 'Major'}"


def eligible(a: dict) -> bool:
    return (a["clipped_fraction"] < 1e-4 and a["peak_dbfs"] <= -0.1
            and -24.0 <= a["lufs"] <= -8.0 and a["vocal_active_fraction"] >= 0.2)


def select_best(takes: list[dict]) -> dict:
    ok = [t for t in takes if t.get("alignment", {}).get("ok") and eligible(t["analysis"])
          and align.monotonic(t["alignment"]["words"])]
    pool = ok or [t for t in takes if t.get("alignment", {}).get("ok")] or takes
    return max(pool, key=lambda t: (t.get("alignment", {}).get("score", 0.0),
                                    t.get("alignment", {}).get("confident_fraction", 0.0)))


def word_lines(words: list[dict], lines: list[str]) -> list[dict]:
    out, i = [], 0
    for text in lines:
        n = len(align.normalise_words(text))
        chunk = words[i:i + n]
        i += n
        if chunk:
            out.append({"text": text, "start": chunk[0]["start"], "end": chunk[-1]["end"]})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--episode", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--takes", type=int, default=4)
    ap.add_argument("--thinking", action="store_true",
                    help="use the 0.6B 5Hz LM for semantic codes (more memory)")
    ap.add_argument("--seeds", type=str, default="")
    ap.add_argument("--duration", type=float, default=60.0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    ep = json.loads(args.episode.read_text())
    lines = ep["lyrics_lines"]
    lyrics = format_lyrics(sections_from_lines(lines))
    seeds = ([int(s) for s in args.seeds.split(",")] if args.seeds
             else [1101 + 7919 * i for i in range(args.takes)])
    out = args.out
    takes_dir = out / "takes"
    if args.dry_run:
        print(lyrics)
        print(json.dumps({"seeds": seeds, "caption": CAPTION, "bpm": ep["bpm"],
                          "key": ace_key(ep["key"]), "argv": worker_argv("sing", Path("J"))},
                         indent=2))
        return 0
    if not ACE_PY.exists():
        raise SystemExit(f"ACE-Step venv missing: {ACE_PY} (see INSTALL.json)")
    takes: list[dict] = []
    for n, seed in enumerate(seeds, 1):
        wav = takes_dir / f"take{n:02d}-seed{seed}.wav"
        res_json = wav.with_suffix(".result.json")
        if wav.exists() and res_json.exists():
            gen = json.loads(res_json.read_text())  # idempotent re-run
            gen.setdefault("pressure", pressure_summary(wav.with_suffix(".pressure.jsonl")))
        else:
            print(f"[sing] take {n}/{len(seeds)} seed {seed}", flush=True)
            log = wav.with_suffix(".pressure.jsonl")
            start = time.time()
            try:
                gen = sing(lyrics, CAPTION, ep["bpm"], ace_key(ep["key"]), args.duration, seed,
                           wav, thinking=args.thinking)
            except PressureStop as exc:
                report = {"stopped_take": n, "seed": seed, "reason": str(exc),
                          "pressure": pressure_summary(log, since=start),
                          "finished_takes": [t["take"] for t in takes]}
                (out / "stopped.json").write_text(json.dumps(report, indent=2))
                print(f"[sing] STOPPED by resource guard: {json.dumps(report)}", flush=True)
                return 3
            res_json.write_text(json.dumps(gen, indent=2))
        takes.append({"take": n, "seed": seed, "wav": str(wav), "generation": gen})

    analyze_job = takes_dir / "analyze.job.json"
    analyze_res = takes_dir / "analyze.result.json"
    want = sorted(t["wav"] for t in takes)
    have = (sorted(a["wav"] for a in json.loads(analyze_res.read_text())["takes"])
            if analyze_res.exists() else [])
    if have != want:
        analyze_job.write_text(json.dumps({
            "takes": [{"wav": t["wav"]} for t in takes], "result_json": str(analyze_res),
            "torch_home": str(ACE_HOME / "models" / "torch"),
            "wav2vec2_dir": str(ACE_HOME / "models" / "wav2vec2-base-960h"),
        }, indent=2))
        start = time.time()
        try:
            run_guarded(worker_argv("analyze", analyze_job),
                        takes_dir / "analyze.pressure.jsonl")
        except PressureStop as exc:
            print(f"[sing] analyze STOPPED by resource guard: {exc}", flush=True)
            return 3
        print(f"[sing] analyze done in {time.time() - start:.0f} s", flush=True)
    analysis = json.loads(analyze_res.read_text())
    by_wav = {a["wav"]: a for a in analysis["takes"]}
    text = sung_text(lyrics)
    for t in takes:
        a = by_wav[t["wav"]]
        npz = np.load(a["ctc_npz"])
        res = align.align(npz["logp"], json.loads(str(npz["vocab"])), text,
                          float(npz["frame_s"]))
        t["analysis"] = a
        t["alignment"] = {k: v for k, v in res.items() if k != "words"}
        t["alignment"]["words"] = res["words"]
        t["eligible"] = eligible(a)
    best = select_best(takes)
    shutil.copyfile(best["wav"], out / "best.wav")
    shutil.copyfile(best["analysis"]["vocals_wav"], out / "best.vocals.wav")
    shutil.copyfile(best["analysis"]["spectrogram_png"], out / "best.spectrogram.png")
    (out / "words.json").write_text(json.dumps({
        "source": "wav2vec2-base-960h CTC forced alignment on htdemucs vocal stem",
        "take": best["take"], "seed": best["seed"], "score": best["alignment"]["score"],
        "confident_threshold": align.CONFIDENT, "words": best["alignment"]["words"],
        "lines": word_lines(best["alignment"]["words"], lines),
    }, indent=2))
    summary = [{**{k: v for k, v in t.items() if k != "alignment"},
                "alignment": {k: v for k, v in t["alignment"].items() if k != "words"}}
               for t in takes]
    (out / "takes.json").write_text(json.dumps({
        "title": ep["title"], "caption": CAPTION, "lyrics": lyrics, "bpm": ep["bpm"],
        "key": ace_key(ep["key"]), "best_take": best["take"], "takes": summary,
        "analyze": {k: v for k, v in analysis.items() if k != "takes"},
        "note": "Local draft audio only. Never approved or published by this tool.",
    }, indent=2))
    for t in takes:
        al, an = t["alignment"], t["analysis"]
        print(f"take {t['take']} seed {t['seed']}: score {al['score']:.3f} "
              f"conf {al['confident_fraction']:.0%} LUFS {an['lufs']} peak {an['peak_dbfs']} "
              f"vocal-active {an['vocal_active_fraction']:.0%} eligible {t['eligible']}")
    print(f"best: take {best['take']} -> {out / 'best.wav'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
