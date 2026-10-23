"""Stage unit tests that need no ffmpeg: policy lint, script/fact-check stages, captions, shot plan."""

import itertools
import json
from pathlib import Path

import pytest

from studio.config import DEFAULT_BRAND_DIR, Settings, load_brand
from studio.contracts import Beat, FactCheck, JobStatus, Script, VideoJob
from studio.pipeline.mock_llm import MockChatClient
from studio.pipeline.runner import build_context, make_video
from studio.stages import captions, fact_check, script
from studio.stages.base import MOCK_ENGINES, StageContext, StageError
from studio.stages.policy import lint_script
from studio.stages.render import MOTIONS, plan_shots


@pytest.fixture(autouse=True)
def _no_angle_step(monkeypatch):
    # ScriptedChat replies are consumed in order; the optional angle call is covered in
    # tests/test_script_quality.py.
    monkeypatch.setenv("STUDIO_SCRIPT_ANGLE", "0")


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
    chat = ScriptedChat(good_reply(), good_reply())
    job = script.run(new_job(tmp_path), brand("gravity-what-if"), ctx_for(tmp_path, chat))
    system = chat.messages[0][0]["content"]
    assert chat.roles == ["draft", "polish"]
    assert "first 1-2 s" in system and "8-12 beats" in system and "loop line" in system
    assert "no NASA insignia" in system and "scientists are baffled" in system
    assert "macro detail" in system and "stock_query" in system and "overlay" in system
    assert '"beats"' in system  # JSON schema included
    polish = chat.messages[1]
    assert "senior editor" in polish[0]["content"]
    assert "DRAFT SCRIPT" in polish[1]["content"] and "AUTOMATED QA REPORT" in polish[1]["content"]
    assert job.script and (tmp_path / "script.json").exists()
    assert (tmp_path / ".cache" / "script.json").exists()


def test_script_retries_once_on_invalid_json(tmp_path):
    chat = ScriptedChat("not json at all", "```json\n" + good_reply() + "\n```", good_reply())
    job = script.run(new_job(tmp_path), brand("gravity-what-if"), ctx_for(tmp_path, chat))
    assert chat.roles == ["draft", "draft", "polish"] and job.script is not None
    assert "previous reply was not valid" in chat.messages[1][-1]["content"]


def test_script_polish_retries_once(tmp_path):
    chat = ScriptedChat(good_reply(), "{}", good_reply())
    job = script.run(new_job(tmp_path), brand("gravity-what-if"), ctx_for(tmp_path, chat))
    assert chat.roles == ["draft", "polish", "polish"] and job.script is not None
    assert "previous reply was not valid" in chat.messages[2][-1]["content"]


@pytest.mark.parametrize("fmt", ["short", "long"])
def test_writer_and_editor_require_evidence_not_numeric_hooks(tmp_path, fmt):
    job = new_job(tmp_path, "car-cost-truth")
    job.format = fmt
    job.topic = "Why streamlined cars face less air resistance"
    b = brand("car-cost-truth")
    draft = Script.model_validate_json(good_reply())
    prompts = [script.build_messages(job, b),
               script.build_polish_messages(job, b, draft, [])]
    for messages in prompts:
        system = messages[0]["content"]
        assert "Do not invent numbers" in system
        assert "no verified source evidence" in system
        assert "Model memory or confidence is not evidence" in system
        for mandate in ("contains a specific number", "give it a number", "Use 3-5 numbers",
                        "with a specific number", "true sentence with a number"):
            assert mandate not in system
    draft.hook = "Why do streamlined cars face less air resistance?"
    draft.sources = []
    assert not any("hook" in q or "sources is empty" in q
                   for q in script.qa_issues(draft, fmt, b))


def test_script_fails_after_two_invalid_replies(tmp_path):
    chat = ScriptedChat('{"title": "x"}', '{"hook": "y"}')
    with pytest.raises(StageError, match="failed validation twice"):
        script.run(new_job(tmp_path), brand("gravity-what-if"), ctx_for(tmp_path, chat))


def test_mock_script_has_rich_beats():
    s = Script.model_validate(MockChatClient().script("How the Hoover Dam holds back Lake Mead"))
    assert len(s.beats) == 10
    assert all(b.shot in script.SHOTS and b.visual_prompt and len(b.overlay.split()) <= 5
               for b in s.beats)
    assert all(not b.stock_query for b in s.beats if b.shot in script.NO_STOCK_SHOTS)
    assert any(b.stock_query for b in s.beats) and len({b.shot for b in s.beats}) >= 4
    assert script.qa_issues(s, "short", brand("megabuild-blueprint")) == []


def test_normalize_and_qa_flag_weak_scripts():
    weak = Script(title="T", hook="In this video we look at dams", cta="Subscribe for more", beats=[
        Beat(text="Dams are big.", visual_prompt="a dam with a logo", shot="Close up",
             overlay="this overlay is far too long", stock_query="Diagram, Of; A Dam"),
        Beat(text="Water is heavy.", visual_prompt="a dam with a logo", shot="schematic",
             stock_query="water pressure"),
    ])
    qa = " ".join(script.qa_issues(weak, "short", brand("megabuild-blueprint")))
    for needle in ("2 beats", "stock opener", "outro", "vague",
                   "mentions 'logo'", "repeats an earlier", "max 5", "policy: [banned_template]"):
        assert needle in qa, needle
    n = script.normalize(weak)
    assert [b.shot for b in n.beats] == ["close-up", "diagram"]
    assert n.beats[0].overlay == "this overlay is far too" and n.beats[0].stock_query == "diagram of a dam"
    assert n.beats[1].stock_query == ""  # no stock search for diagram beats


def test_policy_lint_fails_the_job(tmp_path, monkeypatch):
    # No model was loaded; runner cleanup must not import MLX in this offline unit test.
    monkeypatch.setattr("studio.pipeline.runner.UNLOAD", {})
    bad = MockChatClient().script("SEO test")
    bad["beats"][0]["text"] = "We guarantee page one rankings and passive income."
    settings = Settings(output_dir=tmp_path, brand_dir=DEFAULT_BRAND_DIR)
    job = make_video("rank-test-lab", "SEO test", mock=True,
                     chat=ScriptedChat(*[json.dumps(bad)] * 3), settings=settings)
    assert job.status == JobStatus.FAILED
    assert "policy lint failed" in job.log[-1] and "income_guarantee" in job.log[-1]
    assert (Path(job.workdir) / "script.rejected.json").exists()
    assert not (Path(job.workdir) / "script.json").exists()
    saved = VideoJob.model_validate_json((Path(job.workdir) / "job.json").read_text())
    assert saved.status == JobStatus.FAILED


def test_overlay_is_policy_linted(tmp_path):
    bad = MockChatClient().script("SEO test")
    bad["beats"][1]["overlay"] = "passive income"
    chat = ScriptedChat(*[json.dumps(bad)] * 3)
    with pytest.raises(StageError, match=r"beats\[1\]\.overlay"):
        script.run(new_job(tmp_path, "rank-test-lab"), brand("rank-test-lab"), ctx_for(tmp_path, chat))
    assert chat.roles == ["draft", "polish", "polish"]  # one policy repair pass, then the gate
    assert "POLICY VIOLATION" in chat.messages[2][1]["content"]


def test_policy_repair_pass_can_fix_the_script(tmp_path):
    bad = MockChatClient().script("SEO test")
    bad["description"] = "Passive income tips."
    chat = ScriptedChat(json.dumps(bad), json.dumps(bad), good_reply())
    job = script.run(new_job(tmp_path, "rank-test-lab"), brand("rank-test-lab"), ctx_for(tmp_path, chat))
    assert chat.roles == ["draft", "polish", "polish"] and job.script is not None


def test_cached_script_is_policy_linted(tmp_path):
    chat = ScriptedChat(good_reply(), good_reply())
    ctx = ctx_for(tmp_path, chat)
    job = script.run(new_job(tmp_path), brand("gravity-what-if"), ctx)
    job.script.beats[0].overlay = "passive income"
    with pytest.raises(StageError, match="income_guarantee"):
        script.run(job, brand("gravity-what-if"), ctx)
    assert chat.roles == ["draft", "polish"]


# ---------------------------------------------------------------- fact-check stage


def scripted_job(tmp_path, channel):
    job = new_job(tmp_path, channel)
    job.script = Script.model_validate(MockChatClient().script("Topic"))
    return job


def claims(*verdicts):
    return json.dumps({"claims": [
        {"claim": f"claim {i}", "verdict": v, "fix": "" if v == "correct" else f"fixed {i}"}
        for i, v in enumerate(verdicts)
    ]})


def test_fact_check_reviews_non_critical_channel(tmp_path):
    chat = ScriptedChat(claims("correct"))
    job = fact_check.run(scripted_job(tmp_path, "gravity-what-if"), brand("gravity-what-if"),
                         ctx_for(tmp_path, chat))
    assert chat.roles == ["fact_check"] and job.fact_check.model != "skipped"
    assert job.fact_check.passed


@pytest.mark.parametrize("text", [
    "Air resistance is one factor in fuel use.",
    "Air resistance is ONE FACTOR among many influences.",
    "Air resistance is one piece of the puzzle.",
    "Air resistance is one piece of puzzle.",
])
def test_qualitative_one_does_not_trigger_quantity_rewrite(tmp_path, text):
    job = scripted_job(tmp_path, "car-cost-truth")
    job.script.beats[0].text = text
    chat = ScriptedChat(claims("correct"))
    fact_check.run(job, brand("car-cost-truth"), ctx_for(tmp_path, chat))
    assert job.fact_check.passed
    assert chat.roles == ["fact_check"]
    assert not fact_check.unsupported_quantities(job.script)


@pytest.mark.parametrize("field", [
    "title", "hook", "cta", "description", "text", "overlay", "hashtags",
])
@pytest.mark.parametrize("text", [
    "Air resistance is one factor, reducing fuel use by 30%.",
    "Air resistance is one piece of the puzzle, doubling fuel use.",
    "Air resistance is one factor; the trip costs twice as much.",
    "One factor affects two vehicles.",
    "One piece of the puzzle weighs 10 grams.",
    "The concentration is one part per million.",
    "The concentration is one part per litre.",
    "The assembly needs one piece per vehicle.",
    "The sample contains one element.",
    "The model measures one aspect.",
    "The checklist contains one consideration.",
    "The model assigns one factor per vehicle.",
    "The model assigns one factor for each vehicle.",
    "The model uses one factor of safety.",
])
def test_idiom_exemption_preserves_quantities_in_every_published_field(field, text):
    s = make_script("Air resistance affects fuel use.")
    if field in {"text", "overlay"}:
        setattr(s.beats[0], field, text)
        expected_field = f"beats[0].{field}"
    elif field == "hashtags":
        s.hashtags = [text]
        expected_field = "hashtags[0]"
    else:
        setattr(s, field, text)
        expected_field = field
    s.sources = ["Model-generated citation: https://example.com/research"]
    unsupported = fact_check.unsupported_quantities(s)
    assert len(unsupported) == 1
    assert unsupported[0].claim == f"{expected_field}: {text}"
    assert unsupported[0].verdict == "unverifiable"


@pytest.mark.parametrize("text", [
    "Every 10\u202fmph boost adds about 16% more drag.",
    "Rolling resistance stays steady, about 5% of engine load.",
    "At 70 mph the car uses 25–30% more fuel.",
    "A 20-mph speed bump can double your trip cost.",
    "The trip costs twice as much.",
    "Air resistance is one factor, reducing fuel use by 30%.",
    "The concentration is one part per million.",
    "The concentration is one part per litre.",
    "The model assigns one factor per vehicle.",
])
def test_model_agreement_and_generated_citations_cannot_verify_statistics(tmp_path, text):
    job = scripted_job(tmp_path, "car-cost-truth")
    job.script.beats[0].text = text
    job.script.sources = ["U.S. Department of Energy, model-generated reference, 2023"]
    # The model votes correct both times and the editor keeps the unsupported statement.
    chat = ScriptedChat(claims("correct"), job.script.model_dump_json(), claims("correct"))
    with pytest.raises(StageError, match="no supplied, verified source evidence"):
        fact_check.run(job, brand("car-cost-truth"), ctx_for(tmp_path, chat))
    assert not job.fact_check.passed
    assert chat.roles == ["fact_check", "polish", "fact_check"]
    rounds = json.loads((tmp_path / "fact_check_claims.json").read_text())
    assert all(r["sources_verified"] is False for r in rounds)
    assert any(c["verdict"] == "unverifiable" for c in rounds[-1]["claims"])


def test_unsupported_statistic_can_be_rewritten_qualitatively(tmp_path):
    job = scripted_job(tmp_path, "car-cost-truth")
    job.script.beats[0].text = "Every 10 mph adds 16% drag."
    fixed = json.loads(good_reply())
    fixed["hook"] = "Why do streamlined cars face less air resistance?"
    chat = ScriptedChat(claims("correct"), json.dumps(fixed), claims("correct"))
    fact_check.run(job, brand("car-cost-truth"), ctx_for(tmp_path, chat))
    assert job.fact_check.passed
    assert not fact_check.unsupported_quantities(job.script)
    assert "Remove the quantitative claim" in chat.messages[1][1]["content"]


def test_fact_check_calls_role_for_accuracy_critical(tmp_path):
    chat = ScriptedChat(claims("correct", "correct"))
    job = fact_check.run(scripted_job(tmp_path, "wing-and-turbine"), brand("wing-and-turbine"),
                         ctx_for(tmp_path, chat))
    assert chat.roles == ["fact_check"] and job.fact_check.passed
    assert '"hook"' in chat.messages[0][1]["content"]
    assert "visual_prompt" not in chat.messages[0][1]["content"]  # facts only
    checked = json.loads(chat.messages[0][1]["content"])
    assert checked["description"] == job.script.description
    assert checked["hashtags"] == job.script.hashtags
    assert checked["beats"][1]["overlay"] == job.script.beats[1].overlay


@pytest.mark.parametrize("invalid", [
    "not JSON", '{"claims": []}',
    '{"claims": [{"claim": "  ", "verdict": "correct"}]}',
    '{"claims": [{"claim": "A fact", "verdict": "maybe"}]}',
])
def test_fact_check_retries_invalid_review_once(tmp_path, invalid):
    chat = ScriptedChat(invalid, claims("correct"))
    job = fact_check.run(scripted_job(tmp_path, "wing-and-turbine"), brand("wing-and-turbine"),
                         ctx_for(tmp_path, chat))
    assert job.fact_check.passed and chat.roles == ["fact_check", "fact_check"]
    assert "Invalid reply" in chat.messages[1][-1]["content"]


def test_fact_check_invalid_review_clears_previous_pass(tmp_path):
    job = scripted_job(tmp_path, "wing-and-turbine")
    job.fact_check = FactCheck(passed=True, model="old review")
    chat = ScriptedChat('{"claims": []}', '{"claims": []}')
    with pytest.raises(StageError, match="invalid reply twice"):
        fact_check.run(job, brand("wing-and-turbine"), ctx_for(tmp_path, chat))
    assert job.fact_check is None
    assert not (tmp_path / ".cache" / "fact_check.json").exists()


def test_fact_check_rewrites_once_then_passes(tmp_path):
    fixed = MockChatClient().script("Topic")
    fixed["hook"] = "Why do supports need room to move?"
    chat = ScriptedChat(claims("correct", "wrong"), json.dumps(fixed), claims("correct"))
    job = fact_check.run(scripted_job(tmp_path, "wing-and-turbine"), brand("wing-and-turbine"),
                         ctx_for(tmp_path, chat))
    assert chat.roles == ["fact_check", "polish", "fact_check"]
    assert "REQUIRED CORRECTIONS" in chat.messages[1][1]["content"]
    assert "fixed 1" in chat.messages[1][1]["content"]
    assert job.fact_check.passed and job.script.hook == "Why do supports need room to move?"
    assert (tmp_path / "script.prefactcheck.json").exists()
    assert Script.model_validate_json((tmp_path / "script.json").read_text()).hook == job.script.hook


def test_fact_check_failure_raises(tmp_path):
    chat = ScriptedChat(claims("wrong"), json.dumps(MockChatClient().script("Topic")),
                        claims("correct", "misleading"))
    with pytest.raises(StageError, match="misleading: claim 1"):
        fact_check.run(scripted_job(tmp_path, "wing-and-turbine"), brand("wing-and-turbine"),
                       ctx_for(tmp_path, chat))
    assert chat.roles == ["fact_check", "polish", "fact_check"]


def test_fact_check_rewrite_is_policy_linted_and_keeps_audit(tmp_path):
    bad = MockChatClient().script("Topic")
    bad["beats"][1]["overlay"] = "passive income"
    chat = ScriptedChat(claims("wrong"), json.dumps(bad))
    job = scripted_job(tmp_path, "wing-and-turbine")
    original = job.script.model_dump()
    with pytest.raises(StageError, match="income_guarantee"):
        fact_check.run(job, brand("wing-and-turbine"), ctx_for(tmp_path, chat))
    assert chat.roles == ["fact_check", "polish"]
    assert job.script.model_dump() == original
    assert job.fact_check is None
    assert json.loads((tmp_path / "fact_check_claims.json").read_text())[0]["claims"]
    assert (tmp_path / "script.rejected.json").exists()
    assert not (tmp_path / ".cache" / "fact_check.json").exists()


@pytest.mark.parametrize("passed", [True, False])
def test_fact_check_caches_final_rewrite_result(tmp_path, passed):
    chat = ScriptedChat(claims("wrong"), good_reply(),
                        claims("correct" if passed else "misleading"))
    job = scripted_job(tmp_path, "wing-and-turbine")
    b = brand("wing-and-turbine")
    ctx = ctx_for(tmp_path, chat)
    for _ in range(2):
        if passed:
            fact_check.run(job, b, ctx)
        else:
            with pytest.raises(StageError, match="misleading"):
                fact_check.run(job, b, ctx)
    assert chat.roles == ["fact_check", "polish", "fact_check"]
    assert len(json.loads((tmp_path / "fact_check_claims.json").read_text())) == 2


def test_fact_check_cache_requires_audit_and_current_brand(tmp_path):
    chat = ScriptedChat(*[claims("correct")] * 3)
    job = scripted_job(tmp_path, "wing-and-turbine")
    b = brand("wing-and-turbine")
    ctx = ctx_for(tmp_path, chat)
    fact_check.run(job, b, ctx)
    (tmp_path / "fact_check_claims.json").unlink()
    fact_check.run(job, b, ctx)
    fact_check.run(job, b.model_copy(update={"niche": "Updated educational niche"}), ctx)
    assert chat.roles == ["fact_check"] * 3


def test_mock_fact_check_uses_claims_from_input(tmp_path):
    chat = MockChatClient()
    job = scripted_job(tmp_path, "wing-and-turbine")
    fact_check.run(job, brand("wing-and-turbine"), ctx_for(tmp_path, chat))
    reviewed = json.loads((tmp_path / "fact_check_claims.json").read_text())[0]["claims"]
    assert job.fact_check.passed and job.fact_check.model == "mock"
    assert job.script.hook in {c["claim"] for c in reviewed}
    assert job.script.beats[1].overlay in {c["claim"] for c in reviewed}


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
