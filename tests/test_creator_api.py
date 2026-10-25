"""Offline creator boundaries; no subprocesses, models or provider calls."""
import hashlib
import io
import json
import threading
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from studio.web.creator_api import register_creator
from studio.web.creator_models import CreationRender
from studio.web.security import Security
from studio.web.store import Store
from studio.web.supervisor import FatalCleanupError

BASE = "/api/creator"
BRIEF = {"idea": "An original fox explores the cloud forest", "duration": 10,
         "style": "watercolor-anime"}


def planner(brief):
    return {"title": "Cloud forest", "hook": "A tiny discovery",
            "shots": [{"number": i + 1, "prompt": "An original fox in mist, no text",
                       "caption": "Look closely"} for i in range(brief.duration // 5)],
            "description": "An original forest journey", "hashtags": ["#forest"]}


def cloud():
    return {"enabled": True, "available": True, "cost_per_second_usd": 0.05,
            "reason": "/private/provider-secret", "token": "private-provider-secret"}


def supervisor(command, **kwargs):
    assert command[1:3] == ["-m", "studio.web.worker"]
    out = Path(command[-1])
    assert Path(command[command.index("--project") + 1]) == out / "creation.json"
    assert kwargs["timeout_seconds"] == 900
    assert json.loads((out / "request.json").read_text())["kind"] == "creation"
    (out / "video.mp4").write_bytes(bytes(range(128)))
    (out / "poster.png").write_bytes(b"offline-png")
    (out / "captions.srt").write_text("1\n00:00:00,000 --> 00:00:05,000\nLook closely\n")
    (out / "description.txt").write_text("Cloud forest\nAn original journey\n#forest\n")
    (out / "metadata.json").write_text(json.dumps({"width": 1080, "height": 1920,
        "source_width": 480, "source_height": 832, "token": "private-provider-secret"}))
    (out / "result.json").write_text(json.dumps({"kind": "creation", "guide_audio": True,
                                                "publishable": True}))
    return {"returncode": 0}


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.delenv("STUDIO_CREATOR_CLOUD_ENABLED", raising=False)
    monkeypatch.delenv("STUDIO_CREATOR_CLOUD_BUDGET_USD", raising=False)


def make_app(root, *, worker=supervisor, planning=planner, status=cloud, token=None):
    store = Store(root)
    service = None

    @asynccontextmanager
    async def lifespan(app):
        service.start()
        try:
            yield
        finally:
            service.stop()
            store.db.close()

    app = FastAPI(lifespan=lifespan)
    app.middleware("http")(Security("127.0.0.1", token).middleware)

    @app.exception_handler(RequestValidationError)
    async def validation(request, exc):
        return JSONResponse({"detail": "Invalid request"}, 422)

    service = register_creator(app, store, supervisor=worker, planner=planning, cloud_status=status)
    app.state.creator = service
    app.state.store = store
    return app


@pytest.fixture
def client(tmp_path):
    with TestClient(make_app(tmp_path)) as client:
        yield client


def create(client, **extra):
    response = client.post(BASE + "/projects", json={**BRIEF, **extra})
    assert response.status_code == 200, response.text
    return response.json()


def planned(client, **extra):
    project = create(client, **extra)
    response = client.post(f"{BASE}/projects/{project['id']}/plan")
    assert response.status_code == 200, response.text
    return response.json()


def submit(client, project, **extra):
    return client.post(f"{BASE}/projects/{project['id']}/jobs", json=extra)


def wait(client, job):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result = client.get(f"{BASE}/jobs/{job['id']}").json()
        if result["status"] in {"succeeded", "failed", "cancelled"}:
            return result
        time.sleep(0.01)
    pytest.fail("Mocked queue did not finish")


def save_body(project):
    return {key: project[key] for key in ("revision", "brief", "plan")}


def test_projects_planning_revisions_and_sqlite_restart(tmp_path):
    with TestClient(make_app(tmp_path)) as client:
        project = planned(client)
        url = f"{BASE}/projects/{project['id']}"
        assert project["revision"] == 2
        body = save_body(project)
        body["plan"]["title"] = "A new title"
        saved = client.put(url, json=body).json()
        assert saved["revision"] == 3
        assert client.put(url, json=body).status_code == 409
        assert client.get(BASE + "/projects").json()["projects"][0] == saved
    with TestClient(make_app(tmp_path)) as client:
        assert client.get(url).json() == saved
        assert client.app.state.store.all("creation_event")


@pytest.mark.parametrize("style", ["watercolor-anime", "anime", "cartoon", "live-action",
                                    "cinematic", "motion-graphics"])
def test_all_styles(client, style):
    assert create(client, style=style)["brief"]["style"] == style


@pytest.mark.parametrize("extra", [
    {"audience": "kids"}, {"duration": 11}, {"duration": 65}, {"style": "other"},
    {"endpoint": "http://private"}, {"model": "paid"}, {"platforms": ["youtube", "youtube"]},
])
def test_invalid_briefs(client, extra):
    assert client.post(BASE + "/projects", json={**BRIEF, **extra}).status_code == 422


def test_kids_youtube_only(client):
    project = planned(client, audience="kids", platforms=["youtube"])
    job = wait(client, submit(client, project).json())
    assert job["publishable"] is False
    with zipfile.ZipFile(io.BytesIO(client.get(job["package_url"]).content)) as archive:
        meta = json.loads(archive.read("metadata.json"))
        assert meta["made_for_kids"] and meta["platforms"] == ["youtube"]


@pytest.mark.parametrize("change", ["duration", "duplicate"])
def test_plan_invariants(client, change):
    project = planned(client)
    body = save_body(project)
    if change == "duration":
        body["plan"]["shots"].pop()
    else:
        body["plan"]["shots"][1]["number"] = 1
    assert client.put(f"{BASE}/projects/{project['id']}", json=body).status_code == 422


def test_stale_plan_and_missing_plan(client):
    project = create(client)
    assert submit(client, project).status_code == 409
    project = client.post(f"{BASE}/projects/{project['id']}/plan").json()
    body = save_body(project)
    body["brief"]["idea"] = "A different original forest story"
    url = f"{BASE}/projects/{project['id']}"
    assert client.put(url, json=body).status_code == 409
    body["plan"] = None
    assert client.put(url, json=body).status_code == 200


def test_render_ranges_package_and_private_metadata(client):
    project = planned(client)
    job = wait(client, submit(client, project).json())
    assert job["status"] == "succeeded", job
    assert job["draft"] and job["review_required"] and not job["publishable"]
    assert "dispatched" not in job
    video = client.get(job["video_url"], headers={"Range": "bytes=2-5"})
    assert video.status_code == 206 and video.content == bytes(range(2, 6))
    assert client.get(job["video_url"], headers={"Range": "bytes=-2"}).content == bytes([126, 127])
    assert client.get(job["video_url"], headers={"Range": "bytes=999-"}).status_code == 416
    assert "attachment" in client.get(job["download_url"]).headers["content-disposition"]
    assert client.get(job["poster_url"]).status_code == 200
    response = client.get(job["package_url"])
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert set(archive.namelist()) == {"video.mp4", "poster.png", "captions.srt", "description.txt",
                                          "creation.json", "brief.json", "plan.json", "metadata.json",
                                          "review.json"}
        metadata = json.loads(archive.read("metadata.json"))
        assert metadata["sha256"]["video.mp4"] == hashlib.sha256(bytes(range(128))).hexdigest()
        assert metadata["source_width"] == 480 and metadata["width"] == 1080
        assert "private-provider-secret" not in archive.read("metadata.json").decode()
        assert not metadata["approved"] and not metadata["publishable"]
        assert json.loads(archive.read("creation.json")) == project
    body = save_body(project)
    body["plan"]["title"] = "Changed after generation"
    assert client.put(f"{BASE}/projects/{project['id']}", json=body).status_code == 200
    assert client.get(f"{BASE}/jobs/{job['id']}").json()["stale"]
    assert str(client.app.state.store.root) not in json.dumps(job)
    assert len(client.get(BASE + "/jobs", params={"project_id": project["id"]}).json()["jobs"]) == 1


def test_cloud_disabled_without_server_optin_or_cap(client, monkeypatch):
    project = planned(client)
    assert submit(client, project, mode="cloud", max_cost_usd=1).status_code == 409
    monkeypatch.setenv("STUDIO_CREATOR_CLOUD_ENABLED", "1")
    assert submit(client, project, mode="cloud", max_cost_usd=1).status_code == 409
    monkeypatch.setenv("STUDIO_CREATOR_CLOUD_BUDGET_USD", "1")
    assert submit(client, project, mode="cloud", max_cost_usd=0.1).status_code == 422
    assert submit(client, project, mode="cloud", max_cost_usd=2).status_code == 409
    assert submit(client, project, mode="cloud", max_cost_usd=1, enabled=True).status_code == 422
    job = wait(client, submit(client, project, mode="cloud", max_cost_usd=1).json())
    assert job["status"] == "succeeded" and job["estimated_cost_usd"] == 0.5
    reservation = client.app.state.store.get("creation_reservation", job["id"])
    assert reservation["amount_usd"] == 1 and reservation["status"] == "unknown"
    assert submit(client, project, mode="cloud", max_cost_usd=1).status_code == 409
    status = client.get(BASE + "/status")
    assert status.json()["cloud"]["remaining_budget_usd"] == 0
    assert "private-provider-secret" not in status.text and "/private/" not in status.text


def test_guard_blocks_without_launch_and_releases_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_CREATOR_CLOUD_ENABLED", "1")
    monkeypatch.setenv("STUDIO_CREATOR_CLOUD_BUDGET_USD", "1")
    def blocked(*args, **kwargs):
        return {"returncode": None, "resource_stopped": True, "reason": "Memory pressure yellow"}
    with TestClient(make_app(tmp_path, worker=blocked)) as client:
        job = wait(client, submit(client, planned(client), mode="cloud", max_cost_usd=1).json())
        assert job["status"] == "failed"
        assert client.app.state.store.get("creation_reservation", job["id"])["status"] == "released"
        assert client.get(f"{BASE}/jobs/{job['id']}/video").status_code == 409


def test_single_queue_active_conflicts_and_cancellation(tmp_path):
    entered = threading.Event()
    calls = []
    def blocking(command, **kwargs):
        calls.append(command)
        entered.set()
        assert kwargs["cancel_event"].wait(3)
        return {"returncode": -15, "cancelled": True}
    with TestClient(make_app(tmp_path, worker=blocking)) as client:
        first, second = planned(client), planned(client)
        running = submit(client, first).json()
        assert entered.wait(1)
        queued = submit(client, second).json()
        assert queued["status"] == "queued"
        assert submit(client, first).status_code == 409
        url = f"{BASE}/projects/{first['id']}"
        assert client.post(url + "/plan").status_code == 409
        assert client.put(url, json=save_body(first)).status_code == 409
        assert client.post(f"{BASE}/jobs/{queued['id']}/cancel").json()["status"] == "cancelled"
        client.post(f"{BASE}/jobs/{running['id']}/cancel")
        assert wait(client, running)["status"] == "cancelled"
        assert len(calls) == 1


def test_restart_never_replays_cloud_and_keeps_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_CREATOR_CLOUD_ENABLED", "1")
    monkeypatch.setenv("STUDIO_CREATOR_CLOUD_BUDGET_USD", "3")
    app = make_app(tmp_path)
    with TestClient(app) as client:
        project = planned(client)
    # Persist interrupted jobs without invoking the worker.
    app = make_app(tmp_path)
    service = app.state.creator
    first = service.submit(project["id"], CreationRender(mode="cloud", max_cost_usd=1))
    other = dict(project, id="other")
    service.store.put("creation", other)
    second = service.submit("other", CreationRender(mode="cloud", max_cost_usd=1))
    running = service.store.get("creation_job", second["id"])
    running.update(status="running", dispatched=True)
    service.store.put("creation_job", running)
    service.store.db.close()
    def never(*args, **kwargs):
        pytest.fail("Restart replayed work")
    with TestClient(make_app(tmp_path, worker=never)) as client:
        for job in (first, second):
            result = client.get(f"{BASE}/jobs/{job['id']}").json()
            assert result["status"] == "failed" and "reconciliation" in result["error"]
        store = client.app.state.store
        assert store.get("creation_reservation", first["id"])["status"] == "released"
        assert store.get("creation_reservation", second["id"])["status"] == "unknown"


@pytest.mark.parametrize("fatal", [False, True])
def test_supervisor_errors_are_sanitized_and_poison(tmp_path, fatal):
    def broken(*args, **kwargs):
        raise (FatalCleanupError if fatal else RuntimeError)("/private/provider-token-secret")
    with TestClient(make_app(tmp_path, worker=broken)) as client:
        project = planned(client)
        job = wait(client, submit(client, project).json())
        assert job["status"] == "failed" and "provider-token-secret" not in json.dumps(job)
        assert client.app.state.creator.stopping.is_set()
        assert submit(client, project).status_code == 429


def test_planner_errors_and_invalid_output_sanitized(tmp_path):
    def broken(brief):
        raise RuntimeError("/private/planner-secret")
    with TestClient(make_app(tmp_path, planning=broken)) as client:
        project = create(client)
        result = client.post(f"{BASE}/projects/{project['id']}/plan")
        assert result.status_code == 503 and "planner-secret" not in result.text
        assert client.app.state.creator.planning == set()


def test_security_inherited(tmp_path):
    token = "test-creator-token-1234567890123456"
    with TestClient(make_app(tmp_path, token=token)) as client:
        assert client.get(BASE + "/projects").status_code == 401
        headers = {"Authorization": f"Bearer {token}"}
        assert client.get(BASE + "/projects", headers=headers).status_code == 200
        assert client.post(BASE + "/projects", json=BRIEF,
                           headers={**headers, "Origin": "https://foreign.test"}).status_code == 403
        assert client.get(BASE + "/status", headers={**headers, "Host": "foreign.test"}).status_code == 403
        assert client.post(BASE + "/jobs/unknown/approve", headers=headers).status_code == 404
        assert client.post(BASE + "/jobs/unknown/publish", headers=headers).status_code == 404


def test_provider_journal_sqlite_tracking_and_no_public_leak(tmp_path):
    def tracked(command, **kwargs):
        out = Path(command[-1])
        (out / "provider.json").write_text(json.dumps({"events": [
            {"event": "submitting", "shot": 0, "time": 1},
            {"event": "prediction", "shot": 0, "prediction_id": "provider123", "time": 2,
             "raw": "private-secret"},
            {"event": "status", "status": "succeeded", "prediction_id": "../private-secret"},
            {"event": "private-secret", "prediction_id": "ignored"},
        ]}))
        return supervisor(command, **kwargs)
    with TestClient(make_app(tmp_path, worker=tracked)) as client:
        job = wait(client, submit(client, planned(client)).json())
        events = client.app.state.store.all("creation_provider_event")
        assert len(events) == 3
        assert any(event.get("prediction_id") == "provider123" for event in events)
        assert "private-secret" not in json.dumps(events)
        assert "provider123" not in json.dumps(job)
        with zipfile.ZipFile(io.BytesIO(client.get(job["package_url"]).content)) as archive:
            metadata = json.loads(archive.read("metadata.json"))
            assert metadata["sha256"]["plan.json"] == hashlib.sha256(archive.read("plan.json")).hexdigest()
            assert "provider.json" not in archive.namelist()


def test_unknown_provider_cost_on_running_cancellation(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDIO_CREATOR_CLOUD_ENABLED", "1")
    monkeypatch.setenv("STUDIO_CREATOR_CLOUD_BUDGET_USD", "1")
    entered = threading.Event()
    def blocking(command, **kwargs):
        entered.set()
        assert kwargs["cancel_event"].wait(3)
        return {"returncode": -15, "cancelled": True}
    with TestClient(make_app(tmp_path, worker=blocking)) as client:
        job = submit(client, planned(client), mode="cloud", max_cost_usd=1).json()
        assert entered.wait(1)
        client.post(f"{BASE}/jobs/{job['id']}/cancel")
        assert wait(client, job)["status"] == "cancelled"
        assert client.app.state.store.get("creation_reservation", job["id"])["status"] == "unknown"


def test_nursery_routes_remain_separate(tmp_path):
    from studio.web.api import create_app
    with TestClient(create_app(tmp_path, supervisor=supervisor)) as client:
        nursery = client.post("/api/projects", json={})
        assert nursery.status_code == 200
        assert nursery.json()["made_for_kids"] is True
        project = create(client)
        assert client.get(BASE + "/projects").json()["projects"] == [project]
        assert len(client.get("/api/projects").json()["projects"]) == 1


def test_planner_http_exception_is_not_forwarded(tmp_path):
    from fastapi import HTTPException
    def broken(brief):
        raise HTTPException(502, "/private/planner-token")
    with TestClient(make_app(tmp_path, planning=broken)) as client:
        project = create(client)
        result = client.post(f"{BASE}/projects/{project['id']}/plan")
        assert result.status_code == 503 and "planner-token" not in result.text


def test_planning_does_not_hold_sqlite_lock_and_blocks_duplicate(tmp_path):
    entered, release = threading.Event(), threading.Event()
    def blocking(brief):
        entered.set()
        assert release.wait(3)
        return planner(brief)
    with TestClient(make_app(tmp_path, planning=blocking)) as client:
        project = create(client)
        other = create(client)
        url = f"{BASE}/projects/{project['id']}"
        responses = []
        thread = threading.Thread(target=lambda: responses.append(client.post(url + "/plan")))
        thread.start()
        try:
            assert entered.wait(1)
            assert client.get(BASE + "/projects").status_code == 200
            assert client.post(url + "/plan").status_code == 409
            assert client.post(f"{BASE}/projects/{other['id']}/plan").status_code == 429
            assert submit(client, project).status_code == 409
        finally:
            release.set()
            thread.join(3)
        assert responses[0].status_code == 200
        assert client.post(f"{BASE}/projects/{other['id']}/plan").status_code == 200


def test_cloud_narration_requires_capability(client, monkeypatch):
    monkeypatch.setenv("STUDIO_CREATOR_CLOUD_ENABLED", "1")
    monkeypatch.setenv("STUDIO_CREATOR_CLOUD_BUDGET_USD", "2")
    project = planned(client, audio="narration")
    response = submit(client, project, mode="cloud", max_cost_usd=1)
    assert response.status_code == 409 and "narration" in response.text


@pytest.mark.parametrize("numbers", [[2, 1], [1, 3], [2, 3]])
def test_shot_numbers_are_contiguous_in_timeline_order(client, numbers):
    project = planned(client)
    body = save_body(project)
    for shot, number in zip(body["plan"]["shots"], numbers, strict=True):
        shot["number"] = number
    assert client.put(f"{BASE}/projects/{project['id']}", json=body).status_code == 422


def test_duplicate_cast_names_rejected_in_brief_and_plan(client):
    characters = [{"name": "Fox", "description": "Original orange fox"},
                  {"name": " fox ", "description": "Original blue fox"}]
    assert client.post(BASE + "/projects", json={**BRIEF, "characters": characters}).status_code == 422
    project = planned(client)
    body = save_body(project)
    body["plan"]["characters"] = characters
    assert client.put(f"{BASE}/projects/{project['id']}", json=body).status_code == 422


def test_planner_output_cannot_bypass_timeline_order(tmp_path):
    def bad_order(brief):
        value = planner(brief)
        value["shots"].reverse()
        return value
    with TestClient(make_app(tmp_path, planning=bad_order)) as client:
        project = create(client)
        assert client.post(f"{BASE}/projects/{project['id']}/plan").status_code == 422
        assert client.get(f"{BASE}/projects/{project['id']}").json()["plan"] is None
