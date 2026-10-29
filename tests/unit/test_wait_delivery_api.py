"""A successful tmux key write must not erase an unconsumed event wait."""
import pytest


@pytest.fixture
def waiting_client(arui_env, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.app import agent_wait, api
    from backend.app.db import SessionLocal
    from backend.app.models import Project, Run
    with SessionLocal() as db:
        db.add(Project(id="p", name="research", purpose="Test waiting delivery"))
        db.add(Run(id="r1", project_id="p", run_name="baseline", status="running"))
        db.commit()
    agent_wait.arm("Compare r1 when it completes.")
    monkeypatch.setattr(api, "_tmux_alive", lambda _: True)
    monkeypatch.setattr("time.sleep", lambda _: None)
    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize("success_after", [None, 1, 2])
def test_operator_message_requires_consumed_input(waiting_client, monkeypatch, nudge_terminal, success_after):
    from backend.app import agent_wait, supervisor
    from backend.app.db import SessionLocal
    from backend.app.models import ChatMessage
    terminal = nudge_terminal(success_after=success_after)
    monkeypatch.setattr(supervisor._sp, "run", terminal)
    result = waiting_client.post("/api/agent/send", json={"text": "Inspect the baseline"}).json()
    assert result["ok"] is (success_after is not None)
    with SessionLocal() as db:
        assert db.query(ChatMessage).count() == (1 if success_after is not None else 0)
    if success_after is None:
        assert agent_wait.status()["status"] == "waiting"
        assert terminal.enters == 2 and terminal.clears == 1
    else:
        assert agent_wait.status() == {}
        assert terminal.enters == success_after


@pytest.mark.parametrize("success_after", [None, 1])
def test_terminal_enter_keeps_unconsumed_operator_draft(waiting_client, monkeypatch, nudge_terminal, success_after):
    from backend.app import agent_wait, supervisor
    terminal = nudge_terminal(success_after=success_after)
    terminal.draft = "My instruction\nPreserve this second line"
    monkeypatch.setattr(supervisor._sp, "run", terminal)
    result = waiting_client.post("/api/agent/keys", json={"data": "\r", "session": "agent"}).json()
    assert result["ok"] is (success_after is not None)
    assert terminal.enters == 1 and terminal.clears == 0
    if success_after is None:
        assert agent_wait.status()["status"] == "waiting"
        assert terminal.draft == "My instruction\nPreserve this second line"
    else:
        assert agent_wait.status() == {}


@pytest.mark.parametrize("placeholder", ["", "Ask Codex to do anything", 'Try "run tests"'])
def test_empty_terminal_enter_does_not_cancel_wait(waiting_client, monkeypatch, nudge_terminal, placeholder):
    from backend.app import agent_wait, supervisor
    terminal = nudge_terminal(success_after=None)
    terminal.placeholder = placeholder
    monkeypatch.setattr(supervisor._sp, "run", terminal)
    result = waiting_client.post("/api/agent/keys", json={"data": "\r", "session": "agent"}).json()
    assert result["ok"] is True
    assert agent_wait.status()["status"] == "waiting"
    assert terminal.enters == 1 and terminal.clears == 0
