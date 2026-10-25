"""Offline tests for the ACE-Step song wrapper and the numpy CTC aligner (no models)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np
import pytest

from scripts.kids_song import align, sing

VOCAB = {"<pad>": 0, "|": 1, "A": 2, "B": 3, "L": 4, "O": 5, "P": 6, "'": 7}
EPISODE = Path(__file__).resolve().parents[1] / "docs/2-channels/27-plinkety-pond-episode-01.json"


def posteriors(frames: list[str], vocab=VOCAB, peak: float = 0.9) -> np.ndarray:
    """Synthetic CTC log-posteriors: one dominant symbol per frame, '-' is blank."""
    V = len(vocab)
    p = np.full((len(frames), V), (1 - peak) / (V - 1))
    for t, sym in enumerate(frames):
        p[t, vocab["<pad>" if sym == "-" else sym]] = peak
    return np.log(p)


# --- align.py ---------------------------------------------------------------------------

def test_normalise_words_strips_tags_punctuation_and_hyphens():
    assert align.normalise_words("[Outro]\nBye-bye, bubbles. Pop! ... pop!") == [
        "BYE", "BYE", "BUBBLES", "POP", "POP"]
    assert align.normalise_words("Who’s got (Me, me!)") == ["WHO'S", "GOT", "ME", "ME"]


def test_viterbi_recovers_word_timings_and_scores():
    frames = list("--PP-O-P--|--BB-O-B---")  # "POP BOB" at 20 ms frames
    res = align.align(posteriors(frames), VOCAB, "Pop, Bob!", frame_s=0.02)
    assert res["ok"] and [w["word"] for w in res["words"]] == ["POP", "BOB"]
    pop, bob = res["words"]
    assert pop["start"] == pytest.approx(0.04) and pop["end"] == pytest.approx(0.16)
    assert bob["start"] == pytest.approx(0.26) and bob["end"] == pytest.approx(0.38)
    assert res["score"] == pytest.approx(0.9, abs=1e-6)
    assert res["confident_fraction"] == 1.0
    assert align.monotonic(res["words"])


def test_repeated_letters_need_a_blank_between_them():
    loop = [VOCAB[c] for c in "LOOP"]
    assert align.viterbi(posteriors(list("LO-OP")), loop) is not None
    # CTC needs a blank between the two O's, so 4 frames cannot hold "LOOP".
    assert align.viterbi(posteriors(list("LOOP")), loop) is None


def test_low_confidence_words_are_counted():
    p = np.exp(posteriors(list("-PP-O-P-|-BB-O-B-")))
    p[9:17] = 1.0 / len(VOCAB)  # "BOB" frames carry no evidence
    res = align.align(np.log(p), VOCAB, "pop bob")
    assert res["ok"]
    assert res["words"][0]["score"] > 0.8 > align.CONFIDENT > res["words"][1]["score"]
    assert res["confident_fraction"] == 0.5


def test_infeasible_alignment_reports_failure():
    res = align.align(posteriors(list("-P-")), VOCAB, "pop bob bob")
    assert res["ok"] is False and res["score"] == 0.0


# --- sing.py ----------------------------------------------------------------------------

def test_format_lyrics_tags_sections_and_call_response():
    lines = json.loads(EPISODE.read_text())["lyrics_lines"]
    text = sing.format_lyrics(sing.sections_from_lines(lines))
    rows = text.splitlines()
    assert rows[0] == "[Intro]"
    assert rows[1] == "Is it bath time? (Yes, it's bath time!)"
    assert "Who's got bubbles? (Me, me, me!)" in rows
    tags = [r for r in rows if r.startswith("[")]
    assert tags == ["[Intro]", "[Verse]", "[Chorus]", "[Verse]", "[Chorus]", "[Verse]",
                    "[Outro]"]
    assert all(r == r.strip() for r in rows)  # tags and lyrics each on their own line
    assert text.count("Bubbles on my tummy") == 2
    sung = sing.sung_text(text)
    assert "[" not in sung and "(" not in sung and "Yes, it's bath time!" in sung
    assert len(align.normalise_words(sung)) == sum(
        len(align.normalise_words(ln)) for ln in lines)


def test_caption_and_key():
    assert "British English" in sing.CAPTION and "96 bpm" in sing.CAPTION
    assert sing.ace_key("C major") == "C Major"


def test_argv_runs_worker_in_ace_venv_under_guard(tmp_path):
    argv = sing.worker_argv("sing", tmp_path / "j.json")
    assert argv[0].endswith("output/runtimes/ace-step/repo/.venv/bin/python")
    assert argv[1] == "-I" and argv[2].endswith("scripts/kids_song/ace_worker.py")
    g = sing.guarded_argv(argv, tmp_path / "p.jsonl")
    assert g[1].endswith("scripts/run_guarded.py")
    assert g[2:4] == ["--threads", "3"] and g[g.index("--") + 1:] == argv
    env = sing.guard_env({"PATH": "/bin", "PYTHONPATH": "x"})
    assert env["PYTORCH_MPS_HIGH_WATERMARK_RATIO"] == "0.0"
    assert env["HF_HUB_OFFLINE"] == "1" and "PYTHONPATH" not in env


def test_watermark_env_only_goes_to_the_child(monkeypatch):
    import os
    monkeypatch.delenv("PYTORCH_MPS_HIGH_WATERMARK_RATIO", raising=False)
    base = {"PATH": "/bin"}
    sing.guard_env(base)
    sing.guard_env()
    assert "PYTORCH_MPS_HIGH_WATERMARK_RATIO" not in os.environ
    assert base == {"PATH": "/bin"}


def test_sing_writes_job_and_reads_result(tmp_path):
    calls = []

    def fake_run(cmd, cwd, env, check, **kw):
        calls.append((cmd, env))
        job = json.loads(Path(cmd[-1]).read_text())
        Path(job["result_json"]).write_text(json.dumps({"seed": job["seed"], "wall_s": 1.0}))
        return subprocess.CompletedProcess(cmd, 0)

    out = tmp_path / "t.wav"
    res = sing.sing("[Verse]\nla\n", sing.CAPTION, 96, "C Major", 60, 7, out,
                    runner=fake_run, is_free=lambda: True, sleep=lambda s: None)
    assert res["seed"] == 7 and "guard_wall_s" in res and res["pressure"] == {"samples": 0}
    job = json.loads(out.with_suffix(".job.json").read_text())
    assert job["steps"] == 8 and job["vocal_language"] == "en" and job["bpm"] == 96
    assert job["duration"] == 60.0 and job["key"] == "C Major" and job["lyrics"].startswith("[")
    assert job["thinking"] is False  # no-LM path by default (lower peak memory)
    cmd, env = calls[0]
    assert cmd[-2:] == ["sing", str(out.with_suffix(".job.json"))]
    assert env["PYTORCH_MPS_HIGH_WATERMARK_RATIO"] == "0.0"


def test_run_guarded_waits_for_lock_and_retries_busy_exit(tmp_path):
    free = iter([False, False, True, True])
    results = iter([(75, "Resource guard: Another guarded job is running; wait\n"), (0, "")])
    sleeps = []

    def fake_run(cmd, cwd, env, check, **kw):
        code, err = next(results)
        return subprocess.CompletedProcess(cmd, code, stderr=err)

    sing.run_guarded(["py", "-I", "w.py", "sing", "j"], tmp_path / "l", runner=fake_run,
                     is_free=lambda: next(free), sleep=sleeps.append, wait_s=1)
    assert sleeps == [1, 1, 1]


def test_run_guarded_does_not_loop_on_pressure_stop(tmp_path):
    calls = []

    def fake_run(cmd, cwd, env, check, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd, 75, stderr="Resource guard: Memory pressure is not green; stopped workload\n")

    with pytest.raises(sing.PressureStop, match="not green"):
        sing.run_guarded(["py", "-I", "w.py", "sing", "j"], tmp_path / "l", runner=fake_run,
                         is_free=lambda: True, sleep=lambda s: None)
    assert len(calls) == 1


def test_run_guarded_raises_on_real_failure(tmp_path):
    def fake_run(cmd, cwd, env, check, **kw):
        return subprocess.CompletedProcess(cmd, 1)

    with pytest.raises(RuntimeError, match="exit 1"):
        sing.run_guarded(["py", "-I", "w.py", "sing", "j"], tmp_path / "l", runner=fake_run,
                         is_free=lambda: True, sleep=lambda s: None)


def test_slot_free_detects_held_lock(tmp_path):
    import fcntl
    import os
    lock = tmp_path / ".lock"
    assert sing.slot_free(lock)
    fd = os.open(lock, os.O_CREAT | os.O_WRONLY)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        assert not sing.slot_free(lock)
    finally:
        os.close(fd)


def test_select_best_prefers_eligible_high_score():
    good = {"clipped_fraction": 0.0, "peak_dbfs": -1.0, "lufs": -14.0,
            "vocal_active_fraction": 0.6}
    words = [{"start": 0.0}, {"start": 1.0}]
    takes = [
        {"take": 1, "analysis": {**good, "peak_dbfs": 0.0},  # clipping: excluded
         "alignment": {"ok": True, "score": 0.9, "confident_fraction": 0.9, "words": words}},
        {"take": 2, "analysis": good,
         "alignment": {"ok": True, "score": 0.5, "confident_fraction": 0.4, "words": words}},
        {"take": 3, "analysis": {**good, "lufs": -30.0},  # too quiet: excluded
         "alignment": {"ok": True, "score": 0.8, "confident_fraction": 0.8, "words": words}},
    ]
    assert sing.select_best(takes)["take"] == 2


def test_pressure_summary_counts_levels(tmp_path):
    log = tmp_path / "p.jsonl"
    log.write_text("\n".join(json.dumps(r) for r in [
        {"time": 1, "pressure": 1, "free_gb": 30}, {"time": 2, "pressure": 1, "free_gb": 29},
        {"time": 3, "pressure": 2, "free_gb": 28}]))
    s = sing.pressure_summary(log, since=2)
    assert s == {"samples": 2, "levels": {"green": 1, "yellow": 1}, "min_free_gb": 28}


def test_word_lines_groups_words_by_lyric_line():
    words = [{"word": w, "start": i, "end": i + 0.5} for i, w in
             enumerate(["POP", "POP", "AWAY", "IT", "GOES"])]
    out = sing.word_lines(words, ["Pop! Pop!", "Away it goes!"])
    assert out == [{"text": "Pop! Pop!", "start": 0, "end": 1.5},
                   {"text": "Away it goes!", "start": 2, "end": 4.5}]


def test_ready_needs_free_slot_green_pressure_and_no_heavy_job(tmp_path):
    lock = tmp_path / ".lock"
    kw = {"sleep": lambda s: None, "green": lambda: True, "heavy": lambda: False}
    assert sing.ready(lock, mem=lambda: 70, **kw)
    assert not sing.ready(lock, mem=lambda: 40, **kw)
    seq = iter([70, 70, 70, 45])
    assert not sing.ready(lock, mem=lambda: next(seq), **kw)  # must hold for all 4 checks
    assert not sing.ready(lock, mem=lambda: None, **kw)
    assert not sing.ready(lock, mem=lambda: 70, sleep=lambda s: None, green=lambda: False,
                          heavy=lambda: False)
    assert not sing.ready(lock, mem=lambda: 70, sleep=lambda s: None, green=lambda: True,
                          heavy=lambda: True)
