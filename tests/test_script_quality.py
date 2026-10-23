"""Offline tests for the deterministic script quality gate, its repair pass and the angle step."""

import json
from pathlib import Path

import pytest

from studio.config import DEFAULT_BRAND_DIR, Settings, load_brand
from studio.contracts import Beat, Script, VideoJob
from studio.pipeline.mock_llm import MockChatClient
from studio.pipeline.runner import build_context
from studio.stages import script
from studio.stages import script_quality as q
from studio.stages.base import StageContext, StageError

TOPIC = "What if the Moon disappeared?"


def brand(slug="gravity-what-if"):
    return load_brand(slug, DEFAULT_BRAND_DIR)


def good(long: bool = False) -> Script:
    return script.normalize(Script.model_validate(MockChatClient().script(TOPIC, long)))


def checks(report: q.QualityReport, severity: str | None = None) -> set[str]:
    return {i.check for i in report.issues if severity is None or i.severity == severity}


def gate(s: Script, fmt: str = "short") -> q.QualityReport:
    return q.gate(s, fmt)


class ScriptedChat:
    """Fake ChatClient returning canned replies in order."""

    def __init__(self, *replies: str):
        self.replies = list(replies)
        self.roles: list[str] = []
        self.messages: list[list[dict]] = []

    def complete(self, messages, *, role, json_schema=None, max_tokens=2048):
        self.roles.append(role)
        self.messages.append(messages)
        return self.replies.pop(0)


def ctx_for(tmp_path: Path, chat) -> StageContext:
    return build_context(True, None, chat, Settings(output_dir=tmp_path, brand_dir=DEFAULT_BRAND_DIR))


def new_job(tmp_path: Path) -> VideoJob:
    return VideoJob(job_id="j1", channel="gravity-what-if", topic=TOPIC, workdir=tmp_path)


def dump(s: Script) -> str:
    return s.model_dump_json()


@pytest.fixture
def no_angle(monkeypatch):
    monkeypatch.setenv("STUDIO_SCRIPT_ANGLE", "0")


# ---------------------------------------------------------------- each check


@pytest.mark.parametrize("long", [False, True])
def test_mock_scripts_pass_the_gate_cleanly(long):
    report = gate(good(long), "long" if long else "short")
    assert report.passed and report.issues == [], [str(i) for i in report.issues]


@pytest.mark.parametrize("hook", ["Why is a dam thicker at its base?",
                                  "Jet fire runs hotter than the blades' melting point.",
                                  "Three bolts hold the whole engine on the wing."])
def test_hook_passes_with_question_claim_or_number(hook):
    assert not q.check_hook(good().model_copy(update={"hook": hook}), "short")


@pytest.mark.parametrize("hook", ["In this video we look at the Moon.", "Did you know the Moon moves?",
                                  "Hey everyone, the Moon is leaving.", ""])
def test_hook_stock_openers_are_errors(hook):
    issues = q.check_hook(good().model_copy(update={"hook": hook}), "short")
    assert any(i.severity == q.ERROR for i in issues)


def test_hook_without_front_loaded_claim_warns():
    s = good().model_copy(update={"hook": "So it is all about how it is for you now."})
    issues = q.check_hook(s, "short")
    assert issues and all(i.severity == q.WARNING for i in issues)
    long_hook = "The Moon " + "slowly " * 14 + "drifts away."
    assert any("words" in i.message for i in q.check_hook(good().model_copy(
        update={"hook": long_hook}), "short"))


def test_beat_count_and_duration():
    s = good()
    assert not q.check_length(s, "short")
    few = s.model_copy(update={"beats": s.beats[:4]})
    found = {(i.check, i.severity) for i in q.check_length(few, "short")}
    assert ("beat_count", q.ERROR) in found and ("duration", q.ERROR) in found
    padded = [b.model_copy(update={"text": b.text + " It holds."}) for b in s.beats]
    near = q.check_length(s.model_copy(update={"beats": padded}), "short")
    assert [(i.check, i.severity) for i in near] == [("duration", q.WARNING)]  # within 10%
    assert any(i.check == "duration" for i in q.check_length(s, "long"))


def test_pacing_words_per_beat():
    s = good()
    assert not q.check_pacing(s, "short")
    wordy = [b.model_copy(update={"text": " ".join(["word"] * 18)}) for b in s.beats]
    issues = q.check_pacing(s.model_copy(update={"beats": wordy}), "short")
    assert any(i.severity == q.ERROR for i in issues)  # ~7 s per picture
    terse = [b.model_copy(update={"text": "Air pushes back."}) for b in s.beats]
    assert any(i.severity == q.ERROR for i in q.check_pacing(s.model_copy(update={"beats": terse}),
                                                             "short"))


def test_loop_and_payoff_for_shorts():
    s = good()
    assert not q.check_loop(s, "short")
    assert {(i.check, i.severity) for i in q.check_loop(s.model_copy(update={"cta": ""}), "short")} \
        >= {("loop", q.ERROR)}
    outro = q.check_loop(s.model_copy(update={"cta": "Subscribe for more!"}), "short")
    assert any(i.severity == q.ERROR for i in outro)
    closed = q.check_loop(s.model_copy(update={"cta": "Rivers carry heavy silt."}), "short")
    assert [(i.check, i.severity) for i in closed] == [("loop", q.WARNING)]
    flat = [b.model_copy(update={"text": "Rocks sit quietly in fields."}) for b in s.beats]
    no_payoff = s.model_copy(update={"beats": flat, "cta": "Rivers carry heavy silt."})
    assert "payoff" in {i.check for i in q.check_loop(no_payoff, "short")}
    assert not any(i.severity == q.ERROR for i in q.check_loop(s.model_copy(update={"cta": ""}),
                                                               "long"))


def test_overlay_word_limit():
    s = good()
    assert not q.check_overlays(s)
    beats = list(s.beats)
    beats[2] = beats[2].model_copy(update={"overlay": "far too many words in this overlay"})
    beats[3] = beats[3].model_copy(update={"text": "Spread the load.", "overlay": "Spread the load"})
    found = [(i.check, i.severity) for i in q.check_overlays(s.model_copy(update={"beats": beats}))]
    assert found == [("overlay", q.ERROR), ("overlay", q.WARNING)]


def test_visual_prompt_specificity_and_text_requests():
    s = good()
    assert not q.check_visual_prompts(s)

    def one(vp: str) -> list[q.QualityIssue]:
        beat = Beat(text="x", visual_prompt=vp, shot="wide")
        return q.check_visual_prompts(s.model_copy(update={"beats": [beat]}))

    assert any(i.severity == q.ERROR for i in one("a dam"))
    no_camera = one("a massive concrete arch dam holding back a deep blue reservoir in a dry "
                    "desert canyon under hard afternoon sun with long shadows on rock")
    assert [i.severity for i in no_camera] == [q.WARNING]
    for word in ("text", "a caption", "the logo", "words", "labeled parts"):
        vp = (f"close-up of a jet engine fan with {word} on the casing, 85mm lens, soft studio "
              "light from upper left, dark background, shallow depth of field")
        assert any(i.severity == q.ERROR and "asks for" in i.message for i in one(vp)), word
    clean = ("close-up of a jet engine fan, no text, no logos or labels, 85mm lens, soft studio "
             "light from upper left, dark background, shallow depth of field, brushed titanium")
    assert not one(clean)


def test_repetition_openings_and_ngrams():
    s = good()
    assert not q.check_repetition(s)
    beats = list(s.beats)
    beats[4] = beats[4].model_copy(update={"text": "The structure relies on bolts in the rock."})
    found = q.check_repetition(s.model_copy(update={"beats": beats}))
    assert any(i.severity == q.ERROR and "open" in i.message for i in found)
    looped = [b.model_copy(update={"text": f"{w} the heavy load pushes the tall wall down hard."})
              for w, b in zip(("First", "Then", "Next", "Later", "Again", "Still", "Now", "Soon",
                               "Finally", "Thus"), s.beats, strict=True)]
    dup = q.check_repetition(s.model_copy(update={"beats": looped}))
    assert any(i.severity == q.ERROR and "trigrams" in i.message for i in dup)
    beats = list(s.beats)
    beats[-1] = beats[-1].model_copy(update={"text": s.cta})
    echo = q.check_repetition(s.model_copy(update={"beats": beats}))
    assert any(i.severity == q.ERROR and "repeats the cta" in i.message for i in echo)


@pytest.mark.parametrize("phrase", ["Let's dive in.", "This is a real game-changer.",
                                    "Buckle up for this one.", "Engineers delve into the core.",
                                    "This will unlock the secret of lift.", "In this video, we see."])
def test_cliches_are_errors_in_spoken_text(phrase):
    s = good()
    beats = list(s.beats)
    beats[5] = beats[5].model_copy(update={"text": phrase})
    issues = q.check_cliches(s.model_copy(update={"beats": beats}))
    assert any(i.severity == q.ERROR and i.check == "cliche" for i in issues)


def test_cliche_list_spares_literal_uses():
    s = good().model_copy(update={"hook": "Why does a phone unlock with your face?"})
    assert not q.check_cliches(s)
    desc = q.check_cliches(s.model_copy(update={"description": "A deep dive on lift."}))
    assert [i.severity for i in desc] == [q.WARNING]


def test_fixture_mode_relaxes_only_length_checks():
    s = good()
    tiny = s.model_copy(update={"beats": s.beats[:2]})
    assert not q.gate(tiny, "short").passed
    relaxed = q.gate(tiny, "short", fixture=True)
    assert relaxed.passed and {"beat_count", "duration"} <= checks(relaxed, q.WARNING)
    bad = tiny.model_copy(update={"cta": "Subscribe now!"})
    assert not q.gate(bad, "short", fixture=True).passed


# ---------------------------------------------------------------- repair loop (fake client)


def weak() -> Script:
    s = good()
    beats = list(s.beats)
    beats[0] = beats[0].model_copy(update={"text": "Tides are a true game-changer for coasts."})
    return s.model_copy(update={"beats": beats})


def test_gate_errors_trigger_one_repair_pass_then_pass(tmp_path, no_angle):
    chat = ScriptedChat(dump(weak()), dump(weak()), dump(good()))
    job = script.run(new_job(tmp_path), brand(), ctx_for(tmp_path, chat))
    assert chat.roles == ["draft", "polish", "polish"] and job.script is not None
    repair = chat.messages[2][1]["content"]
    assert "REQUIRED CORRECTIONS" in repair and "QUALITY ERROR (must fix): cliche" in repair
    rounds = json.loads((tmp_path / "script_quality.json").read_text())
    assert [r["round"] for r in rounds] == ["polish", "repair"]
    assert rounds[0]["errors"] >= 1 and rounds[1]["passed"]
    assert any("quality gate (repair): 0 errors" in line for line in job.log)


def test_gate_fails_job_when_errors_remain(tmp_path, no_angle):
    chat = ScriptedChat(dump(weak()), dump(weak()), dump(weak()))
    with pytest.raises(StageError, match="script quality gate failed"):
        script.run(new_job(tmp_path), brand(), ctx_for(tmp_path, chat))
    assert chat.roles == ["draft", "polish", "polish"]  # exactly one repair pass
    assert (tmp_path / "script.rejected.json").exists()
    assert not (tmp_path / "script.json").exists()


def test_warnings_are_logged_without_repair(tmp_path, no_angle):
    s = good().model_copy(update={"cta": "Rivers carry heavy silt."})  # loop warning only
    chat = ScriptedChat(dump(s), dump(s))
    job = script.run(new_job(tmp_path), brand(), ctx_for(tmp_path, chat))
    assert chat.roles == ["draft", "polish"]
    assert any("quality WARNING [loop]" in line for line in job.log)


def test_policy_lint_still_runs_after_quality_gate(tmp_path, no_angle):
    bad = good().model_copy(update={"description": "Passive income from the Moon."})
    chat = ScriptedChat(*[dump(bad)] * 3)
    with pytest.raises(StageError, match="policy lint failed"):
        script.run(new_job(tmp_path, ), brand("rank-test-lab"), ctx_for(tmp_path, chat))


# ---------------------------------------------------------------- angle step


ANGLE = json.dumps({"surprising_fact": "Tides would shrink but not vanish.",
                    "viewer": "Someone who thinks the Moon only lights the night.",
                    "promise": "Explain what the Moon quietly does for Earth.",
                    "angle": "Improves on common coverage by following tides and spin."})


def test_angle_brief_feeds_the_draft_prompt(tmp_path, monkeypatch):
    monkeypatch.delenv("STUDIO_SCRIPT_ANGLE", raising=False)
    chat = ScriptedChat(ANGLE, dump(good()), dump(good()))
    script.run(new_job(tmp_path), brand(), ctx_for(tmp_path, chat))
    assert chat.roles == ["draft", "draft", "polish"]  # a single extra call
    assert "angle strategist" in chat.messages[0][0]["content"]
    draft_user = chat.messages[1][1]["content"]
    assert draft_user.startswith(f"Topic: {TOPIC}") and "ANGLE BRIEF" in draft_user
    assert "Explain what the Moon quietly does" in draft_user
    assert json.loads((tmp_path / "angle.json").read_text())["viewer"].startswith("Someone")


def test_invalid_angle_is_skipped_not_fatal(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_SCRIPT_ANGLE", "1")
    chat = ScriptedChat("not json", dump(good()), dump(good()))
    job = script.run(new_job(tmp_path), brand(), ctx_for(tmp_path, chat))
    assert chat.roles == ["draft", "draft", "polish"] and job.script is not None
    assert "ANGLE BRIEF" not in chat.messages[1][1]["content"]
    assert any("angle brief skipped" in line for line in job.log)


def test_angle_toggle_changes_cache_key(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_SCRIPT_ANGLE", "0")
    chat = MockChatClient()
    ctx = ctx_for(tmp_path, chat)
    job = script.run(new_job(tmp_path), brand(), ctx)
    script.run(job, brand(), ctx)
    assert chat.calls == ["draft", "polish"]  # second run cached
    monkeypatch.setenv("STUDIO_SCRIPT_ANGLE", "1")
    script.run(job, brand(), ctx)
    assert chat.calls[2:] == ["draft", "draft", "polish"]


# ---------------------------------------------------------------- mock runs


@pytest.mark.parametrize("fmt", ["short", "long"])
def test_mock_run_passes_gate_with_angle(tmp_path, monkeypatch, fmt):
    monkeypatch.delenv("STUDIO_SCRIPT_ANGLE", raising=False)
    chat = MockChatClient()
    job = new_job(tmp_path)
    job.format = fmt
    job = script.run(job, brand(), ctx_for(tmp_path, chat))
    assert chat.calls == ["draft", "draft", "polish"]
    rounds = json.loads((tmp_path / "script_quality.json").read_text())
    assert len(rounds) == 1 and rounds[0]["passed"] and rounds[0]["issues"] == []
    assert job.script is not None and (tmp_path / "angle.json").exists()


def test_reduced_mock_fixture_still_runs(tmp_path):
    job = script.run(new_job(tmp_path), brand(), ctx_for(tmp_path, MockChatClient(beats=2)))
    assert len(job.script.beats) == 2
    assert any("(offline fixture)" in line for line in job.log)
