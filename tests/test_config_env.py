"""The dependency-free .env loader in studio/config.py."""

import os

import pytest

from studio import config


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(config, "_loaded_env_files", set())
    for k in ("STUDIO_ENV_FILE", "ENVT_A", "ENVT_B", "ENVT_C", "ENVT_D", "ENVT_E", "ENVT_F",
              "ENVT_EMPTY", "STUDIO_TTS_ENGINE"):
        monkeypatch.delenv(k, raising=False)
    yield
    for k in ("ENVT_A", "ENVT_B", "ENVT_C", "ENVT_D", "ENVT_E", "ENVT_F", "ENVT_EMPTY",
              "STUDIO_TTS_ENGINE"):
        os.environ.pop(k, None)


ENV_TEXT = """
# a comment
ENVT_A=plain

ENVT_B="double quoted # not a comment"
ENVT_C='single quoted'
export ENVT_D=exported
ENVT_E=value # trailing comment
ENVT_EMPTY=
not a line
ENVT_F = spaced
"""


def test_parse_env_text():
    got = config.parse_env_text(ENV_TEXT)
    assert got == {"ENVT_A": "plain", "ENVT_B": "double quoted # not a comment",
                   "ENVT_C": "single quoted", "ENVT_D": "exported", "ENVT_E": "value",
                   "ENVT_EMPTY": "", "ENVT_F": "spaced"}


def test_load_never_overrides_existing(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text(ENV_TEXT)
    monkeypatch.setenv("ENVT_A", "from-shell")
    keys = config.load_env_file(path)
    assert os.environ["ENVT_A"] == "from-shell"
    assert os.environ["ENVT_C"] == "single quoted"
    assert "ENVT_EMPTY" not in os.environ  # empty values are skipped
    assert "ENVT_A" not in keys and "ENVT_C" in keys
    assert config.load_env_file(tmp_path / "missing.env") == []


def test_settings_from_env_loads_studio_env_file_once(tmp_path, monkeypatch):
    path = tmp_path / "custom.env"
    path.write_text("STUDIO_TTS_ENGINE=from-dotenv\nENVT_A=1\n")
    monkeypatch.setenv("STUDIO_ENV_FILE", str(path))
    assert config.Settings.from_env().engines["tts"] == "from-dotenv"
    os.environ.pop("ENVT_A")
    config.get_settings()  # second call: already loaded, does not re-read the file
    assert "ENVT_A" not in os.environ


def test_repo_env_not_loaded_under_pytest_and_can_be_disabled(monkeypatch):
    assert config.env_file_path() is None  # offline suite never reads the real .env
    monkeypatch.setenv("STUDIO_ENV_FILE", "none")
    assert config.env_file_path() is None
    assert config.load_dotenv_once() == []


def test_comment_only_value_is_empty():
    from studio.config import parse_env_text

    parsed = parse_env_text("BEDROCK_MODEL_ID_FAST=        # drafts (Haiku-class)\nA=x # note\n")
    assert parsed["BEDROCK_MODEL_ID_FAST"] == ""
    assert parsed["A"] == "x"
