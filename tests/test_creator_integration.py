"""Creator routes share the existing studio's authentication boundary."""
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from studio.web.api import create_app


def test_creator_page_and_assets_share_origin(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        response = client.get("/create")
        assert response.status_code == 200
        assert "/static/create.js" in response.text
        assert "/static/create.css" in response.text
        assert "javascript" in client.get("/static/create.js").headers["content-type"]
        assert "text/css" in client.get("/static/create.css").headers["content-type"]


def test_creator_api_requires_existing_session_and_origin(tmp_path):
    token = "only-a-test-token-for-creator-session"
    with TestClient(create_app(tmp_path, token=token)) as client:
        assert client.get("/api/creator/projects").status_code == 401
        assert client.post("/api/session", json={"token": token}).status_code == 200
        assert client.get("/api/creator/projects").status_code == 200
        body = {"idea": "A tiny explorer discovers a glowing forest", "style": "anime"}
        assert client.post("/api/creator/projects", json=body).status_code == 403
        assert client.post("/api/creator/projects", json=body,
                           headers={"Origin": "https://foreign.test"}).status_code == 403
        assert client.post("/api/creator/projects", json=body,
                           headers={"Origin": "http://testserver"}).status_code == 200


def test_creator_and_nursery_records_are_separate(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        brief = {"idea": "A futuristic train travels beneath the ocean", "style": "cinematic"}
        created = client.post("/api/creator/projects", json=brief)
        assert created.status_code == 200
        assert client.get("/api/projects").json() == {"projects": []}
        assert len(client.get("/api/creator/projects").json()["projects"]) == 1
    with TestClient(create_app(tmp_path)) as client:
        assert client.get("/api/creator/projects").json()["projects"][0]["brief"]["idea"] == brief["idea"]
