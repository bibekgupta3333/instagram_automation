"""Runner tests: mock end-to-end render, idempotency, approval binding, stage-by-stage batches."""

import shutil
from pathlib import Path

import pytest

from studio.config import DEFAULT_BRAND_DIR, Settings, load_brand
from studio.contracts import JobStatus, VideoJob
from studio.pipeline import runner
from studio.pipeline.mock_llm import MockChatClient
from studio.pipeline.runner import (
    JobSpec,
    approve,
    build_context,
    load_job,
    make_video,
    run_batch,
    save_job,
    verify_approval,
)
from studio.stages import render, tts
from studio.stages.base import StageError, ffprobe

needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg not installed"
)
TOPIC = "What if the Moon disappeared?"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(output_dir=tmp_path / "output", brand_dir=DEFAULT_BRAND_DIR)


def short_video(settings, channel="gravity-what-if", topic=TOPIC, fmt="short", beats=2, **kw):
    return make_video(channel, topic, fmt, mock=True, chat=MockChatClient(beats=beats),
                      settings=settings, **kw)


# ---------------------------------------------------------------- end to end (ffmpeg)


@needs_ffmpeg
def test_mock_end_to_end_short(settings):
    job = short_video(settings, beats=3)
    assert job.status == JobStatus.RENDERED, job.log
    probe = ffprobe(job.video_path)
    video = next(s for s in probe["streams"] if s["codec_type"] == "video")
    audio = [s for s in probe["streams"] if s["codec_type"] == "audio"]
    assert (video["width"], video["height"]) == (1080, 1920)
    assert video["r_frame_rate"] == "30/1" and video["codec_name"] == "h264"
    assert audio and audio[0]["codec_name"] == "aac"
    segs = tts.segments(job.script)
    speed = tts.delivery(load_brand(job.channel, DEFAULT_BRAND_DIR))[1]  # energy floor
    expected = sum(tts.estimate_seconds(s["text"], speed) for s in segs) + tts.GAP_S * (len(segs) - 1)
    assert abs(float(probe["format"]["duration"]) - expected) < 0.5
    work = Path(job.workdir)
    for name in ("job.json", "script.json", "narration.wav", "captions.ass", "captions.srt",
                 "content_hash.txt", "video.mp4"):
        assert (work / name).exists(), name
    assert len(job.image_paths) == 3
    assert job.content_hash == render.compute_content_hash(job)
    assert load_job(job.job_id, settings).status == JobStatus.RENDERED


@needs_ffmpeg
def test_mock_end_to_end_long_is_landscape(settings):
    job = short_video(settings, topic="How a dam is poured", channel="megabuild-blueprint",
                      fmt="long", beats=1)
    video = next(s for s in ffprobe(job.video_path)["streams"] if s["codec_type"] == "video")
    assert (video["width"], video["height"]) == (1920, 1080)


@needs_ffmpeg
def test_second_run_skips_every_stage(settings):
    first = short_video(settings)
    mtime = Path(first.video_path).stat().st_mtime_ns
    n_logs = len(first.log)
    second = short_video(settings)
    assert second.job_id == first.job_id
    new = second.log[n_logs:]
    for stage in ("script", "fact_check", "tts", "captions", "images", "render"):
        assert any(line.endswith(f"{stage}: cached, skipped") for line in new), stage
    assert Path(second.video_path).stat().st_mtime_ns == mtime
    assert second.content_hash == first.content_hash
    for stage in ("script", "fact_check", "tts", "captions", "images", "render"):
        assert (Path(first.workdir) / ".cache" / f"{stage}.json").exists()


@needs_ffmpeg
def test_input_change_reruns_downstream_only(settings):
    job = short_video(settings)
    n = len(job.log)
    ctx = build_context(True, {"tts": "silence"}, MockChatClient(beats=2), settings)
    job = make_video(job.channel, job.topic, mock=True, ctx=ctx)
    new = job.log[n:]
    assert any(line.endswith("script: cached, skipped") for line in new)
    assert any("tts: silence" in line for line in new)
    assert any(line.endswith("images: cached, skipped") for line in new)
    assert any("render: h264" in line or "render: libx264" in line for line in new)


@needs_ffmpeg
def test_approval_binds_to_content_hash(settings):
    job = short_video(settings)
    approved = approve(job.job_id, "bibek", settings)
    assert approved.status == JobStatus.APPROVED
    assert approved.approved_hash == approved.content_hash and verify_approval(approved)

    # an unchanged re-run keeps the approval
    again = short_video(settings)
    assert again.status == JobStatus.APPROVED and verify_approval(again)

    # a content change (caption edit) changes the hash and voids the approval on re-render
    again.script.description = "Edited caption after approval."
    save_job(again)
    after = render.run(again, None, build_context(True, None, MockChatClient(), settings))
    assert after.content_hash != approved.approved_hash
    assert after.approved_hash is None and after.status == JobStatus.RENDERED
    assert not verify_approval(after)
    assert any("approval by bibek voided" in line for line in after.log)


@needs_ffmpeg
def test_tampered_video_is_not_approvable(settings):
    job = short_video(settings)
    approve(job.job_id, "bibek", settings)
    with Path(job.video_path).open("ab") as fh:
        fh.write(b"tamper")
    assert not verify_approval(load_job(job.job_id, settings))
    with pytest.raises(StageError, match="changed since render"):
        approve(job.job_id, "bibek", settings)


# ---------------------------------------------------------------- batch order (no ffmpeg)


def test_batch_runs_stage_by_stage(settings, monkeypatch):
    calls: list[tuple[str, str]] = []
    unloads: list[str] = []

    def fake(name):
        def run(job, brand, ctx):
            calls.append((name, job.topic))
            if name == "tts" and job.topic == "B":
                raise StageError("boom")
            return job
        return run

    monkeypatch.setattr(runner, "STAGES", [(n, fake(n)) for n, _ in runner.STAGES])
    monkeypatch.setattr(runner, "UNLOAD", {n: (lambda n=n: unloads.append(n))
                                           for n in ("tts", "captions", "images")})
    specs = [JobSpec("gravity-what-if", t) for t in ("A", "B", "C")]
    jobs = run_batch(specs, mock=True, chat=MockChatClient(), settings=settings)

    order = [name for name, _ in calls]
    stages = [n for n, _ in runner.STAGES]
    # every stage runs for all (live) jobs before the next stage starts
    assert order == sorted(order, key=stages.index)
    assert calls[:3] == [("script", "A"), ("script", "B"), ("script", "C")]
    assert ("captions", "B") not in calls and ("render", "C") in calls  # failure isolated
    assert unloads == ["tts", "captions", "images"]
    status = {j.topic: j.status for j in jobs}
    assert status["B"] == JobStatus.FAILED and status["A"] != JobStatus.FAILED
    on_disk = VideoJob.model_validate_json((Path(jobs[1].workdir) / "job.json").read_text())
    assert on_disk.status == JobStatus.FAILED


def test_job_ids_are_deterministic_and_resume(settings):
    a = runner.plan_job("gravity-what-if", TOPIC, "short", settings)
    b = runner.plan_job("gravity-what-if", TOPIC, "short", settings)
    c = runner.plan_job("gravity-what-if", TOPIC, "long", settings)
    assert a.job_id == b.job_id != c.job_id
    assert (settings.output_dir / a.job_id / "job.json").exists()


def test_missing_real_engine_fails_with_clear_error(settings):
    try:
        import mlx_audio  # noqa: F401
        pytest.skip("mlx-audio installed")
    except ImportError:
        pass
    ctx = build_context(False, {"tts": "kokoro", "captions": "mock", "images": "mock"},
                        MockChatClient(beats=1), settings)
    job = make_video("gravity-what-if", "Kokoro check", ctx=ctx)
    assert job.status == JobStatus.FAILED
    assert "needs mlx-audio" in job.log[-1]
