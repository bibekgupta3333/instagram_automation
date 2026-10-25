"""Layered-scene kids renderer: scene plan, character motion, scene render and song mode.

All offline and fast: synthetic RGBA layers, a synthetic sung WAV and words.json, tiny
frame sizes. ffmpeg tests are skipped when ffmpeg is missing.
"""

import json
import math
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from scripts.kids_song import assemble, episode, render_scene, scenes
from scripts.kids_song import character_motion as cm

REPO = Path(__file__).resolve().parents[1]
EP01 = REPO / "docs" / "2-channels" / "27-plinkety-pond-episode-01.json"
HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")


def tiny_episode() -> dict:
    return {
        "title": "Tiny Hop", "bpm": 120, "key": "G major", "intro_bars": 1, "outro_bars": 1,
        "melody": [
            {"note": "G4", "beats": 1, "syllable": "Hap"},
            {"note": "B4", "beats": 1, "syllable": "py"},
            {"note": "D5", "beats": 1, "syllable": "frog"},
            {"note": "R", "beats": 1, "syllable": ""},
            {"note": "C5", "beats": 1, "syllable": "jump"},
            {"note": "A4", "beats": 1, "syllable": "and"},
            {"note": "F#4", "beats": 2, "syllable": "hop!"},
        ],
        "lyrics_lines": ["Happy frog", "jump and hop!"],
        "shots": [{"start_beat": 0, "shot": "medium", "lyric": "Happy frog",
                   "prompt": "Pebble and Bloop sit on a rug. Slow gentle push-in."},
                  {"start_beat": 4, "shot": "wide", "lyric": "jump and hop!",
                   "prompt": "Mum, Fern and Pebble in a bathroom. Slow gentle pan left."}],
    }


def probe(path: Path) -> list[str]:
    return subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries",
         "stream=width,height,nb_read_frames", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True).stdout.strip().split(",")


def write_layer(path: Path, color, w=60, h=90, mouth=False, eyes_closed=False) -> None:
    import cv2

    img = np.zeros((h, w, 4), np.uint8)
    cv2.ellipse(img, (w // 2, h // 2), (w // 2 - 4, h // 2 - 4), 0, 0, 360, (*color, 255), -1)
    if mouth:
        cv2.circle(img, (w // 2, h // 3), 5, (0, 0, 120, 255), -1)
    if eyes_closed:
        cv2.line(img, (w // 3, h // 4), (2 * w // 3, h // 4), (0, 0, 0, 255), 2)
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), img)


def make_pack(root: Path) -> Path:
    colors = {"pebble": (60, 130, 200), "mum": (110, 120, 240), "fern": (200, 160, 210),
              "bloop": (150, 230, 150)}
    for name, col in colors.items():
        d = root / name
        for view in ("front", "three_quarter", "side"):
            write_layer(d / f"{view}.png", col)
        write_layer(d / "expr_singing.png", col, mouth=True)
        write_layer(d / "expr_eyes_closed.png", col, eyes_closed=True)
    return root


# ---------------------------------------------------------------- character motion


def test_squash_stretch_preserves_area():
    for ph in np.linspace(0, 1, 37):
        sx, sy = cm.squash_stretch(float(ph), 0.06)
        assert math.isclose(sx * sy, 1.0, rel_tol=1e-12)
    sx0, sy0 = cm.squash_stretch(0.0, 0.06)
    sxm, sym = cm.squash_stretch(0.5, 0.06)
    assert sy0 < 1 < sx0 and sym > 1 > sxm  # squashed on landing, stretched mid-hop
    # full pose: only the tiny breathing term moves the area away from 1
    areas = [cm.pose_at("pebble", t, 96).area for t in np.linspace(0, 5, 151)]
    assert max(abs(a - 1) for a in areas) < 0.02


def test_bob_is_on_the_beat_grid():
    bpm, off = 96, 0.37
    beat = 60 / bpm
    for k in range(6):
        ph = cm.beat_phase(off + k * beat, bpm, off)
        assert min(ph, 1 - ph) < 1e-9 and abs(cm.bob(ph, 1.0)) < 1e-9  # lands on beats
        assert math.isclose(cm.bob(cm.beat_phase(off + (k + 0.5) * beat, bpm, off), 1.0), 1.0)
    # each character's whole pose repeats with its hop period (Mum hops every 2 beats)
    for name, period in (("pebble", beat), ("bloop", beat), ("mum", 2 * beat)):
        a = cm.pose_at(name, 2.0, bpm, off)
        b = cm.pose_at(name, 2.0 + period, bpm, off)
        assert math.isclose(a.lift, b.lift, abs_tol=1e-9) and math.isclose(a.phase, b.phase)
    assert cm.pose_at("bloop", 1.3, bpm).lift >= 0


def test_blink_schedule_and_layer_track():
    starts = cm.blink_schedule(30.0, "pebble", fps=30)
    assert starts == cm.blink_schedule(30.0, "pebble", fps=30)  # deterministic
    assert starts != cm.blink_schedule(30.0, "fern", fps=30)
    gaps = np.diff(starts) / 30
    assert len(starts) >= 7 and gaps.min() >= 2.0 - 0.05 and gaps.max() <= 4.0 + 0.05
    # a shot starting at t0 sees the same song-time blinks
    later = cm.blink_schedule(10.0, "pebble", fps=30, t0=12.0)
    assert [s + 360 for s in later] == [s for s in starts if 360 <= s < 660]
    mask = cm.blink_mask(900, starts)
    assert mask.sum() == cm.BLINK_FRAMES * len(starts)
    track = cm.layer_track(900, mask, np.zeros(900, bool), True, True)
    assert track[starts[0]:starts[0] + 3] == ["blink"] * 3 and track[starts[0] + 3] == "base"
    assert set(cm.layer_track(900, mask, np.zeros(900, bool), False, True)) == {"base"}


def test_mouth_switches_with_word_timings():
    words = [(1.0, 1.4), (1.4, 1.9), (3.0, 3.5)]
    mouth = cm.mouth_from_words(words, 150, t0=0.5, fps=30)
    f = lambda t: round((t - 0.5) * 30)
    assert mouth[f(1.2)] and mouth[f(1.7)] and mouth[f(3.2)]
    assert not mouth[f(2.4)] and not mouth[f(0.6)] and not mouth[f(4.2)]
    assert not mouth[f(1.4) - 2]  # back-to-back words get a closing gap (after the lead)
    track = cm.layer_track(150, np.ones(150, bool), mouth, True, True)
    assert track[f(1.2)] == "singing" and track[f(2.4)] == "blink"


def test_contact_shadow_darkens_under_feet_and_fades_when_lifted():
    frame = np.full((60, 80, 3), 200, np.float32)
    cm.draw_shadow(frame, 40, 40, 30, lift=0.0)
    grounded = 200 - frame[40, 40, 0]
    frame2 = np.full((60, 80, 3), 200, np.float32)
    cm.draw_shadow(frame2, 40, 40, 30, lift=1.0)
    assert grounded > 30 and 0 < 200 - frame2[40, 40, 0] < grounded
    assert frame[5, 5, 0] == 200


def test_feet_anchor_stays_put_under_squash():
    rgba = np.zeros((100, 50, 4), np.uint8)
    rgba[10:90, 10:40] = (90, 90, 200, 255)
    layer = cm.prepare_layer(rgba, 1.0)
    cam = np.array([[1.0, 0, 0], [0, 1.0, 0]])
    for sy in (0.9, 1.1):
        pose = cm.Pose(1 / sy, sy, 0.0, 0.0, 0.0, 0.0)
        m = cm.character_matrix(layer, pose, 100.0, 150.0, 80.0, cam)
        fx, fy = m @ np.array([*layer.anchor, 1.0])
        assert math.isclose(fx, 100.0, abs_tol=1e-6) and math.isclose(fy, 150.0, abs_tol=1e-6)


# ---------------------------------------------------------------- scene plan


def test_scene_plan_scale_and_z_order(tmp_path):
    tl = episode.load(EP01)
    sc = scenes.plan_scene(tl.shots[14], tl, make_pack(tmp_path / "chars"), tmp_path / "bg")
    by = {c.name: c for c in sc.characters}
    assert set(by) == {"Pebble", "Fern", "Mum", "Bloop"}
    assert by["Mum"].height > by["Fern"].height > by["Pebble"].height > by["Bloop"].height
    assert sc.characters[0].name == "Mum"  # tallest is drawn first (behind)
    assert by["Bloop"].z > by["Pebble"].z > by["Fern"].z > by["Mum"].z
    assert [c.z for c in sc.characters] == sorted(c.z for c in sc.characters)
    assert by["Pebble"].singing and not by["Mum"].singing
    assert by["Pebble"].source == "pack" and "singing" in by["Pebble"].layers
    assert sc.background is None and sc.mock  # no plate -> placeholder, do not publish
    # cutaway: no characters; camera from the prompt
    assert scenes.plan_scene(tl.shots[3], tl, tmp_path / "chars", tmp_path / "bg").characters \
        == []
    assert scenes.camera_for(tl.shots[0]) == "push-in"
    assert scenes.camera_for(tl.shots[1]) == "pan-right"


def test_render_overrides_pack_and_plate_matching(tmp_path):
    tl = episode.load(EP01)
    chars = make_pack(tmp_path / "chars")
    bgdir = tmp_path / "bg"
    write_layer(bgdir / "otter-bathroom-9x16.png", (200, 200, 150))
    write_layer(bgdir / "otter-hallway-9x16.png", (150, 200, 220))
    renders = tmp_path / "renders"
    write_layer(renders / "shot-06-pebble.png", (10, 10, 250))
    sc = scenes.plan_scene(tl.shots[6], tl, chars, bgdir, renders)
    peb = next(c for c in sc.characters if c.name == "Pebble")
    mum = next(c for c in sc.characters if c.name == "Mum")
    assert peb.source == "render" and peb.base.endswith("shot-06-pebble.png")
    assert peb.layers == {}  # no matching render variants: no expression swaps
    assert mum.source == "pack" and not sc.mock
    assert sc.background.endswith("otter-bathroom-9x16.png")
    assert scenes.plan_scene(tl.shots[1], tl, chars, bgdir).background.endswith(
        "otter-hallway-9x16.png")
    assert render_scene.format_variant(sc.background, 1920, 1080) == sc.background
    write_layer(bgdir / "otter-bathroom-16x9.png", (200, 200, 150))
    assert render_scene.format_variant(sc.background, 1920, 1080).endswith("-16x9.png")


# ---------------------------------------------------------------- scene render


@needs_ffmpeg
@pytest.mark.parametrize("size", [(108, 192), (192, 108)])
def test_render_scene_dimensions_and_frames(tmp_path, size):
    tl = episode.build_timeline(tiny_episode())
    sc = scenes.plan_scene(tl.shots[1], tl, make_pack(tmp_path / "chars"), tmp_path / "bg")
    out = tmp_path / "shot.mp4"
    rep = render_scene.render_scene(sc, size, 23, out, t0=4.0, bpm=tl.bpm,
                                    words=[(4.0, 4.4), (4.5, 5.0)],
                                    frames_png=tmp_path / "png", png_every=11)
    assert probe(out) == [str(size[0]), str(size[1]), "23"]
    assert rep["frames"] == 23 and len(list((tmp_path / "png").glob("*.png"))) == 3


def test_render_frames_move_and_characters_are_visible(tmp_path):
    tl = episode.build_timeline(tiny_episode())
    sc = scenes.plan_scene(tl.shots[1], tl, tmp_path / "none", tmp_path / "none")
    frames = list(render_scene.render_frames(sc, 96, 160, 12, t0=4.0, bpm=tl.bpm))
    assert len(frames) == 12 and frames[0].shape == (160, 96, 3)
    assert np.abs(frames[0].astype(int) - frames[6].astype(int)).mean() > 0.5  # it moves
    again = list(render_scene.render_frames(sc, 96, 160, 12, t0=4.0, bpm=tl.bpm))
    assert all(np.array_equal(a, b) for a, b in zip(frames, again, strict=True))


# ---------------------------------------------------------------- song mode


def write_song(song_dir: Path, seconds: float, first_word: float) -> list[dict]:
    import soundfile as sf

    sr = 48000
    t = np.arange(int(seconds * sr)) / sr
    rng = np.random.default_rng(1)
    audio = 0.2 * np.sin(2 * np.pi * 330 * t) + 0.02 * rng.standard_normal(t.size)
    song_dir.mkdir(parents=True, exist_ok=True)
    sf.write(str(song_dir / "best.wav"), np.stack([audio, audio], 1).astype(np.float32), sr)
    names = ["HAPPY", "FROG", "JUMP", "AND", "HOP"]
    offs = [0.0, 0.5, 2.0, 2.5, 3.0]
    words = [{"word": w, "start": round(first_word + o, 3), "end": round(first_word + o + 0.4, 3),
              "score": 0.9} for w, o in zip(names, offs, strict=True)]
    words[3] = {"word": "AND", "start": 0.0, "end": 0.0, "score": 0.0}  # unaligned
    (song_dir / "words.json").write_text(json.dumps({"words": words}))
    return words


def test_song_timeline_shift_trim_and_title_card(tmp_path):
    tl = episode.build_timeline(tiny_episode())  # 8 s, first note at 2.0 s, shot 1 at 4.0 s
    write_song(tmp_path / "s", 7.0, 2.2)
    words = assemble.load_words(tmp_path / "s" / "words.json", 7.0)
    assert words[2].end <= words[3].start < words[3].end <= words[4].start  # interpolated
    new, info = assemble.song_timeline(tl, 7.0, words)
    assert [s.index for s in new.shots] == [-1, 0, 1] and info["title_card"]
    assert new.shots[0].end == 1.0 and new.shots[1].start == 1.0
    assert math.isclose(new.shots[2].start, 4.2) and new.shots[2].end == 7.0
    assert math.isclose(new.total_sec, 7.0) and math.isclose(info["beat_shift"], 0.2)
    assert info["last_shot_delta"] < 0  # trimmed to the shorter song
    longer, info2 = assemble.song_timeline(tl, 9.5, words)
    assert longer.shots[-1].end == 9.5 and info2["last_shot_delta"] > 0  # stretched
    no_intro = [assemble.Word(w.text, w.start - 1.6, w.end - 1.6) for w in words]
    plain, info3 = assemble.song_timeline(tl, 7.0, no_intro)
    assert not info3["title_card"] and plain.shots[0].index == 0
    frames = episode.frame_boundaries(new)
    assert frames[-1] == 210 and frames == sorted(frames)


def test_song_captions_are_word_level(tmp_path):
    tl = episode.build_timeline(tiny_episode())
    write_song(tmp_path / "s", 7.0, 2.2)
    words = assemble.load_words(tmp_path / "s" / "words.json", 7.0)
    lines = assemble.group_words(words, tl.raw["lyrics_lines"])
    assert [[t for t, _ in ln] for ln in lines] == [["Happy", "frog"], ["jump", "and", "hop!"]]
    from scripts.kids_song import clips

    ass = assemble.build_song_ass(tl, words, clips.VERTICAL)
    assert "Fredoka" in ass or "Arial Rounded" in ass
    assert f"{{\\1c{assemble.CURRENT}\\fscx114\\fscy114}}Happy" in ass
    assert "Dialogue: 1,0:00:00.10" in ass and "Tiny Hop" in ass  # title over the intro
    assert assemble.display_tokens("Bye-bye, bubbles,") == ["Bye-", "bye,", "bubbles,"]
    assert assemble.display_tokens("Pop! ... pop!") == ["Pop! ...", "pop!"]


@needs_ffmpeg
def test_song_mode_assembly(tmp_path):
    from scripts.kids_song import make

    ep_path = tmp_path / "ep.json"
    ep_path.write_text(json.dumps(tiny_episode()))
    out = tmp_path / "out"
    write_song(out / "song", 7.0, 2.2)
    chars = make_pack(tmp_path / "chars")
    assert make.main(["--episode", str(ep_path), "--out", str(out), "--song",
                      "--characters", str(chars), "--backgrounds", str(tmp_path / "bg"),
                      "--scale", "0.1"]) == 0
    run = json.loads((out / "run.json").read_text())
    assert run["mode"] == "song" and run["visuals"] == "scenes"
    assert run["made_for_kids"] is True and run["approved"] is False
    assert run["published"] is False and run["ai_disclosure"] is True
    assert run["do_not_publish"] is True  # placeholder background plate
    assert run["audio"]["vocal_mode"] == "song" and run["song"]["title_card"]
    assert [s["shot"] for s in run["scenes"]] == [-1, 0, 1]
    assert run["seconds"] == 7.0
    for name, (w, h) in (("video.mp4", (108, 192)), ("video-16x9.mp4", (192, 108))):
        assert probe(out / name) == [str(w), str(h), "210"]
    final = run["audio"]["final"]
    assert abs(final["integrated_lufs"] + 14) < 1.0 and abs(final["seconds"] - 7.0) < 0.01
    assert not (out / "audio" / "music.wav").exists()  # synth music skipped
    ass = (out / "captions-9x16.ass").read_text()
    assert "hop!" in ass and "PLACEHOLDER ART" in ass
    # cached: a second run renders nothing new
    before = sorted(p.name for p in (out / "scenes").rglob("*.mp4"))
    assert make.main(["--episode", str(ep_path), "--out", str(out), "--song",
                      "--characters", str(chars), "--backgrounds", str(tmp_path / "bg"),
                      "--scale", "0.1"]) == 0
    assert sorted(p.name for p in (out / "scenes").rglob("*.mp4")) == before


def test_song_mode_dry_run_and_missing_song(tmp_path, capsys):
    from scripts.kids_song import make

    ep_path = tmp_path / "ep.json"
    ep_path.write_text(json.dumps(tiny_episode()))
    with pytest.raises(SystemExit):
        make.main(["--episode", str(ep_path), "--out", str(tmp_path / "o"), "--song"])
    write_song(tmp_path / "song", 7.0, 2.2)
    assert make.main(["--episode", str(ep_path), "--out", str(tmp_path / "o"), "--song",
                      str(tmp_path / "song"), "--dry-run"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["song"]["title_card"] and len(plan["scenes"]) == 3
    assert not (tmp_path / "o").exists()
