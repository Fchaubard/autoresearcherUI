"""GET /api/agent/alive: cheap liveness probe (no pane bytes) that replaced the
huge-offset /agent/raw status poll which starved the terminal stream."""
import pytest


@pytest.fixture
def client(arui_env, fake_subprocess):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.app.api import router
    app = FastAPI(); app.include_router(router)
    with TestClient(app) as c:
        yield c


def test_agent_alive_shape(client, monkeypatch):
    from backend.app import tmux_safe, pane_stream
    monkeypatch.setattr(tmux_safe, "is_alive", lambda n: True)
    monkeypatch.setattr(pane_stream, "size", lambda n: 12345)
    r = client.get("/api/agent/alive?session=agent")
    assert r.status_code == 200
    b = r.json()
    assert b == {"alive": True, "size": 12345}
    assert "chunk" not in b            # NO pane bytes returned


def test_agent_alive_rejects_bad_name(client):
    b = client.get("/api/agent/alive?session=bad%20name").json()
    assert b["alive"] is False and b["size"] == 0


def test_agent_alive_dead_session(client, monkeypatch):
    from backend.app import tmux_safe, pane_stream
    monkeypatch.setattr(tmux_safe, "is_alive", lambda n: False)
    monkeypatch.setattr(pane_stream, "size", lambda n: 0)
    assert client.get("/api/agent/alive?session=agent").json()["alive"] is False
