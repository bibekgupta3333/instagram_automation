"""Local mflux engines must never trigger an implicit multi-GB download (doc 18 §3)."""

import sys
import types

import pytest

from studio.stages import images
from studio.stages.base import EngineUnavailable


def test_mflux_models_use_prequantized_repos_without_q_flag():
    for _name, cmd in images.MFLUX_MODELS.values():
        repo = images._mflux_repo(cmd)
        assert repo and repo.startswith("mflux-community/") and repo.endswith("-q4")
        assert "-q" not in cmd


def test_hf_cached_detects_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    repo = "mflux-community/flux2-klein-4b-mflux-q4"
    assert not images._hf_cached(repo)
    snap = tmp_path / "hub" / "models--mflux-community--flux2-klein-4b-mflux-q4" / "snapshots" / "x"
    snap.mkdir(parents=True)
    (snap / "transformer.safetensors").write_bytes(b"0")
    assert images._hf_cached(repo)


def test_missing_model_raises_instead_of_downloading(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "mflux", types.ModuleType("mflux"))
    monkeypatch.setattr(images.shutil, "which", lambda _c: "/usr/local/bin/mflux-generate-flux2")
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    monkeypatch.delenv("STUDIO_ALLOW_MODEL_DOWNLOAD", raising=False)
    with pytest.raises(EngineUnavailable, match="not downloaded"):
        images._mflux_cmd("flux2-klein-4b")
