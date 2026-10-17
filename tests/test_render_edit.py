"""Retention edit (render) and Shorts-style captions/overlays: shot pacing, xfade transitions,
music bed + ducking + whooshes, caption chunking/safe area and beat overlay cards. Offline:
synthetic stills, tones and noise built with ffmpeg; no models."""

import itertools
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from studio.config import DEFAULT_BRAND_DIR, Settings, load_brand
from studio.contracts import Beat, Script, VideoJob
from studio.pipeline.mock_llm import MockChatClient
from studio.stages import captions, render
from studio.stages.base import StageContext

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
FPS = render.FPS


@pytest.fixture(autouse=True)
def _no_user_assets(tmp_path, monkeypatch):
    """Never pick up the developer's real assets/ (music, fonts) in tests."""
    monkeypatch.setenv("STUDIO_ASSETS_DIR", str(tmp_path / "no-assets"))
    monkeypatch.delenv("STUDIO_MUSIC", raising=False)
    monkeypatch.delenv("STUDIO_MUSIC_MOOD", raising=False)
    monkeypatch.delenv("STUDIO_SFX", raising=False)
    monkeypatch.setenv("STUDIO_MOTION", "zoompan")
    monkeypatch.setattr(render, "ENCODERS", [render.ENCODERS[-1]])  # CPU only


def brand(slug="gravity-what-if"):
    return load_brand(slug, DEFAULT_BRAND_DIR)


def ff(*args):
    threads = str(max(1, int(os.environ.get("STUDIO_CPU_THREADS", "3"))))
    bounded = []
    for arg in args:
        if arg == "-i":
            bounded += ["-threads", threads]
        bounded.append(arg)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-filter_threads", threads, "-filter_complex_threads", threads,
                    *bounded[:-1], "-threads", threads, bounded[-1]], check=True)


TIMINGS = {"segments": [
    {"kind": "hook", "beat": 0, "start": 0.0, "end": 2.0, "text": "What if Earth had rings?"},
    {"kind": "beat", "beat": 0, "start": 2.1, "end": 7.0,
     "text": "They would weigh forty thousand tons per metre."},
    {"kind": "beat", "beat": 1, "start": 7.1, "end": 12.0,
     "text": "Here is the part most explanations skip entirely."},
    {"kind": "beat", "beat": 2, "start": 12.1, "end": 13.4, "text": "Night would glow."},
    {"kind": "cta", "beat": 2, "start": 13.5, "end": 16.0, "text": "Part two tomorrow."},
]}


# ---------------------------------------------------------------- pacing + transitions


def test_pacing_fast_start_and_a_change_every_2_to_3_s():
    shots = render.plan_shots(TIMINGS, 3, 16.0, seed="job-a")
    assert sum(s["frames"] for s in shots) == 16 * FPS
    assert shots[0]["frames"] <= 1.5 * FPS  # the first cut lands inside 1.5 s
    assert all(s["frames"] <= render.MAX_SHOT_S * FPS + 1 for s in shots)
    assert all(s["frames"] >= render.MIN_SHOT_S * FPS - 1 for s in shots)
    assert all(a["motion"] != b["motion"] for a, b in itertools.pairwise(shots))
    # inside a block, alternate shots are punch-ins of the same image
    for _, grp in itertools.groupby(shots, key=lambda s: s["block"]):
        grp = list(grp)
        assert len({s["image"] for s in grp}) == 1
        assert [s["punch"] for s in grp] == [j % 2 == 1 for j in range(len(grp))]


def test_transitions_only_between_beats_and_varied():
    shots = render.plan_shots(TIMINGS, 3, 16.0, seed="job-a")
    starts = [s for s in shots if "xfade" in s]
    firsts = [g[0] for g in (list(x) for _, x in itertools.groupby(shots, key=lambda s: s["block"]))]
    assert starts == firsts[1:]  # one transition into every block but the first
    assert all(s["xfade"] in render.TRANSITIONS for s in starts)
    assert all(4 <= s["xfade_frames"] <= 10 for s in starts)
    names = [n for n, _ in render.transition_sequence("job-a", 12)]
    assert all(a != b for a, b in itertools.pairwise(names))
    assert names != [n for n, _ in render.transition_sequence("job-b", 12)]


def test_short_block_shrinks_or_drops_the_transition():
    timings = {"segments": [{"beat": 0, "start": 0.0}, {"beat": 1, "start": 3.0},
                            {"beat": 2, "start": 3.2}]}
    shots = render.plan_shots(timings, 3, 6.0, seed="s")
    by_block = {s["block"]: s for s in shots if "xfade" in s}
    blk1 = sum(s["frames"] for s in shots if s["block"] == 1)  # 6 frames long
    assert blk1 == 6
    assert all(s.get("xfade_frames", 0) <= blk1 // 2 for s in by_block.values())


def test_xfade_offsets_are_centred_on_the_beat_boundary():
    shots = render.plan_shots(TIMINGS, 3, 16.0, seed="job-a")
    graph = render.video_graph(shots, 1080, 1920, "c.ass")
    boundaries = render.transition_times(shots)
    offsets = [float(x) for x in re.findall(r"xfade=transition=\w+:duration=[\d.]+:offset=([\d.]+)",
                                            graph)]
    frames = [s["xfade_frames"] for s in shots if "xfade" in s]
    assert len(offsets) == len(boundaries) == len(frames)
    for off, b, t in zip(offsets, boundaries, frames, strict=True):
        assert off == pytest.approx(b - (t - t // 2) / FPS, abs=1e-3)
    assert "tpad=start_mode=clone" in graph and graph.endswith("ass=c.ass[vout]")


def test_fonts_dir_is_quoted_into_the_ass_filter(tmp_path):
    shots = render.plan_shots(TIMINGS, 3, 16.0, seed="job-a")
    graph = render.video_graph(shots, 1080, 1920, "c.ass", fonts_dir=tmp_path)
    assert graph.endswith(f"ass=c.ass:fontsdir='{tmp_path}'[vout]")


# ---------------------------------------------------------------- music + sfx selection


def _assets(tmp_path, music=("cinematic",), sfx=True):
    root = tmp_path / "assets"
    for mood in music:
        (root / "music" / mood).mkdir(parents=True, exist_ok=True)
    if sfx:
        (root / "sfx").mkdir(parents=True, exist_ok=True)
    return root


def test_music_follows_brand_mood_and_is_stable(tmp_path, monkeypatch):
    root = _assets(tmp_path, music=("cinematic", "tech"))
    for name in ("a.mp3", "b.mp3", "c.mp3"):
        (root / "music" / "cinematic" / name).write_bytes(b"x")
    (root / "music" / "tech" / "t.mp3").write_bytes(b"x")
    (root / "music" / "cinematic" / "notes.txt").write_text("licence")
    monkeypatch.setenv("STUDIO_ASSETS_DIR", str(root))
    pick = render.pick_music(brand(), "job-1")
    assert pick.parent.name == "cinematic" and pick.suffix == ".mp3"
    assert render.pick_music(brand(), "job-1") == pick  # deterministic per job
    assert render.pick_music(brand("software-shortcut"), "job-1").name == "t.mp3"
    assert render.pick_music(brand("car-cost-truth"), "job-1") is None  # no upbeat folder
    monkeypatch.setenv("STUDIO_MUSIC_MOOD", "tech")
    assert render.pick_music(brand(), "job-1").name == "t.mp3"
    monkeypatch.setenv("STUDIO_MUSIC", "0")
    assert render.pick_music(brand(), "job-1") is None


def test_audio_graph_ducks_music_under_the_voice():
    g = render.audio_graph(5, 10.0, music_idx=6, music_gain_db=-9.5,
                           sfx=[(7, -3.0, [1.0, 4.5])])
    assert "[5:a]" in g and "asplit=2[voice][key]" in g
    assert "[6:a]" in g and "atrim=0:10.000" in g and "volume=-9.50dB" in g
    assert f"[bed][key]sidechaincompress={render.DUCK}[duck]" in g
    assert "adelay=1000:all=1" in g and "adelay=4500:all=1" in g
    assert "amix=inputs=4:duration=first:normalize=0" in g and g.endswith("[m]")
    voice_only = render.audio_graph(0, 10.0)
    assert "sidechain" not in voice_only and "amix" not in voice_only
    assert render.VOICE_CHAIN in voice_only and voice_only.endswith("[voice]anull[m]")


# ---------------------------------------------------------------- end to end


def _render_job(tmp_path, fmt="short", overlays=("8 MIN", "", "THE PART THEY SKIP")):
    ff("-f", "lavfi", "-i", "sine=frequency=330:duration=16", "-af", "volume=0.3",
       "-ar", "24000", "-ac", "1", str(tmp_path / "narration.wav"))
    imgs = tmp_path / "images"
    imgs.mkdir()
    for i, c in enumerate(("red", "green")):
        ff("-f", "lavfi", "-i", f"testsrc2=size=576x1024:d=1,drawbox=c={c}:t=40", "-frames:v",
           "1", "-update", "1", str(imgs / f"beat_0{i}.png"))
    # beat 2 is a landscape stock clip (with its own audio, which must stay muted)
    ff("-f", "lavfi", "-i", "testsrc=size=640x360:rate=25:duration=2", "-f", "lavfi",
       "-i", "sine=frequency=3000:duration=2", "-shortest", "-c:v", "libx264",
       "-pix_fmt", "yuv420p", "-c:a", "aac", str(imgs / "beat_02.mp4"))
    (tmp_path / "timings.json").write_text(json.dumps(TIMINGS))
    beats = [Beat(text=f"Beat {i}.", visual_prompt="v", overlay=o) for i, o in enumerate(overlays)]
    job = VideoJob(job_id="job-edit", channel="gravity-what-if", format=fmt, topic="t",
                   workdir=tmp_path, audio_path=tmp_path / "narration.wav",
                   script=Script(title="T", hook="H?", beats=beats, description="d"),
                   image_paths=[imgs / "beat_00.png", imgs / "beat_01.png", imgs / "beat_02.mp4"])
    ctx = StageContext(settings=Settings(output_dir=tmp_path, brand_dir=DEFAULT_BRAND_DIR),
                       chat=MockChatClient(), engines={"tts": "mock", "captions": "mock"})
    return job, ctx


def _probe(path):
    return json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-print_format", "json", "-show_streams",
         "-show_format", str(path)], capture_output=True, text=True, check=True).stdout)


def _lufs(path):
    err = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-threads",
                          os.environ.get("STUDIO_CPU_THREADS", "3"), "-i", str(path),
                          "-filter_threads", os.environ.get("STUDIO_CPU_THREADS", "3"), "-af",
                          "loudnorm=print_format=json", "-f", "null", "-"],
                         capture_output=True, text=True, check=True).stderr
    return float(json.loads(re.findall(r"\{[^{}]*\}", err)[-1])["input_i"])


@needs_ffmpeg
def test_render_with_transitions_music_and_whooshes(tmp_path, monkeypatch):
    root = _assets(tmp_path)
    ff("-f", "lavfi", "-i", "anoisesrc=d=6:c=pink:a=0.2", "-ac", "2",
       str(root / "music" / "cinematic" / "bed.wav"))  # shorter than the video: must loop
    ff("-f", "lavfi", "-i", "anoisesrc=d=0.4:c=white:a=0.5", str(root / "sfx" / "whoosh.wav"))
    monkeypatch.setenv("STUDIO_ASSETS_DIR", str(root))
    job, ctx = _render_job(tmp_path)
    job = captions.run(job, brand(), ctx)
    ass = Path(job.captions_path).read_text()
    assert ass.count(",Card,,") == 2  # two non-empty overlays
    job = render.run(job, brand(), ctx)

    plan = json.loads((tmp_path / "render_plan.json").read_text())
    assert sum(s["frames"] for s in plan) == 16 * FPS
    assert sum("xfade" in s for s in plan) == 2
    assert {s.get("kind") for s in plan} == {None, "clip"}  # stills and a clip mixed
    p = _probe(job.video_path)
    v = next(s for s in p["streams"] if s["codec_type"] == "video")
    assert [s["codec_type"] for s in p["streams"]].count("audio") == 1
    assert (v["width"], v["height"]) == (1080, 1920)
    assert abs(int(v["nb_read_frames"]) - 16 * FPS) <= 1  # transitions keep it frame-exact
    assert abs(float(p["format"]["duration"]) - 16.0) < 0.15
    assert _lufs(job.video_path) == pytest.approx(-14.0, abs=1.0)
    assert any("render: music bed.wav" in line for line in job.log)
    cache = json.loads((tmp_path / ".cache" / "render.json").read_text())
    assert cache["music"].endswith("bed.wav") and cache["sfx"] == ["whoosh.wav"]

    # turning music off changes the cache key, the video and therefore the content hash
    first = job.content_hash
    monkeypatch.setenv("STUDIO_MUSIC", "0")
    job = render.run(job, brand(), ctx)
    assert job.content_hash != first
    assert any("no music" in line for line in job.log)
    assert job.content_hash == render.compute_content_hash(job)


@needs_ffmpeg
def test_render_long_form_voice_only(tmp_path):
    job, ctx = _render_job(tmp_path, fmt="long")
    job = captions.run(job, brand(), ctx)
    job = render.run(job, brand(), ctx)
    v = next(s for s in _probe(job.video_path)["streams"] if s["codec_type"] == "video")
    assert (v["width"], v["height"]) == (1920, 1080)
    assert any("no music for moods" in line for line in job.log)


# ---------------------------------------------------------------- captions + overlays


def _words(text, start=0.0, step=0.3, gap_after=()):
    out, t = [], start
    for i, tok in enumerate(text.split()):
        out.append({"text": tok, "start": round(t, 3), "end": round(t + step - 0.02, 3)})
        t += step + (0.8 if i in gap_after else 0.0)
    return out


def _style(ass, name):
    line = next(x for x in ass.splitlines() if x.startswith(f"Style: {name},"))
    return line.split(":", 1)[1].split(",")


def _text(event):
    return re.sub(r"\{[^}]*\}", "", event.split(",", 9)[9])


def _events(ass, style):
    return [x for x in ass.splitlines() if x.startswith("Dialogue:") and f",{style},," in x]


def test_short_captions_chunk_highlight_and_safe_area(tmp_path):
    words = _words("What if Earth had rings like Saturn? Extraordinarily bright nights, "
                   "every single night", gap_after=(2,))
    captions.write_ass(words, brand(), "short", tmp_path / "c.ass")
    ass = (tmp_path / "c.ass").read_text()
    events = _events(ass, "Word")
    assert len(events) == len(words)  # one event per spoken word
    for e in events:
        text = _text(e)
        assert 1 <= len(text.split()) <= 3
        assert len(text) <= 15 or len(text.split()) == 1  # a lone long word may exceed
        assert text == text.upper()
    # the active word: accent colour + \t scale pop; chunks pop in
    accent = "&H0027B6FF"  # gravity-what-if solar #FFB627 in ASS BGR
    assert all(f"\\c{accent}" in e and "\\t(0,70,\\fscx124" in e for e in events)
    assert sum("\\fad(40,0)" in e for e in events) >= 4
    # a pause > 0.45 s starts a new chunk: "EARTH" ends one, "HAD" starts the next
    assert _text(events[3]).startswith("HAD")
    st = _style(ass, "Word")
    size, outline, shadow, align, margin_v = (int(st[2]), int(st[16]), int(st[17]), int(st[18]),
                                               int(st[21]))
    assert st[1] in ("Arial Black", "Helvetica Neue", "Impact") and st[7] == "-1"  # bold
    assert size >= 0.055 * 1920 and outline >= 8 and shadow >= 3 and align == 2
    assert margin_v >= 0.18 * 1920 and 1920 - margin_v - 2 * size > 0.10 * 1920


def test_long_form_captions_are_smaller_and_at_the_bottom(tmp_path):
    words = _words("This is a longer sentence that should sit in one or two lines at the "
                   "bottom of a landscape frame.")
    captions.write_ass(words, brand(), "long", tmp_path / "c.ass")
    ass = (tmp_path / "c.ass").read_text()
    st = _style(ass, "Word")
    assert int(st[2]) <= 0.055 * 1080 and int(st[18]) == 2 and int(st[21]) <= 0.08 * 1080
    events = _events(ass, "Word")
    assert all("\\t(" not in e for e in events)  # no pop on long-form
    texts = [_text(e) for e in events]
    assert max(len(t.split()) for t in texts) <= 8 and any(len(t.split()) > 3 for t in texts)


def test_overlay_cards_timed_to_beats_in_the_upper_third(tmp_path):
    beats = [Beat(text="a", visual_prompt="v", overlay="40 tons"),
             Beat(text="b", visual_prompt="v", overlay=""),
             Beat(text="c", visual_prompt="v", overlay="the part nobody explains")]
    script = Script(title="T", hook="H", beats=beats)
    ovs = captions.overlay_events(script, TIMINGS)
    assert [o["text"] for o in ovs] == ["40 tons", "the part nobody explains"]
    assert ovs[0]["start"] == pytest.approx(2.1 + captions.OVERLAY_DELAY_S)
    assert ovs[0]["end"] == pytest.approx(ovs[0]["start"] + captions.OVERLAY_MAX_S)
    assert ovs[1]["end"] == pytest.approx(13.4)  # a short beat ends its card early
    captions.write_ass(_words("one two"), brand(), "short", tmp_path / "c.ass", ovs)
    ass = (tmp_path / "c.ass").read_text()
    cards = _events(ass, "Card")
    assert len(cards) == 2
    for c in cards:
        y = int(re.search(r"\\pos\(\d+,(\d+)\)", c).group(1))
        assert 0.10 * 1920 <= y <= 1920 / 3 and "\\an8" in c
        assert "\\t(0,150,\\fscx110" in c and "\\fad(" in c
    assert "\\fs" in cards[0] and "40 TONS" in cards[0]  # numbers get a bigger card
    assert "\\N" in cards[1] and "THE PART" in cards[1]  # long text wraps to two lines
    st = _style(ass, "Card")
    assert st[15] == "3" and st[5] == "&H0027B6FF"  # opaque box in the brand accent


def test_captions_cache_key_tracks_overlays(tmp_path):
    job, ctx = _render_job(tmp_path) if HAS_FFMPEG else pytest.skip("ffmpeg needed")
    job = captions.run(job, brand(), ctx)
    assert any("2 overlays" in line for line in job.log)
    job.script.beats[1].overlay = "NEW CARD"
    job = captions.run(job, brand(), ctx)
    assert any("3 overlays" in line for line in job.log)
    assert Path(job.captions_path).read_text().count(",Card,,") == 3


def test_caption_font_env_and_bundled_font(tmp_path, monkeypatch):
    monkeypatch.setattr(captions, "FONT_ENV", "")
    assert captions.caption_font() in ("Arial Black", "Helvetica Neue", "Impact")
    monkeypatch.setattr(captions, "FONT_ENV", "Bebas Neue")
    assert captions.caption_font() == "Bebas Neue"


def test_measure_limits_threads_and_rejects_silence(monkeypatch):
    seen = []
    measurement = {"input_i": "-20", "input_tp": "-4", "input_lra": "2",
                   "input_thresh": "-30", "target_offset": "0"}

    def fake_run(cmd, **kwargs):
        seen.append(cmd)
        return type("Result", (), {"stderr": json.dumps(measurement)})()

    monkeypatch.setenv("STUDIO_CPU_THREADS", "2")
    monkeypatch.setattr(render.subprocess, "run", fake_run)
    assert render.measure(["-i", "voice.wav"], "[0:a]anull[m]") == measurement
    cmd = seen[0]
    assert cmd[cmd.index("-filter_complex_threads") + 1] == "2"
    assert cmd[cmd.index("-filter_threads") + 1] == "2"
    assert cmd[cmd.index("-i") - 2:cmd.index("-i")] == ["-threads", "2"]
    measurement["input_i"] = "-inf"
    assert render.measure(["-i", "voice.wav"], "[0:a]anull[m]") is None


def test_audio_selection_ignores_directories(tmp_path):
    (tmp_path / "folder.wav").mkdir()
    assert render._audio_files(tmp_path) == []


def test_caption_text_cannot_inject_ass_events(tmp_path):
    words = [{"text": "hi\nDialogue: {\\pos(0,0)}", "start": 0.0, "end": 0.3}]
    path = tmp_path / "safe.ass"
    captions.write_ass(words, brand(), "short", path)
    ass = path.read_text()
    assert len(_events(ass, "Word")) == 1
    assert "\\pos(0,0)" not in ass


def test_zero_length_words_do_not_create_invalid_ass_events(tmp_path):
    words = [{"text": "one", "start": 0.0, "end": 0.0},
             {"text": "two", "start": 0.0, "end": 0.3}]
    path = tmp_path / "words.ass"
    captions.write_ass(words, brand(), "short", path)
    events = _events(path.read_text(), "Word")
    assert len(events) == 1
    assert "ONE TWO" == _text(events[0])


@needs_ffmpeg
@pytest.mark.parametrize(("tts_engine", "images_engine", "requested"), [
    ("silence", "mock", "auto"),
    ("mock", "pixabay", "auto"),
    ("silence", "pixabay", "auto"),
    ("kokoro", "mock", "parallax"),
])
def test_placeholder_render_never_calls_depth_model(tmp_path, monkeypatch,
                                                    tts_engine, images_engine, requested):
    calls = []

    def forbidden_depth(*args, **kwargs):
        calls.append(1)
        raise AssertionError("offline placeholder render attempted to load the depth model")

    monkeypatch.setenv("STUDIO_MOTION", requested)
    monkeypatch.setattr(render.motion, "available", lambda: True)
    monkeypatch.setattr(render.motion, "depth_map", forbidden_depth)
    monkeypatch.setattr(render, "frame_size", lambda _: (180, 320))
    job, ctx = _render_job(tmp_path)
    ctx.engines.update(tts=tts_engine, images=images_engine)
    job = captions.run(job, brand(), ctx)
    render.run(job, brand(), ctx)
    assert calls == []
    assert any("motion=zoompan" in line for line in job.log)


@needs_ffmpeg
def test_hard_cut_then_crossfade_keeps_timebase_and_frame_count(tmp_path):
    timings = {"segments": [{"beat": i, "start": start}
                            for i, start in enumerate((0.0, 1.0, 1.2, 3.0))]}
    shots = render.plan_shots(timings, 4, 5.0, "mixed-cuts")
    inputs = []
    for shot in shots:
        shot["kind"] = "clip"
        inputs += ["-f", "lavfi", "-i", "color=red:s=180x320:r=30:d=3"]
    ass = tmp_path / "c.ass"
    captions.write_ass(_words("one two"), brand(), "short", ass)
    graph = render.video_graph(shots, 180, 320, str(ass))
    out = tmp_path / "cuts.mp4"
    ff(*inputs, "-filter_complex", graph, "-map", "[vout]", "-c:v", "libx264", str(out))
    video = next(s for s in _probe(out)["streams"] if s["codec_type"] == "video")
    assert int(video["nb_read_frames"]) == 5 * FPS


def test_parallax_cache_identity_tracks_resolved_shot_metadata(tmp_path, monkeypatch):
    image = tmp_path / "still.png"
    image.write_bytes(b"same image bytes")
    manifest = tmp_path / "visuals.json"
    job = VideoJob(job_id="motion-cache", channel="gravity-what-if", format="short",
                   topic="t", workdir=tmp_path)
    generated = []

    def fake_clip(image, out, seconds, fmt, preset, **kwargs):
        generated.append((out.name, preset, kwargs["shot"]))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"clip")

    monkeypatch.setattr(render.motion, "parallax_clip", fake_clip)
    monkeypatch.setattr(render.motion, "preset_sequence", lambda *_: ["pan-left"])

    def plan(hint):
        manifest.write_text(json.dumps({"beats": [{"shot": hint,
                                                  "visual": {"path": str(image)}}]}))
        shots = [{"image": 0, "frames": 30, "motion": "pan_left", "block": 0}]
        render.add_parallax(job, shots, [image], tmp_path)
        return shots[0]

    wide = plan("wide")
    assert plan("wide")["src"] == wide["src"] and len(generated) == 1
    diagram = plan("diagram")
    assert diagram["src"] != wide["src"] and len(generated) == 2
    assert generated[-1][1:] == ("gentle-zoom", "diagram")
    assert diagram["motion"] == "gentle-zoom"
    monkeypatch.setenv("STUDIO_PARALLAX_AMP", "0.02")
    assert plan("diagram")["src"] != diagram["src"]
    assert len(generated) == 3
