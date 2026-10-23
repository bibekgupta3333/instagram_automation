"""Vocal track aligned to the melody, plus the music/vocal mix with ducking.

Modes:
  sung  (Option A) Kokoro speaks each lyric line (natural coarticulation, word timestamps),
        the line is cut into syllables, and every syllable is re-synthesised with TD-PSOLA
        at the melody note's pitch and duration: flat pitch per note with a small scoop and
        late vibrato, consonants kept at natural speed, vowels stretched. PSOLA keeps the
        formants, so it sounds less "chipmunk" than a phase-vocoder pitch shift.
  chant (Option B) same Kokoro lines, syllables placed on the beat grid, natural pitch.
  mock  offline "ooh" guide tone at the melody pitch (tests, --mock-vocal).

Kokoro runs only in a guarded child process (`python -m scripts.kids_song.vocal tts ...`
under scripts/run_guarded.py); everything else here is numpy/scipy/librosa (ISC).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
from scipy import signal

from .episode import Line, Note, Timeline, letters, midi_to_hz

SR = 48000
TTS_SR = 24000
DEFAULT_VOICE = "af_heart"
DEFAULT_SPEED = 0.9
PEAK = 10 ** (-1 / 20)


# ---------------------------------------------------------------- Kokoro (guarded child)


def tts_key(text: str, voice: str, speed: float) -> str:
    from studio.stages.tts import KOKORO_MODEL

    raw = json.dumps([text, voice, speed, KOKORO_MODEL, 1]).encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def tts_jobs(tl: Timeline, tts_dir: Path, voice: str, speed: float) -> list[dict[str, Any]]:
    jobs = []
    for i, line in enumerate(tl.lines):
        key = tts_key(line.text, voice, speed)
        jobs.append({"index": i, "text": line.text, "wav": str(tts_dir / f"{key}.wav"),
                     "json": str(tts_dir / f"{key}.json")})
    return jobs


def run_tts(jobs: list[dict[str, Any]], voice: str, speed: float) -> None:
    """Synthesise each missing line with Kokoro; write wav + word timings. Child process only."""
    import soundfile as sf

    from studio.stages import tts as T

    T.check_voice(voice)
    lang = T.lang_code(voice) if hasattr(T, "lang_code") else "a"  # bf_/bm_ -> British
    pipeline = T._load_kokoro(lang) if lang != "a" else T._load_kokoro()
    rate = int(getattr(pipeline.model, "sample_rate", TTS_SR))
    try:
        for job in jobs:
            if Path(job["wav"]).is_file() and Path(job["json"]).is_file():
                continue
            chunks, words, offset = [], [], 0
            for result in pipeline(job["text"], voice=voice, speed=speed):
                if result.audio is None:
                    del result
                    T._clear_mlx_cache()
                    continue
                audio = np.asarray(result.audio, dtype=np.float32).reshape(-1)
                for w in T._segment_words([result], rate):
                    words.append({**w, "start": round(offset / rate + w["start"], 3),
                                  "end": round(offset / rate + w["end"], 3)})
                chunks.append(audio)
                offset += len(audio)
                del audio, result
                T._clear_mlx_cache()  # per segment, as in the TTS stage
            if not chunks:
                raise RuntimeError(f"Kokoro returned no audio for line {job['index']}")
            Path(job["wav"]).parent.mkdir(parents=True, exist_ok=True)
            sf.write(job["wav"], np.concatenate(chunks), rate, subtype="FLOAT")
            Path(job["json"]).write_text(json.dumps({"text": job["text"], "voice": voice,
                                                      "speed": speed, "sample_rate": rate,
                                                      "words": words}, indent=1))
    finally:
        T.unload()


# ---------------------------------------------------------------- analysis


def frame_rms(x: np.ndarray, hop: int) -> np.ndarray:
    n = max(1, len(x) // hop)
    frames = x[: n * hop].reshape(n, hop) if len(x) >= hop else x.reshape(1, -1)
    return np.sqrt((frames ** 2).mean(axis=1) + 1e-12)


def f0_track(x: np.ndarray, sr: int, hop: int) -> tuple[np.ndarray, np.ndarray]:
    """(f0 Hz with 0 for unvoiced, voiced flag) per hop; pYIN from librosa."""
    import librosa

    f0, voiced, _ = librosa.pyin(x.astype(np.float64), fmin=75, fmax=600, sr=sr,
                                 frame_length=1024, hop_length=hop, center=True)
    f0 = np.nan_to_num(f0, nan=0.0)
    voiced = voiced & (f0 > 0)
    if voiced.any():
        med = signal.medfilt(np.where(voiced, f0, 0.0), 5)
        f0 = np.where(voiced & (med > 0), med, f0)
    return f0, voiced


def pitch_marks(x: np.ndarray, sr: int, f0: np.ndarray, voiced: np.ndarray, hop: int,
                unvoiced_hz: float = 160.0) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Analysis marks, their local period (samples) and voicing, period-synchronous."""
    marks, periods, flags = [], [], []
    t, last = 0.0, -1e9
    while t < len(x):
        fi = min(int(t / hop), len(f0) - 1)
        if voiced[fi]:
            period = sr / f0[fi]
            lo, hi = int(max(0, t - period / 4)), int(min(len(x), t + period / 4 + 1))
            peak = lo + int(np.argmax(x[lo:hi])) if hi > lo else int(t)
            if peak <= last + period * 0.5:
                peak = int(last + period)
            marks.append(peak)
            periods.append(period)
            flags.append(True)
            last = peak
            t = peak + period
        else:
            period = sr / unvoiced_hz
            marks.append(int(t))
            periods.append(period)
            flags.append(False)
            last = t
            t += period
    return np.array(marks), np.array(periods), np.array(flags, dtype=bool)


def psola(x: np.ndarray, sr: int, f0: np.ndarray, voiced: np.ndarray, hop: int,
          out_len: int, target_hz: float, prev_hz: float | None = None) -> np.ndarray:
    """TD-PSOLA: re-synthesise `x` at a flat target pitch over out_len samples.

    Consonants (before/after the voiced core) keep their natural speed where possible; the
    voiced core is stretched or squeezed to fill the note. Scoop from prev_hz (or slightly
    flat) into the note, and add vibrato on long notes.
    """
    if len(x) < 32 or out_len < 32:
        return np.zeros(max(out_len, 0))
    marks, periods, flags = pitch_marks(x, sr, f0, voiced, hop)
    vframes = np.flatnonzero(voiced[: max(1, len(x) // hop + 1)])
    ls = len(x)
    if len(vframes):
        v0, v1 = vframes[0] * hop, min(ls, (vframes[-1] + 1) * hop)
    else:
        v0, v1 = 0, ls
    onset, coda = v0, ls - v1
    if out_len >= ls and v1 > v0:
        budget = 0.6 * out_len
        scale = min(1.0, budget / max(onset + coda, 1))
        a, c = onset * scale, coda * scale
        knots_out, knots_src = [0, a, out_len - c, out_len], [0, v0, v1, ls]
    else:
        knots_out, knots_src = [0, out_len], [0, ls]

    pad = int(np.max(periods)) + 2 if len(periods) else 2
    y = np.zeros(out_len + 2 * pad)
    w = np.zeros_like(y)
    t = 0.0
    scoop_from = prev_hz if prev_hz else target_hz * 2 ** (-0.7 / 12)
    scoop_len = 0.06 * sr
    vib_start, vib_ramp = 0.22 * sr, 0.15 * sr
    long_note = out_len > 0.45 * sr
    while t < out_len:
        ts = float(np.interp(t, knots_out, knots_src))
        k = int(np.clip(np.searchsorted(marks, ts), 0, len(marks) - 1))
        if k > 0 and abs(marks[k - 1] - ts) < abs(marks[k] - ts):
            k -= 1
        period, mark = periods[k], int(marks[k])
        half = max(8, int(period))
        lo, hi = mark - half, mark + half
        grain = np.zeros(2 * half)
        s0, s1 = max(lo, 0), min(hi, ls)
        if s1 > s0:
            grain[s0 - lo: s1 - lo] = x[s0:s1]
        win = np.hanning(2 * half)
        c = int(t) + pad
        y[c - half: c + half] += grain * win
        w[c - half: c + half] += win
        if flags[k]:
            hz = target_hz
            if t < scoop_len:
                frac = t / scoop_len
                hz = scoop_from * (target_hz / scoop_from) ** (frac ** 0.5)
            if long_note and t > vib_start:
                depth = 0.22 * min(1.0, (t - vib_start) / vib_ramp)
                hz *= 2 ** (depth * np.sin(2 * np.pi * 5.5 * (t - vib_start) / sr) / 12)
            t += sr / hz
        else:
            t += period
    y = y / np.maximum(w, 1.0)
    return y[pad: pad + out_len]


# ---------------------------------------------------------------- syllable segmentation


def split_word(x: np.ndarray, s0: int, s1: int, weights: list[int], sr: int) -> list[int]:
    """Boundaries (k+1 sample indices) splitting x[s0:s1] into len(weights) syllables,
    starting from letter-proportional cuts and snapping each to a nearby energy dip."""
    k = len(weights)
    if k == 1 or s1 - s0 < k * 0.03 * sr:
        cuts = np.cumsum([0] + weights) / max(sum(weights), 1)
        return [int(s0 + c * (s1 - s0)) for c in cuts]
    hop = int(0.005 * sr)
    rms = frame_rms(x[s0:s1], hop)
    cuts = np.cumsum([0] + weights) / sum(weights)
    bounds = [s0]
    span = (s1 - s0) / k
    for c in cuts[1:-1]:
        guess = c * (s1 - s0)
        lo, hi = int(max(0, (guess - 0.3 * span) / hop)), int(min(len(rms), (guess + 0.3 * span)
                                                                  / hop + 1))
        idx = lo + int(np.argmin(rms[lo:hi])) if hi > lo else int(guess / hop)
        bounds.append(max(bounds[-1] + hop, s0 + idx * hop))
    bounds.append(s1)
    return bounds


def syllable_segments(line: Line, notes: list[Note], words: list[dict[str, Any]],
                      x: np.ndarray, sr: int) -> dict[int, tuple[int, int]]:
    """Map each sung note of the line to a (start, end) sample range in the spoken line."""
    sung = [notes[i] for i in line.notes]
    segs: dict[int, tuple[int, int]] = {}
    wi, si = 0, 0
    ok = True
    while si < len(sung) and wi < len(words):
        target = letters(words[wi]["text"])
        group, acc = [], ""
        while si < len(sung) and len(acc) < len(target):
            acc += letters(sung[si].syllable)
            group.append(sung[si])
            si += 1
        if acc != target or not group:
            ok = False
            break
        s0 = int(words[wi]["start"] * sr)
        s1 = max(s0 + 1, int(words[wi]["end"] * sr))
        bounds = split_word(x, s0, min(s1, len(x)), [max(1, len(letters(n.syllable)))
                                                     for n in group], sr)
        for n, a, b in zip(group, bounds[:-1], bounds[1:], strict=True):
            segs[n.index] = (a, b)
        wi += 1
    if ok and si == len(sung) and len(segs) == len(sung):
        return segs
    # fallback: letter-proportional cuts over the whole spoken span
    start = int((words[0]["start"] if words else 0) * sr)
    end = int((words[-1]["end"] if words else len(x) / sr) * sr)
    bounds = split_word(x, start, min(max(end, start + 1), len(x)),
                        [max(1, len(letters(n.syllable))) for n in sung], sr)
    return {n.index: (a, b) for n, a, b in zip(sung, bounds[:-1], bounds[1:], strict=True)}


# ---------------------------------------------------------------- rendering


def _fade(y: np.ndarray, sr: int, ms: float = 8.0) -> np.ndarray:
    n = min(len(y) // 2, int(ms / 1000 * sr))
    if n > 0:
        ramp = np.linspace(0, 1, n)
        y[:n] *= ramp
        y[-n:] *= ramp[::-1]
    return y


def octave_shift(tl: Timeline, source_hz: float | None, max_up: float = 9.0) -> int:
    """0 (the written octave) unless the speaker's register needs more than `max_up`
    semitones of PSOLA up-shift on average; then one octave down (-12). Large up-shifts of
    speech get buzzy, so a lower octave sounds more natural for most Kokoro voices."""
    pitched = [n.midi for n in tl.notes if n.sung]
    if not pitched or not source_hz:
        return 0
    target = float(np.median(pitched))
    src = 69 + 12 * np.log2(source_hz / 440.0)
    return 0 if target - src <= max_up else -12


def place(out: np.ndarray, start: int, y: np.ndarray) -> None:
    if start >= len(out):
        return
    seg = y[: len(out) - start]
    out[start:start + len(seg)] += seg


def render_voice(tl: Timeline, tts_dir: Path | None, mode: str, voice: str = DEFAULT_VOICE,
                 speed: float = DEFAULT_SPEED, sr: int = SR,
                 octave: int | None = None) -> tuple[np.ndarray, dict[str, Any]]:
    """Mono vocal at `sr`, exactly the song length, plus a report dict."""
    n_out = round(tl.total_sec * sr)
    out = np.zeros(n_out)
    report: dict[str, Any] = {"mode": mode}
    if mode == "mock":
        for n in tl.notes:
            if n.sung:
                place(out, int(n.start * sr), _ooh(midi_to_hz(n.midi), int(n.dur * 0.92 * sr),
                                                   sr))
        return _finish(out, sr), report

    import librosa
    import soundfile as sf

    assert tts_dir is not None
    jobs = tts_jobs(tl, tts_dir, voice, speed)
    loaded = []
    for job, line in zip(jobs, tl.lines, strict=True):
        x, rate = sf.read(job["wav"], dtype="float32")
        meta = json.loads(Path(job["json"]).read_text())
        if x.ndim > 1:
            x = x.mean(axis=1)
        loaded.append((line, x.astype(np.float64), rate, meta["words"]))
    hop_t = 256
    tracks, medians = [], []
    for line, x, rate, words in loaded:
        f0, vo = f0_track(x, rate, hop_t) if mode == "sung" else (None, None)
        tracks.append((f0, vo))
        if f0 is not None and vo.any():
            medians.append(float(np.median(f0[vo])))
    src_hz = float(np.median(medians)) if medians else None
    shift = octave if octave is not None else octave_shift(tl, src_hz)
    report.update(source_median_hz=round(src_hz, 1) if src_hz else None, octave_shift=shift,
                  voice=voice, speed=speed)
    rms_ref: list[float] = []
    pieces: list[tuple[int, np.ndarray]] = []
    prev_hz: float | None = None
    for (line, x, rate, words), (f0, vo) in zip(loaded, tracks, strict=True):
        segs = syllable_segments(line, tl.notes, words, x, rate)
        sung = [tl.notes[i] for i in line.notes]
        for idx, note in enumerate(sung):
            a, b = segs[note.index]
            seg = x[a:b]
            if mode == "sung":
                hz = midi_to_hz(note.midi + shift)
                fa, fb = a // hop_t, max(a // hop_t + 1, -(-b // hop_t))
                # re-index the analysis frames to the segment (approximate frame alignment)
                y = psola(seg, rate, f0[fa:fb], vo[fa:fb], hop_t,
                          int(note.dur * 0.94 * rate), hz, prev_hz)
                prev_hz = hz
                nxt = sung[idx + 1] if idx + 1 < len(sung) else None
                if nxt is None or nxt.start - (note.start + note.dur) > 1e-6:
                    prev_hz = None  # phrase break: next note scoops from slightly flat
            else:  # chant: natural pitch, trimmed to the note slot
                y = seg[: int(note.dur * 0.97 * rate)].copy()
            y = _fade(y, rate)
            if rate != sr:
                y = signal.resample_poly(y, sr, rate)
            rms = float(np.sqrt(np.mean(y ** 2))) if len(y) else 0.0
            if rms > 1e-5:
                rms_ref.append(rms)
            pieces.append((int(note.start * sr), y))
    ref = float(np.median(rms_ref)) if rms_ref else 1.0
    for start, y in pieces:  # even out syllable levels (bounded gain)
        rms = float(np.sqrt(np.mean(y ** 2))) if len(y) else 0.0
        gain = float(np.clip(ref / rms, 0.6, 1.8)) if rms > 1e-5 else 1.0
        place(out, start, y * gain)
    del librosa
    return _finish(out, sr), report


def _ooh(hz: float, n: int, sr: int) -> np.ndarray:
    t = np.arange(n) / sr
    vib = 2 ** (0.2 * np.sin(2 * np.pi * 5.5 * t) * np.minimum(t / 0.3, 1) / 12)
    phase = 2 * np.pi * np.cumsum(hz * vib) / sr
    tone = sum(a * np.sin(k * phase) for k, a in ((1, 1.0), (2, 0.35), (3, 0.12), (4, 0.05)))
    env = np.minimum(t / 0.03, 1.0) * np.minimum((n / sr - t) / 0.05, 1.0)
    return tone * np.clip(env, 0, 1)


def _finish(y: np.ndarray, sr: int) -> np.ndarray:
    from .music import reverb_ir

    sos = signal.butter(2, 110, btype="highpass", fs=sr, output="sos")
    y = signal.sosfilt(sos, y)
    ir = reverb_ir(sr, length=1.0, decay=0.22)[:, 0]
    wet = signal.fftconvolve(y, ir)[: len(y)]
    y = y + 0.12 * wet
    peak = float(np.max(np.abs(y))) or 1.0
    return (y * (PEAK / peak)).astype(np.float32)


def duck_and_mix(music: np.ndarray, vocal: np.ndarray, sr: int = SR,
                 duck_db: float = 4.0) -> tuple[np.ndarray, dict[str, float]]:
    """Stereo mix: music ducked by up to duck_db while the vocal sings; -1 dBFS peak."""
    n = min(len(music), len(vocal))
    music, vocal = music[:n].astype(np.float64), vocal[:n].astype(np.float64)
    env = np.abs(vocal)
    att, rel = np.exp(-1 / (0.01 * sr)), np.exp(-1 / (0.25 * sr))
    # one-pole envelope follower, vectorised in blocks via lfilter on the release slope
    smooth = signal.lfilter([1 - rel], [1, -rel], env)
    smooth = np.maximum(smooth, signal.lfilter([1 - att], [1, -att], env))
    thresh = max(float(np.percentile(smooth, 90)) * 0.5, 1e-4)
    amount = np.clip(smooth / thresh, 0, 1)
    gain = 10 ** (-duck_db * amount / 20)
    mix = music * 0.5 * gain[:, None] + vocal[:, None] * 0.95
    peak = float(np.max(np.abs(mix))) or 1.0
    mix *= PEAK / peak
    return mix.astype(np.float32), {"duck_db": duck_db,
                                    "ducked_fraction": round(float((amount > 0.5).mean()), 3)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Guarded Kokoro line synthesis (child step)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tts")
    t.add_argument("--jobs", type=Path, required=True)
    t.add_argument("--voice", default=DEFAULT_VOICE)
    t.add_argument("--speed", type=float, default=DEFAULT_SPEED)
    args = parser.parse_args(argv)
    import os

    if os.environ.get("HF_HUB_OFFLINE") != "1" or os.environ.get("STUDIO_CPU_THREADS") != "3":
        parser.error("Run Kokoro through scripts/run_guarded.py")
    run_tts(json.loads(args.jobs.read_text()), args.voice, args.speed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
