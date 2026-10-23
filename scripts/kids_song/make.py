"""Nursery-song episode JSON -> ~60 s 9:16 + 16:9 video, entirely local.

  uv run --no-sync python -m scripts.kids_song.make --episode EP.json --out output/kids/SLUG \
      [--dry-run] [--mock-clips] [--mock-vocal] [--vocal sung|chant] [--shots 0,1] [--clips-only]

Heavy steps run in guarded child processes (scripts/run_guarded.py): Kokoro once for all
lyric lines, then FastWan one shot at a time. Everything is cached under --out, so re-runs
skip finished work. Output is never approved or published (made_for_kids: true).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path

import numpy as np

from . import assemble, clips, episode, music, vocal

REPO = Path(__file__).resolve().parents[2]
VERSION = 1


def _digest(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:12]


def scaled(fmt: clips.Format, scale: float) -> clips.Format:
    if scale == 1:
        return fmt
    even = lambda v: max(16, round(v * scale / 2) * 2)
    return replace(fmt, width=even(fmt.width), height=even(fmt.height), bitrate="2M")


def plan_summary(tl: episode.Timeline, plan: list[dict]) -> dict:
    return {
        "title": tl.title, "slug": tl.slug, "bpm": tl.bpm, "key": tl.key,
        "beats_per_bar": tl.beats_per_bar, "total_beats": tl.total_beats,
        "seconds": round(tl.total_sec, 3), "chords": tl.chords,
        "lines": [{"text": ln.text, "start": round(ln.start, 3), "end": round(ln.end, 3)}
                  for ln in tl.lines],
        "shots": [{"shot": s.index, "start": round(s.start, 3), "end": round(s.end, 3),
                   "seconds": round(s.dur, 3), "fastwan_frames": clips.frames_for(s.dur),
                   "seed": s.seed, "prompt": clips.full_prompt(s), "edit": p}
                  for s, p in zip(tl.shots, plan, strict=True)],
    }


def guarded(cmd: list[str]) -> None:
    full = [sys.executable, str(REPO / "scripts" / "run_guarded.py"), "--", *cmd]
    result = subprocess.run(full, cwd=REPO, check=False)
    if result.returncode:
        raise SystemExit(f"Guarded step failed (exit {result.returncode}): {' '.join(cmd[:4])}")


def build_audio(tl: episode.Timeline, out: Path, args: argparse.Namespace) -> dict:
    import soundfile as sf

    audio_dir = out / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    mode = "mock" if args.mock_vocal else args.vocal
    code = [hashlib.sha256(Path(m.__file__).read_bytes()).hexdigest()[:8] for m in (music, vocal)]
    key = _digest([VERSION, code, tl.raw, mode, args.voice, args.speed, args.vocal_octave])
    final = audio_dir / f"song-{key}.wav"
    meta_path = audio_dir / f"song-{key}.json"
    if final.is_file() and meta_path.is_file():
        return json.loads(meta_path.read_text())
    report: dict = {"vocal_mode": mode}
    music_path = audio_dir / "music.wav"
    report["music"] = music.write(tl, music_path)
    if mode != "mock":
        tts_dir = out / "vocal" / "tts"
        jobs = vocal.tts_jobs(tl, tts_dir, args.voice, args.speed)
        if not all(Path(j["wav"]).is_file() and Path(j["json"]).is_file() for j in jobs):
            jobs_file = out / "vocal" / "tts-jobs.json"
            jobs_file.parent.mkdir(parents=True, exist_ok=True)
            jobs_file.write_text(json.dumps(jobs, indent=1))
            t0 = time.monotonic()
            guarded([sys.executable, "-m", "scripts.kids_song.vocal", "tts", "--jobs",
                     str(jobs_file), "--voice", args.voice, "--speed", str(args.speed)])
            report["kokoro_seconds"] = round(time.monotonic() - t0, 1)
        voice_audio, vrep = vocal.render_voice(tl, tts_dir, mode, args.voice, args.speed,
                                               octave=args.vocal_octave)
    else:
        voice_audio, vrep = vocal.render_voice(tl, None, "mock")
    report["vocal"] = vrep
    sf.write(str(audio_dir / "vocal.wav"), voice_audio, vocal.SR, subtype="PCM_24")
    music_audio, _ = sf.read(str(music_path), dtype="float32")
    mix, duck = vocal.duck_and_mix(music_audio, voice_audio)
    sf.write(str(audio_dir / "mix.wav"), mix, vocal.SR, subtype="PCM_24")
    report["ducking"] = duck
    report.update(assemble.finalize_audio(audio_dir / "mix.wav", final))
    report["final"] = assemble.audio_stats(final)
    report["final"]["path"] = str(final)
    meta_path.write_text(json.dumps(report, indent=1, default=_np_default))
    return report


def build_clips(tl: episode.Timeline, out: Path, plan: list[dict], fmts: list[clips.Format],
                mock: bool) -> tuple[dict[str, list[Path]], list[dict]]:
    raw = clips.all_raw(tl, out, mock)
    conformed: dict[str, list[Path]] = {f.name: [] for f in fmts}
    for shot, p in zip(tl.shots, plan, strict=True):
        frames = clips.frames_for(shot.dur)
        src_dir = clips.raw_dir(out, shot, frames, mock)
        src = src_dir / "video.mp4"
        if not src.is_file():
            raise SystemExit(f"Missing raw clip for shot {shot.index}: {src}")
        src_frames = clips.probe_frames(src)
        for fmt in fmts:
            key = _digest([VERSION, src_dir.name, src_frames, p["need_frames"], asdict(fmt)])
            dest = out / "clips" / fmt.name / f"shot-{shot.index:02d}-{key}.mp4"
            if not dest.is_file():
                clips.conform(src, src_frames, p["need_frames"], fmt, dest)
            conformed[fmt.name].append(dest)
    return conformed, raw


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--episode", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--dry-run", action="store_true", help="print the plan; render nothing")
    ap.add_argument("--mock-clips", action="store_true", help="coloured placeholder clips")
    ap.add_argument("--mock-vocal", action="store_true", help="'ooh' guide tone, no Kokoro")
    ap.add_argument("--vocal", choices=("sung", "chant"), default="sung")
    ap.add_argument("--voice", default=vocal.DEFAULT_VOICE)
    ap.add_argument("--speed", type=float, default=vocal.DEFAULT_SPEED)
    ap.add_argument("--vocal-octave", type=int, choices=(-12, 0, 12),
                    help="force the sung octave (default: auto from the voice's register)")
    ap.add_argument("--shots", help="comma list of shot indices to generate (FastWan only)")
    ap.add_argument("--clips-only", action="store_true", help="generate raw clips, then stop")
    ap.add_argument("--formats", default="9x16,16x9")
    ap.add_argument("--scale", type=float, default=1.0, help=argparse.SUPPRESS)  # tests
    args = ap.parse_args(argv)

    tl = episode.load(args.episode)
    out = args.out
    plan = assemble.plan_edit(tl)
    summary = plan_summary(tl, plan)
    only = {int(x) for x in args.shots.split(",")} if args.shots else None
    if args.dry_run:
        summary["fastwan"] = [] if args.mock_clips else clips.all_raw(tl, out, False, True, only)
        print(json.dumps(summary, indent=1))
        return 0
    out.mkdir(parents=True, exist_ok=True)
    (out / "plan.json").write_text(json.dumps(summary, indent=1))
    if args.clips_only:
        raw = clips.all_raw(tl, out, args.mock_clips, False, only)
        print(json.dumps(raw, indent=1))
        return 0
    if only is not None:
        ap.error("--shots is only for --clips-only or --dry-run")

    started = time.monotonic()
    fmts = [scaled({"9x16": clips.VERTICAL, "16x9": clips.HORIZONTAL}[f.strip()], args.scale)
            for f in args.formats.split(",")]
    audio = build_audio(tl, out, args)
    conformed, raw = build_clips(tl, out, plan, fmts, args.mock_clips)
    final_audio = Path(audio["final"]["path"])
    videos = {}
    for fmt in fmts:
        name = "video.mp4" if fmt.name == "9x16" else f"video-{fmt.name}.mp4"
        videos[fmt.name] = assemble.render_format(tl, plan, conformed[fmt.name], final_audio,
                                                  fmt, out, name, args.mock_clips)
    run = {
        "tool": "scripts.kids_song.make", "version": VERSION, "episode": str(args.episode),
        **{k: summary[k] for k in ("title", "slug", "bpm", "key", "seconds", "chords")},
        "videos": videos, "audio": audio, "clips": raw,
        "mock_clips": args.mock_clips, "do_not_publish": bool(args.mock_clips),
        "made_for_kids": True, "ai_disclosure": True, "approved": False, "published": False,
        "notes": "Local prototype. Needs human review (visual drift, lyrics, audio) before "
                 "any use; the studio publish gate rejects made-for-kids content.",
        "build_seconds": round(time.monotonic() - started, 1),
    }
    (out / "run.json").write_text(json.dumps(run, indent=1, default=_np_default))
    f = audio["final"]
    print(f"Wrote {', '.join(v['path'] for v in videos.values())}\n"
          f"audio {f['seconds']} s, {f['integrated_lufs']} LUFS, true peak "
          f"{f['true_peak_dbtp']} dBTP, clipped samples {f['clipped_samples']}")
    return 0


def _np_default(o):
    if isinstance(o, np.generic):
        return o.item()
    return str(o)


if __name__ == "__main__":
    raise SystemExit(main())
