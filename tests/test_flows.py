import sys
import types
from dataclasses import dataclass
from datetime import date

from studio import flows
from studio.flows import daily


def test_imports_without_prefect():
    assert flows.PREFECT_AVAILABLE is (daily.PREFECT_MISSING == "")
    if not flows.PREFECT_AVAILABLE:
        assert "uv sync --extra flows" in flows.PREFECT_MISSING


def test_flows_call_run_batch_with_accepted_kwargs(monkeypatch):
    seen = []

    @dataclass
    class JobSpec:
        channel: str
        topic: str
        format: str = "short"

    def run_batch(jobs, mock=False):  # mirrors studio.pipeline.runner.run_batch (no phase)
        seen.append((jobs, mock))
        return ["done"]

    fake = types.ModuleType("studio.pipeline.runner")
    fake.run_batch = run_batch
    fake.JobSpec = JobSpec
    monkeypatch.setitem(sys.modules, "studio.pipeline.runner", fake)
    if daily.PREFECT_AVAILABLE:  # pragma: no cover
        return
    jobs = [{"channel": "c01", "topic": "attic fans"}]
    assert flows.daily_text_flow(day=date(2026, 10, 20), jobs=jobs, mock=True) == ["done"]
    assert flows.overnight_gpu_flow(jobs=jobs, mock=True) == ["done"]
    assert seen == [([JobSpec("c01", "attic fans")], True)] * 2


def test_day_plan_loaded_from_yaml(monkeypatch, tmp_path):
    monkeypatch.setenv("STUDIO_PLAN_DIR", str(tmp_path))
    (tmp_path / "2026-10-20.yaml").write_text("- {channel: c01, topic: t}\n")
    assert daily.load_day_plan(date(2026, 10, 20)) == [{"channel": "c01", "topic": "t"}]
    assert daily.load_day_plan(date(2026, 10, 21)) == []
