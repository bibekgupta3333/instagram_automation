"""Stage unit tests that need no ffmpeg: policy lint, script/fact-check stages, captions, shot plan."""

import itertools
import json
from pathlib import Path

import pytest

from studio.config import DEFAULT_BRAND_DIR, Settings, load_brand
from studio.contracts import Beat, JobStatus, Script, VideoJob
from studio.pipeline.mock_llm import MockChatClient
from studio.pipeline.runner import build_context, make_video
from studio.stages import captions, fact_check, script
from studio.stages.base import MOCK_ENGINES, StageContext, StageError
from studio.stages.policy import lint_script
from studio.stages.render import MOTIONS, plan_shots


def brand(slug):
    return load_brand(slug, DEFAULT_BRAND_DIR)


def make_script(text: str, **kw) -> Script:
    return Script(
        title=kw.get("title", "Title"), hook=kw.get("hook", "What if this happened?"),
        beats=[Beat(text=text, visual_prompt=kw.get("visual", "a wide shot"))],
        description=kw.get("description", ""), sources=kw.get("sources", []),
    )


def rules(script_obj: Script, slug: str) -> set[str]:
    return {v.rule for v in lint_script(script_obj, brand(slug))}


# ---------------------------------------------------------------- policy lint


@pytest.mark.parametrize(
    ("slug", "text", "rule"),
    [
        ("rank-test-lab", "We guarantee first page rankings in 30 days.", "income_guarantee"),
        ("rank-test-lab", "This is how to make money online with SEO.", "income_guarantee"),
        ("rank-test-lab", "Earn $5,000 a month from one blog.", "income_guarantee"),
        ("rank-test-lab", "Step one: buy reviews for your listing.", "black_hat_marketing"),
        ("rank-test-lab", "I'm a real person, not an AI.", "ai_presenter_claims_human"),
        ("gravity-what-if", "Dr. Patel explains what happens next.", "credential_claim"),
        ("furnace-to-faucet", "I'm a licensed electrician, so trust me.", "credential_claim"),
        ("wing-and-turbine", "As a pilot, I can tell you this.", "credential_claim"),
        ("software-shortcut", "Your trusted tech expert is back.", "credential_claim"),
        ("square-foot-money", "You should buy before rates drop.", "personal_advice"),
        ("square-foot-money", "These are the safe neighborhoods.", "fair_housing_steering"),
        ("lock-screen-logic", "Here is how to hack into his email.", "offensive_security"),
        ("lock-screen-logic", "Use a VPN to unblock Netflix abroad.", "offensive_security"),
        ("wing-and-turbine", "The hijackers took the cockpit.", "crash_content"),
        ("wing-and-turbine", "Inside the worst plane crash ever.", "crash_content"),
        ("megabuild-blueprint", "Over 1,200 workers died building it.", "disaster_framing"),
        ("furnace-to-faucet", "Anyone can do this panel swap.", "diy_hazard"),
        ("furnace-to-faucet", "A HELOC pays for the new furnace.", "financing_out_of_lane"),
        ("furnace-to-faucet", "A new water heater costs $1,800.", "undated_cost"),
        ("gravity-what-if", "Hey guys, today we look at the Moon.", "banned_template"),
    ],
)
def test_lint_catches_red_lines(slug, text, rule):
    assert rule in rules(make_script(text), slug)


def test_lint_checks_every_field():
    s = make_script("Fine text.", visual="Dr. Smith in a lab coat", description="like and subscribe")
    found = {(v.rule, v.field) for v in lint_script(s, brand("gravity-what-if"))}
    assert ("credential_claim", "beats[0].visual_prompt") in found
    assert ("banned_template", "description") in found


@pytest.mark.parametrize(
    ("slug", "text"),
    [
        ("wing-and-turbine", "The pilot pulls back and the elevator tilts the nose up."),
        ("furnace-to-faucet", "In 2026, a tank replacement averaged $1,800 in Ohio."),
        ("business-model-files", "Its CPA, the cost per acquisition, fell by half."),
        ("lock-screen-logic", "A phishing text tries to steal your login. Here's the setting that stops it."),
        ("rank-test-lab", "Results vary: our page moved from 14th to 6th."),
    ],
)
def test_lint_allows_legitimate_lines(slug, text):
    s = make_script(text, sources=["RSMeans 2026"])
    assert lint_script(s, brand(slug)) == []


def test_crash_rule_only_applies_where_it_is_a_red_line():
    assert "crash_content" not in rules(make_script("A market crash in 2008."), "business-model-files")


# ---------------------------------------------------------------- script stage


class ScriptedChat:
    """ChatClient returning canned replies in order."""

    def __init__(self, *replies: str):
        self.replies = list(replies)
        self.roles: list[str] = []
        self.messages: list[list[dict]] = []

    def complete(self, messages, *, role, json_schema=None, max_tokens=2048):
        self.roles.append(role)
        self.messages.append(messages)
        return self.replies.pop(0)


def ctx_for(tmp_path: Path, chat) -> StageContext:
    settings = Settings(output_dir=tmp_path, brand_dir=DEFAULT_BRAND_DIR)
    return build_context(True, None, chat, settings)


def new_job(tmp_path: Path, channel="gravity-what-if") -> VideoJob:
    return VideoJob(job_id="j1", channel=channel, topic="What if the Moon disappeared?",
                    workdir=tmp_path)


def good_reply() -> str:
    return json.dumps(MockChatClient().script("What if the Moon disappeared?"))


def test_script_prompt_carries_brand_rules(tmp_path):
    chat = ScriptedChat(good_reply())
    job = script.run(new_job(tmp_path), brand("gravity-what-if"), ctx_for(tmp_path, chat))
    system = chat.messages[0][0]["content"]
    assert chat.roles == ["draft"]
    assert "1-3 seconds" in system and "2-4 seconds" in system and "Loop ending" in system
    assert "no NASA insignia" in system and "scientists are baffled" in system
    assert '"beats"' in system  # JSON schema included
    assert job.script and (tmp_path / "script.json").exists()
    assert (tmp_path / ".cache" / "script.json").exists()


def test_script_retries_once_on_invalid_json(tmp_path):
    chat = ScriptedChat("not json at all", "```json\n" + good_reply() + "\n```")
    job = script.run(new_job(tmp_path), brand("gravity-what-if"), ctx_for(tmp_path, chat))
    assert len(chat.roles) == 2 and job.script is not None
    assert "previous reply was not valid" in chat.messages[1][-1]["content"]


def test_script_fails_after_two_invalid_replies(tmp_path):
    chat = ScriptedChat('{"title": "x"}', '{"hook": "y"}')
    with pytest.raises(StageError, match="failed validation twice"):
        script.run(new_job(tmp_path), brand("gravity-what-if"), ctx_for(tmp_path, chat))


def test_policy_lint_fails_the_job(tmp_path):
    bad = MockChatClient().script("SEO test")
    bad["beats"][0]["text"] = "We guarantee page one rankings and passive income."
    settings = Settings(output_dir=tmp_path, brand_dir=DEFAULT_BRAND_DIR)
    job = make_video("rank-test-lab", "SEO test", mock=True, chat=ScriptedChat(json.dumps(bad)),
                     settings=settings)
    assert job.status == JobStatus.FAILED
    assert "policy lint failed" in job.log[-1] and "income_guarantee" in job.log[-1]
    assert (Path(job.workdir) / "script.rejected.json").exists()
    assert not (Path(job.workdir) / "script.json").exists()
    saved = VideoJob.model_validate_json((Path(job.workdir) / "job.json").read_text())
    assert saved.status == JobStatus.FAILED


# ---------------------------------------------------------------- fact-check stage


def scripted_job(tmp_path, channel):
    job = new_job(tmp_path, channel)
    job.script = Script.model_validate(MockChatClient().script("Topic"))
    return job


def test_fact_check_skipped_for_non_critical(tmp_path):
    chat = ScriptedChat()
    job = fact_check.run(scripted_job(tmp_path, "gravity-what-if"), brand("gravity-what-if"),
                         ctx_for(tmp_path, chat))
    assert chat.roles == [] and job.fact_check.model == "skipped" and job.fact_check.passed


def test_fact_check_calls_role_for_accuracy_critical(tmp_path):
    chat = ScriptedChat(json.dumps({"passed": True, "issues": [], "model": "sonnet"}))
    job = fact_check.run(scripted_job(tmp_path, "wing-and-turbine"), brand("wing-and-turbine"),
                         ctx_for(tmp_path, chat))
    assert chat.roles == ["fact_check"] and job.fact_check.model == "sonnet"


def test_fact_check_failure_raises(tmp_path):
    chat = ScriptedChat(json.dumps({"passed": False, "issues": ["Wrong altitude"], "model": "m"}))
    with pytest.raises(StageError, match="Wrong altitude"):
        fact_check.run(scripted_job(tmp_path, "wing-and-turbine"), brand("wing-and-turbine"),
                       ctx_for(tmp_path, chat))


# ---------------------------------------------------------------- captions + shot plan


def test_mock_words_and_ass(tmp_path):
    timings = {"segments": [{"text": "one two three four", "start": 0.0, "end": 2.0},
                            {"text": "five six", "start": 2.1, "end": 3.0}]}
    words = captions.mock_words(timings, 3.0)
    assert [w["text"] for w in words] == ["one", "two", "three", "four", "five", "six"]
    assert words[1]["start"] == 0.5 and words[3]["end"] == 2.0 and words[4]["start"] == 2.1
    b = brand("gravity-what-if")
    captions.write_ass(words, b, "short", tmp_path / "c.ass")
    ass = (tmp_path / "c.ass").read_text()
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass
    assert "Arial Black" in ass and "&H0027B6FF" in ass  # solar #FFB627 as ASS BGR highlight
    assert ass.count("Dialogue:") == 6
    captions.write_srt(words, tmp_path / "c.srt")
    assert "00:00:00,000 --> " in (tmp_path / "c.srt").read_text()


def test_shot_plan_is_frame_exact_and_varied():
    timings = {"segments": [
        {"beat": 0, "start": 0.0, "end": 1.5}, {"beat": 0, "start": 1.6, "end": 9.0},
        {"beat": 1, "start": 9.1, "end": 12.0}, {"beat": 1, "start": 12.1, "end": 14.0},
    ]}
    shots = plan_shots(timings, 2, 14.0, seed="job-a")
    assert sum(s["frames"] for s in shots) == 14 * 30
    assert all(s["frames"] <= 3.5 * 30 + 1 for s in shots)  # a change every <=3.5 s
    assert all(a["motion"] != b["motion"] for a, b in itertools.pairwise(shots))
    assert {s["motion"] for s in shots} <= set(MOTIONS)
    other = plan_shots(timings, 2, 14.0, seed="job-b")
    assert [s["motion"] for s in shots] != [s["motion"] for s in other]


def test_mock_engines_are_default_in_mock_context(tmp_path):
    ctx = ctx_for(tmp_path, MockChatClient())
    assert ctx.engines == MOCK_ENGINES
