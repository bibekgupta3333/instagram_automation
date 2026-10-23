"""Deterministic synthesised backing track (pure numpy/scipy, no samples or soundfonts).

Layers: marimba lead on the melody, music-box arpeggio in the intro/outro, soft pad and
off-beat music-box plucks on the bar chord (I/IV/V from episode.choose_chords), a bass on the
chord root/fifth, a soft synthesised kick and a shaker on eighth notes. Everything goes
through ADSR/exponential envelopes and a synthetic-IR convolution reverb, then the mix is
peak-normalised to -1 dBFS. Same episode in -> bit-identical WAV out.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from scipy import signal

from .episode import Timeline, midi_to_hz, triads

SR = 48000
PEAK = 10 ** (-1 / 20)  # -1 dBFS
SEED = 20261023


def _pan(pan: float) -> tuple[float, float]:
    theta = (pan + 1) * np.pi / 4
    return float(np.cos(theta)), float(np.sin(theta))


class Bus:
    """Stereo buffer with a separate reverb send."""

    def __init__(self, n: int):
        self.dry = np.zeros((n, 2), dtype=np.float64)
        self.send = np.zeros((n, 2), dtype=np.float64)

    def add(self, start_s: float, sig: np.ndarray, gain: float, pan: float = 0.0,
            reverb: float = 0.0, sr: int = SR) -> None:
        i = round(start_s * sr)
        n = len(self.dry)
        if i >= n or len(sig) == 0:
            return
        seg = sig[: n - i] * gain
        left, right = _pan(pan)
        self.dry[i:i + len(seg), 0] += seg * left
        self.dry[i:i + len(seg), 1] += seg * right
        if reverb:
            self.send[i:i + len(seg), 0] += seg * left * reverb
            self.send[i:i + len(seg), 1] += seg * right * reverb


def adsr(n: int, sr: int, attack: float, decay: float, sustain: float, release: float,
         hold_s: float) -> np.ndarray:
    """ADSR envelope of n samples; the release starts at hold_s."""
    t = np.arange(n) / sr
    env = np.where(t < attack, t / max(attack, 1e-6), 0.0)
    dmask = (t >= attack)
    env = np.where(dmask, sustain + (1 - sustain) * np.exp(-(t - attack) / max(decay, 1e-6)),
                   env)
    level_at_release = sustain + (1 - sustain) * np.exp(-max(hold_s - attack, 0) / max(decay,
                                                                                        1e-6))
    rmask = t >= hold_s
    env = np.where(rmask, level_at_release * np.exp(-(t - hold_s) / max(release, 1e-6)), env)
    return env


def marimba(freq: float, dur: float, sr: int = SR) -> np.ndarray:
    n = int((dur + 0.5) * sr)
    t = np.arange(n) / sr
    tone = (np.sin(2 * np.pi * freq * t) * np.exp(-t / 0.55)
            + 0.28 * np.sin(2 * np.pi * 4.0 * freq * t) * np.exp(-t / 0.10)
            + 0.05 * np.sin(2 * np.pi * 9.9 * freq * t) * np.exp(-t / 0.03))
    attack = np.minimum(t / 0.004, 1.0)
    release = np.where(t > dur, np.exp(-(t - dur) / 0.08), 1.0)
    return tone * attack * release


def music_box(freq: float, sr: int = SR, length: float = 1.2) -> np.ndarray:
    n = int(length * sr)
    t = np.arange(n) / sr
    tone = (np.sin(2 * np.pi * freq * t) * np.exp(-t / 0.45)
            + 0.18 * np.sin(2 * np.pi * 5.4 * freq * t) * np.exp(-t / 0.06))
    return tone * np.minimum(t / 0.002, 1.0)


def pad(freqs: list[float], dur: float, sr: int = SR) -> np.ndarray:
    n = int((dur + 0.5) * sr)
    t = np.arange(n) / sr
    tone = np.zeros(n)
    for f in freqs:
        for cents in (-4.0, 4.0):
            ff = f * 2 ** (cents / 1200)
            tone += np.sin(2 * np.pi * ff * t) + 0.15 * np.sin(4 * np.pi * ff * t)
    env = adsr(n, sr, 0.25, 0.6, 0.75, 0.35, dur)
    return tone * env / (2 * len(freqs))


def bass(freq: float, dur: float, sr: int = SR) -> np.ndarray:
    n = int((dur + 0.15) * sr)
    t = np.arange(n) / sr
    tone = (np.sin(2 * np.pi * freq * t) + 0.45 * np.sin(4 * np.pi * freq * t)
            + 0.15 * np.sin(6 * np.pi * freq * t))
    return tone * adsr(n, sr, 0.008, 0.5, 0.45, 0.06, dur)


def kick(sr: int = SR) -> np.ndarray:
    n = int(0.35 * sr)
    t = np.arange(n) / sr
    freq = 48 + 72 * np.exp(-t / 0.035)
    phase = 2 * np.pi * np.cumsum(freq) / sr
    return np.sin(phase) * np.exp(-t / 0.16) * np.minimum(t / 0.002, 1.0)


def shaker(rng: np.random.Generator, sr: int = SR) -> np.ndarray:
    n = int(0.09 * sr)
    t = np.arange(n) / sr
    noise = rng.standard_normal(n)
    sos = signal.butter(4, [min(4500, 0.25 * sr), min(11000, 0.45 * sr)], btype="bandpass", fs=sr,
                        output="sos")
    noise = signal.sosfilt(sos, noise)
    env = np.minimum(t / 0.006, 1.0) * np.exp(-t / 0.028)
    return noise * env


def reverb_ir(sr: int = SR, length: float = 1.6, decay: float = 0.38) -> np.ndarray:
    rng = np.random.default_rng(SEED + 1)
    n = int(length * sr)
    t = np.arange(n) / sr
    sos = signal.butter(2, min(6500, 0.4 * sr), btype="lowpass", fs=sr, output="sos")
    ir = np.zeros((n, 2))
    pre = int(0.018 * sr)
    for ch in range(2):
        tail = signal.sosfilt(sos, rng.standard_normal(n)) * np.exp(-t / decay)
        ir[pre:, ch] = tail[: n - pre]
    ir /= np.sqrt((ir ** 2).sum(axis=0, keepdims=True))
    return ir


def _midi_in(pc: int, lo: int) -> int:
    """Smallest midi note >= lo with pitch class pc."""
    return lo + (pc - lo) % 12


def render(tl: Timeline, sr: int = SR) -> np.ndarray:
    """The full backing track as float32 stereo, exactly tl.total_sec long."""
    n = round(tl.total_sec * sr)
    bus = Bus(n)
    rng = np.random.default_rng(SEED)
    chords = triads(tl.key)
    beat, bpb = tl.beat_sec, tl.beats_per_bar
    bars = len(tl.chords)
    intro_bars, melody_bars = tl.intro_bars, tl.melody_bars
    k = kick(sr)

    for bar, name in enumerate(tl.chords):
        root, tri = chords[name]
        t0 = bar * bpb * beat
        last = bar == bars - 1
        bar_len = bpb * beat
        pad_freqs = [midi_to_hz(_midi_in(pc, 55)) for pc in tri]
        bus.add(t0, pad(pad_freqs, bar_len * (1.0 if not last else 0.9), sr), 0.20,
                0.0, 0.5, sr)
        # bass: root on beat 1, fifth on beat 3 (4/4); root only otherwise
        r = midi_to_hz(_midi_in(root, 40))
        fifth = midi_to_hz(_midi_in(tri[2], 40))
        hits = [(0, r)] + ([(2, fifth)] if bpb == 4 and not last else [])
        for b, f in hits:
            bus.add(t0 + b * beat, bass(f, beat * (1.8 if not last else bpb * 0.9), sr), 0.32,
                    0.0, 0.05, sr)
        # soft kick on 1 and 3, shaker on eighths (not in the final held bar)
        for b in range(bpb):
            if b % 2 == 0 and (not last or b == 0):
                bus.add(t0 + b * beat, k, 0.42 if b == 0 else 0.32, 0.0, 0.0, sr)
            if last:
                continue
            for half in (0, 1):
                bus.add(t0 + (b + half / 2) * beat, shaker(rng, sr), 0.10 if half else 0.06,
                        0.35, 0.1, sr)
        # off-beat plucks on beats 2 and 4 inside the sung part
        if intro_bars <= bar < intro_bars + melody_bars:
            for b in range(1, bpb, 2):
                for pc in tri:
                    bus.add(t0 + b * beat, music_box(midi_to_hz(_midi_in(pc, 67)), sr), 0.045,
                            0.45, 0.4, sr)
        # intro and outro: gentle music-box arpeggio on eighth notes, up then down
        if bar < intro_bars or bar >= intro_bars + melody_bars:
            arp = [_midi_in(pc, 67) for pc in tri] + [_midi_in(tri[0], 67) + 12]
            arp = arp + arp[-2:0:-1]
            steps = int(bpb * 2) if not last else 4
            for s in range(steps):
                bus.add(t0 + s * beat / 2, music_box(midi_to_hz(arp[s % len(arp)]), sr), 0.16,
                        -0.2, 0.45, sr)

    for note in tl.notes:  # marimba lead on the written melody
        if note.midi is None:
            continue
        bus.add(note.start, marimba(midi_to_hz(note.midi), note.dur * 0.95, sr), 0.30, -0.25,
                0.35, sr)

    ir = reverb_ir(sr)
    wet = np.stack([signal.fftconvolve(bus.send[:, c], ir[:, c])[:n] for c in range(2)], axis=1)
    mix = bus.dry + 0.6 * wet
    sos = signal.butter(2, 30, btype="highpass", fs=sr, output="sos")
    mix = signal.sosfiltfilt(sos, mix, axis=0)
    fade = int(min(1.2, tl.total_sec / 4) * sr)
    mix[-fade:] *= np.linspace(1.0, 0.0, fade)[:, None] ** 2
    mix[: int(0.01 * sr)] *= np.linspace(0, 1, int(0.01 * sr))[:, None]
    peak = float(np.max(np.abs(mix))) or 1.0
    return (mix * (PEAK / peak)).astype(np.float32)


def write(tl: Timeline, path: Path, sr: int = SR) -> dict:
    import soundfile as sf

    audio = render(tl, sr)
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), audio, sr, subtype="PCM_24")
    return {"path": str(path), "sample_rate": sr, "samples": len(audio),
            "seconds": round(len(audio) / sr, 3), "peak": round(float(np.abs(audio).max()), 4),
            "chords": tl.chords}
