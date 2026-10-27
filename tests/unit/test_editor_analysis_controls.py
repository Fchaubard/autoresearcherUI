"""Persistence regressions for Files and Analysis buttons, isolated on disk."""
import pytest


@pytest.fixture
def client(arui_env, fake_subprocess):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.app.api import router

    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        yield client


def test_new_file_does_not_truncate_existing_content(client, arui_env):
    path = arui_env / "important.py"
    path.write_text("valuable = 42\n")
    response = client.put("/api/files/write", json={
        "path": str(path), "content": "", "create_only": True})
    assert response.status_code == 409
    assert response.json()["ok"] is False
    assert path.read_text() == "valuable = 42\n"


def test_new_file_then_save_existing_file(client, arui_env):
    path = arui_env / "new.py"
    response = client.put("/api/files/write", json={
        "path": str(path), "content": "", "create_only": True})
    assert response.json()["ok"] is True
    assert path.read_text() == ""
    response = client.put("/api/files/write", json={
        "path": str(path), "content": "updated = True\n"})
    assert response.json()["ok"] is True
    assert path.read_text() == "updated = True\n"


def test_invalid_editor_content_does_not_truncate_existing_file(client, arui_env):
    path = arui_env / "important.py"
    path.write_text("keep this")
    response = client.put("/api/files/write", json={
        "path": str(path), "content": ["invalid"]})
    assert response.json()["ok"] is False
    assert path.read_text() == "keep this"


def test_deleting_every_analysis_panel_survives_reload(client):
    assert client.get("/api/analysis/panels").json()["panels"]
    response = client.put("/api/analysis/panels", json={"panels": []})
    assert response.status_code == 200
    assert client.get("/api/analysis/panels").json() == {"panels": []}


def test_analysis_reset_restores_defaults_after_empty_layout(client):
    defaults = client.get("/api/analysis/panels").json()
    client.put("/api/analysis/panels", json={"panels": []})
    response = client.put("/api/analysis/panels", json={"reset": True})
    assert response.status_code == 200
    assert client.get("/api/analysis/panels").json() == defaults


@pytest.mark.parametrize("payload", [{}, {"panels": {}}, {"panels": ["bad"]}])
def test_invalid_analysis_save_preserves_existing_layout(client, payload):
    saved = {"panels": [{"id": "custom", "y_keys": ["accuracy"]}]}
    assert client.put("/api/analysis/panels", json=saved).status_code == 200
    assert client.put("/api/analysis/panels", json=payload).status_code == 400
    assert client.get("/api/analysis/panels").json() == saved
