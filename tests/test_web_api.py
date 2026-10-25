"""Offline API boundary tests; workers are mocked or pure Pillow illustration only."""
import hashlib
import io
import json
import threading
import time
import wave
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="Install the optional web extra or run make test-web")
from fastapi.testclient import TestClient
from PIL import Image

from studio.web.api import create_app
from studio.web.supervisor import FatalCleanupError


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    for name in ("STUDIO_WEB_TOKEN", "STUDIO_WEB_HOST", "STUDIO_WEB_ALLOWED_HOSTS"):
        monkeypatch.delenv(name, raising=False)


def png():
    data = io.BytesIO()
    Image.new("RGB", (32, 32), "orange").save(data, "PNG")
    return data.getvalue()


def wav():
    data = io.BytesIO()
    with wave.open(data, "wb") as sound:
        sound.setnchannels(1)
        sound.setsampwidth(2)
        sound.setframerate(8000)
        sound.writeframes(b"\0\0" * 8000)
    return data.getvalue()


def render_supervisor(command, *, cwd, log_path, cancel_event, timeout_seconds):
    out = Path(command[-1])
    request = json.loads((out / "request.json").read_text())
    project = json.loads((out / "project.json").read_text())
    assert command[:3] == [command[0], "-m", "studio.web.worker"]
    assert timeout_seconds == 900
    assert Path(request["assets_dir"]).is_absolute()
    assert all(Path(p).is_file() for p in request["asset_paths"].values())
    if request["kind"] == "character":
        # Exercise the real worker/renderer contract, with no models or subprocess rendering.
        from studio.web.worker import run
        run(project, request, out)
    else:
        (out / "video.mp4").write_bytes(bytes(range(256)))
        (out / "poster.png").write_bytes(png())
        (out / "captions.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nHello")
        (out / "metadata.json").write_text("{}")
        (out / "result.json").write_text(json.dumps({
            "kind": "render", "guide_audio": project["audio"]["mode"] != "uploaded"}))
    return {"returncode": 0, "reason": None, "cancelled": False, "resource_stopped": False}


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path, supervisor=render_supervisor)) as instance:
        yield instance


def create(client, **kwargs):
    result = client.post("/api/projects", json=kwargs)
    assert result.status_code == 200, result.text
    return result.json()


def wait_job(client, job):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        value = client.get(f"/api/jobs/{job['id']}").json()
        if value["status"] in {"succeeded", "failed", "cancelled"}:
            return value
        time.sleep(0.01)
    pytest.fail("Offline worker did not finish")


def submit(client, project, **kwargs):
    response = client.post(f"/api/projects/{project['id']}/jobs", json={"kind": "render", **kwargs})
    assert response.status_code == 200, response.text
    return response.json()


def upload(client, project, data=None, kind="image", filename="otter.png"):
    return client.post(f"/api/projects/{project['id']}/assets",
                       files={"file": (filename, data if data is not None else png())},
                       data={"kind": kind, "rights_confirmed": "true"})


def test_templates_revisions_validation_and_persistence(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        for template in ("bubbles", "counting", "bedtime"):
            project = create(client, template=template)
            assert len(project["scenes"]) == 3
            assert sum(s["duration"] for s in project["scenes"]) == 15
            family = project["characters"]
            assert [c["name"] for c in family] == ["Pebble", "Fern", "Mum", "Bloop"]
            assert {c["species"] for c in family} == {"otter"}
            assert len({c["color"] for c in family}) == 4
            assert {c["accessory"] for c in family} == {"scarf", "bow", "none"}
            main_ids = [family[0]["id"], family[1]["id"]]
            assert all(scene["character_ids"] == main_ids for scene in project["scenes"])
        url = f"/api/projects/{project['id']}"
        project["name"] = "Original story"
        saved = client.put(url, json=project).json()
        assert saved["revision"] == project["revision"] + 1
        assert client.put(url, json=project).status_code == 409
        saved["made_for_kids"] = False
        assert client.put(url, json=saved).status_code == 422
        assert isinstance(client.put(url, json=saved).json()["detail"], str)
        assert client.get("/api/health").json() == {"status": "ok"}
    with TestClient(create_app(tmp_path)) as client:
        assert client.get(url).json()["name"] == "Original story"
        planned = client.post(url + "/plan", json={"topic": "Counting leaves", "template": "counting"})
        assert planned.json()["revision"] == saved["revision"] + 1
        assert [c["name"] for c in planned.json()["characters"]] == ["Pebble", "Fern", "Mum", "Bloop"]
        assert client.get("/api/projects").json()["projects"]


@pytest.mark.parametrize("mutation", ["duplicate", "references", "duration", "count", "path", "extra"])
def test_project_limits(client, mutation):
    project = create(client)
    project_id = project["id"]
    if mutation == "duplicate":
        project["characters"][1]["id"] = project["characters"][0]["id"]
    elif mutation == "references":
        project["scenes"][0]["character_ids"] = ["unknown"]
    elif mutation == "duration":
        project["scenes"][0]["duration"] = 16
    elif mutation == "count":
        scene = project["scenes"][0]
        project["scenes"] = [{**scene, "id": f"scene{i}", "duration": 15} for i in range(9)]
    elif mutation == "path":
        project["id"] = "../escape"
    else:
        project["approved"] = True
    response = client.put(f"/api/projects/{project_id}", json=project)
    assert response.status_code in {404, 422}


def test_upload_ownership_raster_validation_and_audio(client):
    project = create(client)
    image = upload(client, project, filename="../../secret.png").json()
    assert image["name"] == "secret.png"
    assert set(image) == {"id", "name", "kind", "url"}
    assert client.get(image["url"]).headers["content-type"] == "image/png"
    assert upload(client, project, b"<svg>bad</svg>").status_code == 422
    assert upload(client, project, b"ID3not-an-mp3", "audio").status_code == 422
    other = create(client)
    other["characters"][0]["asset_id"] = image["id"]
    assert client.put(f"/api/projects/{other['id']}", json=other).status_code == 422
    assert client.get(f"/api/projects/{other['id']}/assets/{image['id']}").status_code == 422
    audio = upload(client, project, wav(), "audio", "song.wav").json()
    project["audio"].update(mode="uploaded", asset_id=audio["id"])
    assert client.put(f"/api/projects/{project['id']}", json=project).status_code == 200
    response = client.post(f"/api/projects/{project['id']}/assets",
                           files={"file": ("a.png", png())}, data={"kind": "image"})
    assert response.status_code == 422


def test_upload_bounds(client):
    project = create(client)
    assert upload(client, project, b"x" * (8 * 1024 * 1024 + 1)).status_code == 413
    response = client.post(f"/api/projects/{project['id']}/assets", content=b"tiny",
                           headers={"content-length": str(27 * 1024 * 1024)})
    assert response.status_code == 413


def test_auth_origin_cookie_logout_and_host(tmp_path):
    token = "test-owner-token-1234567890123456"
    with TestClient(create_app(tmp_path, token=token)) as client:
        assert client.get("/api/projects").status_code == 401
        assert client.post("/api/session", json={"token": "wrong"}).status_code == 401
        response = client.post("/api/session", json={"token": token})
        assert response.status_code == 200
        assert "HttpOnly" in response.headers["set-cookie"]
        assert "SameSite=strict" in response.headers["set-cookie"]
        assert client.get("/api/projects").status_code == 200
        assert client.post("/api/projects", json={}).status_code == 403
        assert client.post("/api/projects", json={}, headers={"Origin": "http://evil.test"}).status_code == 403
        assert client.post("/api/projects", json={}, headers={"Origin": "http://testserver"}).status_code == 200
        assert client.get("/api/projects", headers={"Host": "evil.test"}).status_code == 403
        assert client.delete("/api/session", headers={"Origin": "http://testserver"}).status_code == 200
        assert client.get("/api/projects").status_code == 401
        assert client.get("/api/projects", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    with pytest.raises(ValueError, match="Non-loopback"):
        create_app(tmp_path / "remote", host="0.0.0.0", token="short")
    with TestClient(create_app(tmp_path / "remote", host="0.0.0.0", token=token,
                               allowed_hosts=["studio.example"])) as client:
        assert client.get("/api/health", headers={"Host": "studio.example"}).status_code == 200
        assert client.get("/api/health", headers={"Host": "unknown.example"}).status_code == 403


def test_unauthenticated_foreign_origin_and_rebinding(client):
    assert client.post("/api/projects", json={}, headers={"Origin": "http://foreign.test"}).status_code == 403
    assert client.get("/api/health", headers={"Host": "foreign.test"}).status_code == 403
    for hostname in ("localhost", "127.0.0.1", "[::1]", "testserver"):
        assert client.get("/api/health", headers={"Host": hostname}).status_code == 200


def test_render_ranges_package_and_revision_readiness(client):
    project = create(client)
    audio = upload(client, project, wav(), "audio", "song.wav").json()
    project["audio"].update(mode="uploaded", asset_id=audio["id"])
    project["review"] = dict.fromkeys(project["review"], True)
    project = client.put(f"/api/projects/{project['id']}", json=project).json()
    job = wait_job(client, submit(client, project))
    assert job["status"] == "succeeded", job
    assert job["review_ready"] and not job["guide_audio"]
    response = client.get(job["video_url"], headers={"Range": "bytes=5-9"})
    assert response.status_code == 206 and response.content == bytes(range(5, 10))
    assert response.headers["content-range"] == "bytes 5-9/256"
    assert client.get(job["video_url"], headers={"Range": "bytes=-3"}).content == bytes(range(253, 256))
    assert client.get(job["video_url"], headers={"Range": "bytes=999-"}).status_code == 416
    assert client.get(job["video_url"], headers={"Range": "bytes=0-1,3-4"}).status_code == 416
    assert "attachment" in client.get(job["download_url"]).headers["content-disposition"]
    bundle = client.get(job["package_url"])
    with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
        assert set(archive.namelist()) == {"video.mp4", "poster.png", "captions.srt", "metadata.json",
                                           "description.txt", "checklist.json", "project.json"}
        metadata = json.loads(archive.read("metadata.json"))
        assert metadata["status"] == "review-ready"
        assert metadata["approved"] is False and metadata["published"] is False
        assert metadata["privacy"] == "private" and metadata["target_platform"] == "youtube"
        assert metadata["sha256"]["video.mp4"] == hashlib.sha256(archive.read("video.mp4")).hexdigest()
        assert json.loads(archive.read("project.json"))["revision"] == project["revision"]
    project["topic"] = "Changed after rendering"
    client.put(f"/api/projects/{project['id']}", json=project)
    assert not client.get(f"/api/jobs/{job['id']}").json()["review_ready"]
    with zipfile.ZipFile(io.BytesIO(client.get(job["package_url"]).content)) as archive:
        assert json.loads(archive.read("metadata.json"))["status"] == "draft"
    assert str(client.app.state.store.root) not in client.get(f"/api/jobs/{job['id']}").text


def test_character_job_real_worker_and_preview(client):
    project = create(client)
    base = f"/api/projects/{project['id']}"
    character = project["characters"][0]
    preview = client.get(base + f"/characters/{character['id']}/preview")
    assert Image.open(io.BytesIO(preview.content)).size == (512, 512)
    job = wait_job(client, submit(client, project, kind="character", character_id=character["id"]))
    assert job["status"] == "succeeded", job
    saved = client.get(base).json()
    assert saved["revision"] == project["revision"] + 1
    assert saved["characters"][0]["asset_id"]
    assert client.get(job["character_url"]).content.startswith(b"\x89PNG")
    assert client.get(f"/api/jobs/{job['id']}/package").status_code == 409
    assert client.get(base + f"/characters/{character['id']}/preview").status_code == 200


def test_single_queue_cancellation_delete_guard_and_trusted_request(tmp_path):
    entered, release = threading.Event(), threading.Event()
    active, maximum = 0, 0

    def blocking(command, **kwargs):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        entered.set()
        kwargs["cancel_event"].wait(5)
        active -= 1
        release.set()
        return {"returncode": -15, "cancelled": True}

    with TestClient(create_app(tmp_path, supervisor=blocking)) as client:
        one, two = create(client), create(client)
        running = submit(client, one)
        assert entered.wait(2)
        queued = submit(client, two)
        assert queued["status"] == "queued"
        assert client.delete(f"/api/projects/{one['id']}").status_code == 409
        assert client.post(f"/api/projects/{one['id']}/jobs", json={"kind": "render"}).status_code == 409
        injected = client.post(f"/api/projects/{two['id']}/jobs",
                               json={"kind": "render", "asset_paths": {"x": "/etc/passwd"}})
        assert injected.status_code == 422
        assert client.post(f"/api/jobs/{queued['id']}/cancel").json()["status"] == "cancelled"
        client.post(f"/api/jobs/{running['id']}/cancel")
        assert release.wait(2)
        assert wait_job(client, running)["status"] == "cancelled"
        assert maximum == 1
        assert client.delete(f"/api/projects/{one['id']}").status_code == 200


def test_restart_fails_unfinished_jobs_and_fatal_poison(tmp_path):
    app = create_app(tmp_path, supervisor=render_supervisor)
    from studio.web.models import CreateProject, JobRequest, Project, storyboard
    characters, scenes = storyboard(CreateProject().template)
    project = Project(characters=characters, scenes=scenes).model_dump()
    app.state.store.put("project", project)
    queued = app.state.jobs.submit(project, JobRequest(kind="render"))
    app.state.store.db.close()
    with TestClient(create_app(tmp_path, supervisor=render_supervisor)) as client:
        value = client.get(f"/api/jobs/{queued['id']}").json()
        assert value["status"] == "failed" and "restart" in value["error"]

    def fatal(*args, **kwargs):
        raise FatalCleanupError("private path and secret diagnostics")

    with TestClient(create_app(tmp_path / "poisoned", supervisor=fatal)) as client:
        project = create(client)
        job = wait_job(client, submit(client, project))
        assert job["status"] == "failed"
        assert "private path" not in json.dumps(job)
        assert client.app.state.jobs.stopping.is_set()
        assert client.post(f"/api/projects/{project['id']}/jobs", json={"kind": "render"}).status_code == 429


def test_system_capabilities_no_model_imports(client):
    response = client.get("/api/system")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["pressure"] in {"green", "yellow", "red", "unknown"}
    assert data["active_job"] is None
    assert data["providers"][0]["id"] == "illustration"
    assert data["providers"][0]["available"] is True
    assert str(client.app.state.store.root) not in response.text


def test_frontend_static_assets_are_real_javascript_and_css(client):
    index = client.get("/")
    assert index.status_code == 200
    assert "/static/app.js" in index.text and "/static/styles.css" in index.text
    javascript = client.get("/static/app.js")
    assert javascript.status_code == 200
    assert "javascript" in javascript.headers["content-type"]
    assert "<!doctype html" not in javascript.text.lower()
    assert "async function api(" in javascript.text
    stylesheet = client.get("/static/styles.css")
    assert stylesheet.status_code == 200
    assert "text/css" in stylesheet.headers["content-type"]
    assert "<!doctype html" not in stylesheet.text.lower()
