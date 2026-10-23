"""Episode JSON loading plus all melody/beat timing maths (pure, no I/O beyond load()).

Episode shape (docs/2-channels/27-<slug>-episode-01.json):
  {title, bpm, key, melody: [{note: "C4", beats: 1.0, syllable: "Splash"}, ...],
   lyrics_lines: ["Splash splash little duck", ...],
   shots: [{start_beat, lyric, prompt, shot}, ...]}
Optional: beats_per_bar (4), intro_bars (2), outro_bars (2), slug, voice, seed per shot.

Conventions: `start_beat` of a shot counts from the first melody note (beat 0), not from the
start of the intro; the first shot also covers the intro and the last one the outro. A melody
item with note "R"/"rest"/"" (or null) is a rest. Times are seconds on the song timeline,
which starts at the first intro beat.
"""

from __future__ import annotations

import json
import math
import re
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

NOTE_RE = re.compile(r"^\s*([A-Ga-g])([#b♯♭]?)(-?\d)\s*$")
PC = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
REST_NAMES = {"", "r", "rest", "-", "_", "x"}
SCALES = {"major": (0, 2, 4, 5, 7, 9, 11), "minor": (0, 2, 3, 5, 7, 8, 10)}
FPS = 30


class EpisodeError(ValueError):
    pass


def note_to_midi(note: str | None) -> int | None:
    """'C4' -> 60, 'F#3' -> 54, 'Bb4' -> 70; rests -> None."""
    if note is None or str(note).strip().lower() in REST_NAMES:
        return None
    m = NOTE_RE.match(str(note))
    if not m:
        raise EpisodeError(f"Unparseable note {note!r} (expected e.g. C4, F#3, Bb4 or R)")
    letter, acc, octave = m.groups()
    shift = {"#": 1, "♯": 1, "b": -1, "♭": -1}.get(acc, 0)
    return 12 * (int(octave) + 1) + PC[letter.upper()] + shift


def midi_to_hz(midi: float) -> float:
    return 440.0 * 2 ** ((midi - 69) / 12)


def parse_key(key: str) -> tuple[int, str]:
    """'C', 'C major', 'G Major', 'A minor', 'Am', 'Bb', 'F# major' -> (tonic pc, mode)."""
    m = re.match(r"^\s*([A-Ga-g])([#b♯♭]?)\s*(m(?:in(?:or)?)?|maj(?:or)?)?\s*$", str(key or "C"),
                 re.IGNORECASE)
    if not m:
        raise EpisodeError(f"Unparseable key {key!r}")
    letter, acc, mode = m.groups()
    shift = {"#": 1, "♯": 1, "b": -1, "♭": -1}.get(acc, 0)
    minor = bool(mode) and mode.lower().startswith("m") and not mode.lower().startswith("maj")
    return (PC[letter.upper()] + shift) % 12, "minor" if minor else "major"


def triads(key: str) -> dict[str, tuple[int, tuple[int, int, int]]]:
    """Primary chords I, IV, V as (root pc, triad pcs). Minor keys use i, iv and major V."""
    tonic, mode = parse_key(key)
    scale = SCALES[mode]

    def triad(degree: int) -> tuple[int, int, int]:
        return tuple((tonic + scale[(degree + k) % 7]) % 12 for k in (0, 2, 4))  # type: ignore

    chords = {"I": triad(0), "IV": triad(3), "V": triad(4)}
    if mode == "minor":  # harmonic-minor dominant: raise the third of v
        r = chords["V"][0]
        chords["V"] = (r, (r + 4) % 12, (r + 7) % 12)
    return {name: (tri[0], tri) for name, tri in chords.items()}


def letters(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


@dataclass
class Note:
    index: int
    midi: int | None
    beats: float
    syllable: str
    start_beat: float  # song beats (intro included)
    start: float
    dur: float

    @property
    def sung(self) -> bool:
        return self.midi is not None and bool(letters(self.syllable))


@dataclass
class Line:
    text: str
    notes: list[int]  # melody indices of the sung syllables, in order
    pieces: list[tuple[str, int | None]]  # caption text pieces, tagged with their note index
    start: float = 0.0
    end: float = 0.0


@dataclass
class Shot:
    index: int
    start_beat: float  # song beats
    end_beat: float
    start: float
    end: float
    prompt: str
    lyric: str
    shot: str
    seed: int

    @property
    def dur(self) -> float:
        return self.end - self.start


@dataclass
class Timeline:
    title: str
    slug: str
    key: str
    bpm: float
    beats_per_bar: int
    intro_beats: float
    melody_beats: float
    total_beats: float
    notes: list[Note]
    lines: list[Line]
    shots: list[Shot]
    chords: list[str]  # one chord name per bar of the whole song
    outro_bars: int = 2
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def intro_bars(self) -> int:
        return round(self.intro_beats / self.beats_per_bar)

    @property
    def melody_bars(self) -> int:
        return len(self.chords) - self.intro_bars - self.outro_bars

    @property
    def beat_sec(self) -> float:
        return 60.0 / self.bpm

    @property
    def total_sec(self) -> float:
        return self.total_beats * self.beat_sec

    def beat_time(self, song_beat: float) -> float:
        return song_beat * self.beat_sec

    def is_bar(self, song_beat: float) -> bool:
        q = song_beat / self.beats_per_bar
        return abs(q - round(q)) < 1e-6


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:48] or "episode"


def _pieces_from_text(text: str, notes: list[Note]) -> list[tuple[str, int | None]] | None:
    """Split the printed line into caption pieces, one per sung syllable (letter matching)."""
    pieces: list[tuple[str, int | None]] = []
    pos = 0
    for n in notes:
        need = len(letters(n.syllable))
        gap_start = pos
        while pos < len(text) and not text[pos].isalnum():
            pos += 1
        if pos > gap_start:
            pieces.append((text[gap_start:pos], None))
        start, got = pos, 0
        while pos < len(text) and got < need:
            if text[pos].isalnum():
                got += 1
            pos += 1
        if got < need or letters(text[start:pos]) != letters(n.syllable):
            return None
        while pos < len(text) and not text[pos].isalnum() and not text[pos].isspace():
            pos += 1  # keep trailing punctuation ("duck,") with its syllable
        pieces.append((text[start:pos], n.index))
    if pos < len(text):
        pieces.append((text[pos:], None))
    return pieces


def _text_from_syllables(notes: list[Note]) -> tuple[str, list[tuple[str, int | None]]]:
    pieces: list[tuple[str, int | None]] = []
    for i, n in enumerate(notes):
        syl = n.syllable.strip()
        joined = syl.endswith("-")
        pieces.append((syl.rstrip("-"), n.index))
        if i < len(notes) - 1 and not joined:
            pieces.append((" ", None))
    return "".join(p for p, _ in pieces), pieces


def group_lines(notes: list[Note], lyrics_lines: list[str], beats_per_bar: int,
                intro_beats: float) -> list[Line]:
    """Assign sung syllables to the printed lyric lines; fall back to 2-bar phrases."""
    sung = [n for n in notes if n.sung]
    lines: list[Line] = []
    cursor = 0
    ok = bool(lyrics_lines)
    for text in lyrics_lines or []:
        target, acc, members = letters(text), "", []
        while cursor < len(sung) and len(acc) < len(target):
            acc += letters(sung[cursor].syllable)
            members.append(sung[cursor])
            cursor += 1
        pieces = _pieces_from_text(text, members) if acc == target else None
        if not target or pieces is None:
            ok = False
            break
        lines.append(Line(text=text, notes=[n.index for n in members], pieces=pieces))
    if not ok or cursor != len(sung):
        lines = []
        phrase = 2 * beats_per_bar
        groups: dict[int, list[Note]] = {}
        for n in sung:
            groups.setdefault(int((n.start_beat - intro_beats) // phrase), []).append(n)
        for _, members in sorted(groups.items()):
            text, pieces = _text_from_syllables(members)
            lines.append(Line(text=text, notes=[n.index for n in members], pieces=pieces))
    for line in lines:
        line.start = notes[line.notes[0]].start
        line.end = notes[line.notes[-1]].start + notes[line.notes[-1]].dur
    return lines


def choose_chords(notes: list[Note], key: str, beats_per_bar: int, intro_bars: int,
                  melody_bars: int, outro_bars: int) -> list[str]:
    """One of I/IV/V per bar: the chord whose tones cover the most melody duration."""
    chords = triads(key)
    intro = ["I"] * max(0, intro_bars - 1) + (["V"] if intro_bars else [])
    body = []
    start_bar = intro_bars
    for b in range(melody_bars):
        lo, hi = (start_bar + b) * beats_per_bar, (start_bar + b + 1) * beats_per_bar
        weight = [0.0] * 12
        for n in notes:
            if n.midi is None:
                continue
            overlap = min(hi, n.start_beat + n.beats) - max(lo, n.start_beat)
            if overlap > 0:
                # the note sounding on the downbeat counts double
                on_downbeat = n.start_beat <= lo < n.start_beat + n.beats
                weight[n.midi % 12] += overlap * (2.0 if on_downbeat else 1.0)
        best, best_score = "I", -1.0
        for name in ("I", "IV", "V"):
            root, tri = chords[name]
            score = sum(weight[pc] for pc in tri) + 0.25 * weight[root] + (0.01 if name == "I"
                                                                            else 0.0)
            if score > best_score + 1e-9:
                best, best_score = name, score
        body.append(best)
    if body:
        body[-1] = "I"
    outro = (["IV"] * max(0, outro_bars - 1) + ["I"]) if outro_bars else []
    return intro + body + outro


def build_timeline(ep: dict[str, Any]) -> Timeline:
    try:
        bpm = float(ep["bpm"])
        melody = list(ep["melody"])
    except (KeyError, TypeError, ValueError) as exc:
        raise EpisodeError(f"Episode needs numeric bpm and a melody list ({exc})") from exc
    if not 40 <= bpm <= 220 or not melody:
        raise EpisodeError("bpm must be 40..220 and the melody non-empty")
    bpb = int(ep.get("beats_per_bar", 4))
    intro_bars, outro_bars = int(ep.get("intro_bars", 2)), int(ep.get("outro_bars", 2))
    beat_sec = 60.0 / bpm
    intro_beats = float(intro_bars * bpb)
    notes, beat = [], intro_beats
    for i, item in enumerate(melody):
        beats = float(item.get("beats", 1.0))
        if beats <= 0:
            raise EpisodeError(f"melody[{i}] has non-positive beats")
        midi = note_to_midi(item.get("note"))
        notes.append(Note(i, midi, beats, str(item.get("syllable") or ""), beat,
                          beat * beat_sec, beats * beat_sec))
        beat += beats
    melody_beats = beat - intro_beats
    melody_bars = math.ceil(melody_beats / bpb - 1e-9)
    total_beats = float((intro_bars + melody_bars + outro_bars) * bpb)
    key = str(ep.get("key", "C major"))
    lines = group_lines(notes, list(ep.get("lyrics_lines") or []), bpb, intro_beats)
    chords = choose_chords(notes, key, bpb, intro_bars, melody_bars, outro_bars)

    raw_shots = sorted(ep.get("shots") or [{"start_beat": 0, "prompt": ep.get("title", "")}],
                       key=lambda s: float(s.get("start_beat", 0)))
    starts = [0.0] + [intro_beats + float(s.get("start_beat", 0)) for s in raw_shots[1:]]
    ends = starts[1:] + [total_beats]
    shots = []
    for i, (s, a, b) in enumerate(zip(raw_shots, starts, ends, strict=True)):
        if b - a <= 0:
            raise EpisodeError(f"shot {i} has no duration (duplicate start_beat?)")
        prompt = str(s.get("prompt", "")).strip()
        seed = int(s["seed"]) if "seed" in s else zlib.crc32(f"{i}:{prompt}".encode()) & 0x7FFFFFFF
        shots.append(Shot(i, a, b, a * beat_sec, b * beat_sec, prompt, str(s.get("lyric", "")),
                          str(s.get("shot", "")), seed))
    title = str(ep.get("title", "Untitled song"))
    return Timeline(title=title, slug=str(ep.get("slug") or slugify(title)), key=key, bpm=bpm,
                    beats_per_bar=bpb, intro_beats=intro_beats, melody_beats=melody_beats,
                    total_beats=total_beats, notes=notes, lines=lines, shots=shots,
                    chords=chords, outro_bars=outro_bars, raw=ep)


def load(path: Path) -> Timeline:
    try:
        ep = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise EpisodeError(f"Cannot read episode {path}: {exc}") from exc
    return build_timeline(ep)


def frame_boundaries(tl: Timeline, fps: int = FPS) -> list[int]:
    """Shot boundary frame indices on the global timeline (first 0, last = total frames)."""
    return [round(s.start * fps) for s in tl.shots] + [round(tl.total_sec * fps)]
