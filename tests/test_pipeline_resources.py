"""Unexpected backend errors must not leave a model resident between batches."""

import pytest

from studio.config import DEFAULT_BRAND_DIR, Settings
from studio.pipeline import runner


def test_batch_unloads_model_on_unexpected_exception(tmp_path, monkeypatch):
    unloaded = []

    def fails(job, brand, ctx):
        raise RuntimeError("backend failure")

    monkeypatch.setattr(runner, "STAGES", [("tts", fails)])
    monkeypatch.setattr(runner, "UNLOAD", {"tts": lambda: unloaded.append("tts")})
    with pytest.raises(RuntimeError, match="backend failure"):
        runner.run_batch(
            [runner.JobSpec("car-cost-truth", "memory cleanup")], mock=True,
            settings=Settings(output_dir=tmp_path, brand_dir=DEFAULT_BRAND_DIR),
        )
    assert unloaded == ["tts"]
