"""Offline tests for scripts/kids_song (nursery-song video tool). No models, no network.

The assembly test renders a tiny mock-clip / mock-vocal episode with ffmpeg at 1/10 scale.
"""

import itertools
import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from scripts.kids_song import assemble, clips, episode, music, vocal

FIXTURE = Path(__file__).parent / "fixtures" / "kids_song_episode.json"
HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")


def tiny_episode(**extra) -> dict:
    ep = {
        "title": "Tiny Hop", "bpm": 120, "key": "G major", "intro_bars": 1, "outro_bars": 1,
        "melody": [
            {"note": "G4", "beats": 1, "syllable": "Hop"},
            {"note": "B4", "beats": 1, "syllable": "py"},
            {"note": "D5", "beats": 1, "syllable": "frog"},
            {"note": "R", "beats": 1, "syllable": ""},
            {"note": "C5", "beats": 1, "syllable": "jump"},
            {"note": "A4", "beats": 1, "syllable": "and"},
            {"note": "F#4", "beats": 2, "syllable": "hop!"},
        ],
        "lyrics_lines": ["Happy frog", "jump and hop!"],
        "shots": [{"start_beat": 0, "prompt": "a frog", "lyric": "Happy frog"},
                  {"start_beat": 4, "prompt": "a pond", "lyric": "jump and hop!"}],
    }
    ep["melody"][0]["syllable"] = "Hap"
    ep.update(extra)
    return ep


# ---------------------------------------------------------------- pitch / key maths


def test_note_and_key_parsing():
    assert episode.note_to_midi("C4") == 60
    assert episode.note_to_midi("F#3") == 54
    assert episode.note_to_midi("Bb4") == 70
    assert episode.note_to_midi("R") is None and episode.note_to_midi(None) is None
    assert abs(episode.midi_to_hz(69) - 440.0) < 1e-9
    assert episode.parse_key("A minor") == (9, "minor")
    assert episode.parse_key("Am") == (9, "minor")
    assert episode.parse_key("Bb") == (10, "major")
    with pytest.raises(episode.EpisodeError):
        episode.note_to_midi("H2")


def test_chord_derivation_major_and_minor():
    c = episode.triads("C major")
    assert c["I"] == (0, (0, 4, 7)) and c["IV"] == (5, (5, 9, 0)) and c["V"] == (7, (7, 11, 2))
    a = episode.triads("A minor")
    assert a["I"][1] == (9, 0, 4) and a["IV"][1] == (2, 5, 9)
    assert a["V"][1] == (4, 8, 11)  # harmonic-minor dominant E major


def test_chords_follow_melody_and_resolve():
    ep = tiny_episode(melody=[
        {"note": n, "beats": 1, "syllable": s} for n, s in
        [("C5", "a"), ("E5", "b"), ("C5", "c"), ("A4", "d"),  # IV of G (C E G)
         ("D5", "e"), ("F#4", "f"), ("A4", "g"), ("D5", "h"),  # V of G (D F# A)
         ("G4", "i"), ("B4", "j"), ("D5", "k"), ("G4", "l")]], lyrics_lines=[])
    tl = episode.build_timeline(ep)
    assert tl.chords == ["V", "IV", "V", "I", "I"]  # intro ends on V, outro on I


# ---------------------------------------------------------------- timing maths


def test_fixture_timeline_maths():
    tl = episode.load(FIXTURE)
    beat = 60 / 104
    assert tl.intro_beats == 8 and tl.melody_beats == 32 and tl.total_beats == 48
    assert tl.total_sec == pytest.approx(48 * beat)
    assert tl.notes[0].start == pytest.approx(8 * beat)
    assert tl.notes[6].dur == pytest.approx(2 * beat)
    assert [s.start for s in tl.shots] == pytest.approx([0, 16 * beat, 24 * beat, 32 * beat])
    assert tl.shots[-1].end == pytest.approx(tl.total_sec)
    assert len(tl.lines) == 4 and tl.lines[1].start == pytest.approx(16 * beat)
    assert len(tl.chords) == 12 and tl.chords[-1] == "I"


def test_lines_pieces_keep_punctuation_and_fallback():
    tl = episode.build_timeline(tiny_episode())
    assert [ln.text for ln in tl.lines] == ["Happy frog", "jump and hop!"]
    assert tl.lines[0].pieces == [("Hap", 0), ("py", 1), (" ", None), ("frog", 2)]
    assert tl.lines[1].pieces[-1] == ("hop!", 6)
    # wrong lyric lines -> 2-bar phrase fallback built from the syllables
    tl2 = episode.build_timeline(tiny_episode(lyrics_lines=["something else"]))
    assert "".join(p for p, _ in tl2.lines[0].pieces).startswith("Hap py frog")


def test_fastwan_frame_counts():
    assert clips.frames_for(0.2) == 5
    assert clips.frames_for(1.0) == 17
    assert clips.frames_for(2.0) == 33
    assert clips.frames_for(9.0) == 49
    for s in (0.3, 1.1, 2.6, 3.0):
        f = clips.frames_for(s)
        assert (f - 1) % 4 == 0 and (f >= s * 16 or f == 49)


def test_edit_plan_frames_add_up():
    tl = episode.load(FIXTURE)
    plan = assemble.plan_edit(tl)
    total = round(tl.total_sec * 30)
    # every inner boundary is on a bar here -> 12-frame crossfades
    assert [p["fade_in"] for p in plan] == [0, 12, 12, 12]
    assert sum(p["need_frames"] for p in plan) - sum(p["fade_in"] for p in plan) == total
    for p in plan[1:]:
        assert p["offset"] == pytest.approx((p["start_frame"] - 6) / 30)


def test_off_bar_shot_gets_a_cut():
    tl = episode.build_timeline(tiny_episode(shots=[{"start_beat": 0, "prompt": "a"},
                                                    {"start_beat": 2, "prompt": "b"}]))
    assert assemble.plan_edit(tl)[1]["fade_in"] == assemble.CUT_FADE


# ---------------------------------------------------------------- captions


def test_caption_timing_tracks_syllables():
    tl = episode.build_timeline(tiny_episode())
    events = assemble.caption_events(tl)
    for (a0, b0, *_), (a1, *_r) in itertools.pairwise(events):
        assert a0 < b0 <= a1 + 1e-9  # ordered, never overlapping
    current = {cur: (a, b) for a, b, _, cur in events if cur is not None and cur >= 0}
    for idx in (0, 1, 2, 4, 5, 6):
        assert current[idx][0] == pytest.approx(tl.notes[idx].start)
    assert assemble.ass_time(61.234) == "0:01:01.23"
    ass = assemble.build_ass(tl, clips.VERTICAL)
    assert "PlayResY: 1920" in ass and assemble.CURRENT in ass and "Tiny Hop" in ass
    marked = assemble.line_markup(tl, 0, 1, 114)
    assert f"{{\\1c{assemble.CURRENT}\\fscx114\\fscy114}}py" in marked


# ---------------------------------------------------------------- audio


def test_music_is_deterministic_and_exact_length():
    tl = episode.build_timeline(tiny_episode())
    a = music.render(tl, sr=16000)
    b = music.render(tl, sr=16000)
    assert a.shape == (round(tl.total_sec * 16000), 2)
    assert np.array_equal(a, b)
    assert np.abs(a).max() == pytest.approx(music.PEAK, abs=1e-4)


def test_psola_hits_target_pitch_and_length():
    librosa = pytest.importorskip("librosa")
    sr = 16000
    t = np.arange(int(0.4 * sr)) / sr
    x = sum(np.sin(2 * np.pi * 200 * k * t) / k for k in range(1, 8)) * 0.3  # "speech" at 200
    f0, vo = vocal.f0_track(x, sr, 256)
    target = episode.midi_to_hz(67)  # G4 ~392 Hz
    y = vocal.psola(x, sr, f0, vo, 256, int(0.8 * sr), target)
    assert len(y) == int(0.8 * sr)
    g, gv, _ = librosa.pyin(y, fmin=100, fmax=800, sr=sr, frame_length=1024, hop_length=256)
    got = np.nanmedian(g[gv][3:-3])
    assert abs(12 * np.log2(got / target)) < 0.5


def test_mock_vocal_and_ducked_mix():
    tl = episode.build_timeline(tiny_episode())
    v, rep = vocal.render_voice(tl, None, "mock", sr=16000)
    m = music.render(tl, sr=16000)
    assert rep["mode"] == "mock" and len(v) == len(m)
    mix, duck = vocal.duck_and_mix(m, v, sr=16000)
    assert mix.shape == m.shape and np.abs(mix).max() <= music.PEAK + 1e-4
    assert 0 < duck["ducked_fraction"] < 1


def test_octave_choice():
    tl = episode.load(FIXTURE)  # melody median ~G4
    assert vocal.octave_shift(tl, 196.0) == -12  # G3 speaker: +12 needed -> sing an octave down
    assert vocal.octave_shift(tl, 330.0) == 0
    assert vocal.octave_shift(tl, None) == 0


# ---------------------------------------------------------------- end to end (mock)


def test_dry_run_prints_fastwan_plan(tmp_path, capsys):
    from scripts.kids_song import make

    assert make.main(["--episode", str(FIXTURE), "--out", str(tmp_path / "o"),
                      "--dry-run"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert len(plan["fastwan"]) == 4 and not (tmp_path / "o").exists()
    cmd = plan["fastwan"][0]["command"]
    assert "run_guarded.py" in cmd[1] and "--preset" in cmd and "fastwan-t2v" in cmd
    assert cmd[cmd.index("--frames") + 1] == "49"


@needs_ffmpeg
def test_mock_clip_assembly(tmp_path):
    from scripts.kids_song import make

    ep_path = tmp_path / "ep.json"
    ep_path.write_text(json.dumps(tiny_episode()))
    out = tmp_path / "out"
    assert make.main(["--episode", str(ep_path), "--out", str(out), "--mock-clips",
                      "--mock-vocal", "--scale", "0.1"]) == 0
    run = json.loads((out / "run.json").read_text())
    assert run["made_for_kids"] is True and run["approved"] is False
    assert run["published"] is False and run["do_not_publish"] is True
    tl = episode.build_timeline(tiny_episode())
    frames = round(tl.total_sec * 30)
    for name, (w, h) in (("video.mp4", (108, 192)), ("video-16x9.mp4", (192, 108))):
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,nb_read_frames", "-of", "csv=p=0",
             str(out / name)], capture_output=True, text=True, check=True).stdout.strip()
        assert probe.split(",") == [str(w), str(h), str(frames)]
    final = run["audio"]["final"]
    assert abs(final["integrated_lufs"] + 14) < 1.0 and final["clipped_samples"] == 0
    assert "MOCK CLIPS" in (out / "captions-9x16.ass").read_text()
