"""Offline tests for the youtube-script project skill's validator (.claude/skills/youtube-script)."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parents[1] / ".claude" / "skills" / "youtube-script"
VALIDATOR = SKILL / "scripts" / "validate_script.py"
GOOD = SKILL / "examples" / "wing-and-turbine-short.json"
BAD = SKILL / "tests" / "bad-script.json"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def validator():
    return _load("skill_validate_script", VALIDATOR)


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(VALIDATOR), *args], check=False,
                          capture_output=True, text=True, timeout=60)


def test_good_sample_passes_cli():
    res = _run(str(GOOD), "--channel", "wing-and-turbine")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "PASS" in res.stdout
    assert "studio policy lint ran for wing-and-turbine" in res.stdout


def test_good_sample_matches_contract():
    from studio.contracts import Script

    Script.model_validate_json(GOOD.read_text())


def test_bad_sample_fails_cli():
    res = _run(str(BAD), "--channel", "wing-and-turbine")
    assert res.returncode == 1, res.stdout
    assert "FAIL" in res.stdout


def test_bad_sample_reports_each_check(validator):
    result = validator.validate(json.loads(BAD.read_text()), "wing-and-turbine", "short")
    assert not result["ok"]
    text = "\n".join(result["errors"])
    for needle in (
        "hook is",  # longer than 2 s at 150 wpm
        "beats; a short needs 8-12",
        "spoken length",
        "overlay is 7 words",
        "asks for text in the image",
        "contains numbers",
        "AI cliche",
        "credential claim",
        "sources' is empty",
        "spoken disclosure",
        "loop line",
        "policy [crash_content]",  # brand red line via studio.stages.policy
        "policy [banned_template]",
    ):
        assert needle in text, f"missing {needle!r} in:\n{text}"


def test_without_studio_still_checks_structure(validator):
    result = validator.validate(json.loads(BAD.read_text()), "wing-and-turbine", "short",
                                use_studio=False)
    assert not result["ok"]
    assert not any(e.startswith("policy ") for e in result["errors"])
    assert any("not checked" in i for i in result["info"])
    good = validator.validate(json.loads(GOOD.read_text()), None, "short", use_studio=False)
    assert good["ok"], good["errors"]


def test_negated_text_words_are_allowed(validator):
    data = json.loads(GOOD.read_text())
    data["beats"][0]["visual_prompt"] += ", no text, no logos or labels"
    assert validator.validate(data, None, "short", use_studio=False)["ok"]


def test_schema_errors(validator):
    result = validator.validate({"title": "x", "beats": [{"text": "a"}], "extra": 1}, None,
                                "short")
    text = "\n".join(result["errors"])
    assert "missing required field 'hook'" in text
    assert "non-empty string 'visual_prompt'" in text
    assert "unknown top-level keys" in text


def test_affiliate_disclosure_passes_when_spoken_and_written(validator):
    data = json.loads(GOOD.read_text())
    data["beats"][-1]["text"] = "If you use our links, we earn a commission."
    data["description"] += " Affiliate links below; we earn a commission."
    errors = validator.validate(data, None, "short", use_studio=False)["errors"]
    assert not any("disclosure" in e for e in errors), errors


def test_unknown_channel_exits_2():
    res = _run(str(GOOD), "--channel", "no-such-channel")
    assert res.returncode == 2


def test_channel_rules_reference_is_current():
    mod = _load("skill_build_channel_rules", SKILL / "scripts" / "build_channel_rules.py")
    assert mod.OUT.read_text() == mod.render(), "run scripts/build_channel_rules.py"
