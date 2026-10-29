"""Settings must commit atomically and change agent sessions without stopping runs."""
import os

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


def saved(key="onboarding"):
    from backend.app.db import SessionLocal
    from backend.app.models import Setting
    with SessionLocal() as db:
        row = db.query(Setting).filter_by(key=key).first()
        return row.value if row else None


def watch_restarts(monkeypatch, *, alive=True, success=True):
    from backend.app import tmux_safe, agent_watcher
    calls = []
    monkeypatch.setattr(tmux_safe, "pane_alive", lambda role: alive)
    def restart(role, *, resume=False):
        calls.append((role, resume))
        return success
    monkeypatch.setattr(agent_watcher, "_restart_session", restart)
    return calls


def test_running_model_switch_restarts_only_changed_role_and_keeps_run(
        client, setting_setter, make_project, make_run, monkeypatch):
    from backend.app.db import SessionLocal
    from backend.app.models import Run
    setting_setter("onboarding", {"research_agent_model": "gpt-5.6-sol", "author_agent_model": "claude-opus-5"})
    make_project()
    run = make_run(status="running")
    run_id = run.id
    monkeypatch.setenv("ARUI_CLAUDE_BIN", "test-only")
    calls = watch_restarts(monkeypatch)
    response = client.put("/api/settings", json={"research_agent_model": "gemini-4-argon"})
    assert response.status_code == 200
    assert response.json()["applied_agents"] == ["agent"]
    assert calls == [("agent", True)]
    assert saved()["research_agent_model"] == "gemini-4-argon"
    with SessionLocal() as db:
        assert db.get(Run, run_id).status == "running"


def test_paused_changes_wait_until_resume(client, setting_setter, monkeypatch):
    from backend.app import runtime_settings, notify
    setting_setter("onboarding", {"research_paused": True, "purpose": "old"})
    calls = watch_restarts(monkeypatch)
    response = client.put("/api/settings", json={"purpose": "new"})
    assert response.json()["pending_agents"] == ["agent"]
    assert calls == []
    monkeypatch.setattr(notify, "research_paused", lambda: False)
    assert runtime_settings.apply_pending()["applied"] == ["agent"]
    assert calls == [("agent", True)]
    assert saved("settings.pending_agents") == {"roles": []}


def test_retry_same_saved_settings_applies_pending_restart(client, setting_setter, monkeypatch):
    setting_setter("onboarding", {"purpose": "old"})
    calls = watch_restarts(monkeypatch, success=False)
    first = client.put("/api/settings", json={"purpose": "new"}).json()
    assert first["status"] == "partial" and first["saved"]
    assert first["pending_agents"] == ["agent"]
    calls = watch_restarts(monkeypatch)
    second = client.put("/api/settings", json={"purpose": "new"}).json()
    assert second["status"] == "ok" and second["applied_agents"] == ["agent"]
    assert calls == [("agent", True)]


def test_pending_intent_survives_apply_exception(client, setting_setter, monkeypatch):
    from backend.app import runtime_settings
    setting_setter("onboarding", {"purpose": "old"})
    def fail():
        raise OSError("storage unavailable")
    monkeypatch.setattr(runtime_settings, "apply_pending", fail)
    response = client.put("/api/settings", json={"purpose": "new"}).json()
    assert response["status"] == "partial"
    assert saved("settings.pending_agents")["roles"] == ["agent"]


def test_settings_save_waits_for_pending_apply_and_preserves_new_intent(
        arui_env, setting_setter, fake_subprocess, monkeypatch):
    """A second save cannot be erased by a previous background apply."""
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from backend.app import api, runtime_settings, tmux_safe, research_processes, agent_watcher
    setting_setter('onboarding', {'purpose': 'first'})
    setting_setter('settings.pending_agents', {'roles': ['agent']})
    monkeypatch.setattr(tmux_safe, 'pane_alive', lambda role: True)
    monkeypatch.setattr(research_processes, 'active_session_workloads', lambda role: [])
    applying, release, transaction_entered = threading.Event(), threading.Event(), threading.Event()
    calls = []
    def restart(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            applying.set()
            assert release.wait(5)
            return True
        return False  # The newer request must remain pending after this attempt.
    monkeypatch.setattr(agent_watcher, '_restart_session', restart)
    original = api._save_live_settings_locked
    def transaction(payload):
        transaction_entered.set()
        return original(payload)
    monkeypatch.setattr(api, '_save_live_settings_locked', transaction)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(runtime_settings.apply_pending)
        assert applying.wait(5)
        second = pool.submit(api._save_live_settings, {'purpose': 'second'})
        try:
            assert not transaction_entered.wait(.15), 'save entered while old apply still owns the lock'
            assert saved()['purpose'] == 'first'
        finally:
            release.set()
        assert first.result(timeout=5)['applied'] == ['agent']
        result, _ = second.result(timeout=5)
    assert result['saved'] and result['pending_agents'] == ['agent']
    assert saved()['purpose'] == 'second'
    assert saved('settings.pending_agents')['roles'] == ['agent']


def test_refreshes_runtime_key_and_masks_new_provider_secrets(client, setting_setter, monkeypatch):
    from backend.app import api
    monkeypatch.setenv("OPENAI_API_KEY", "old-runtime-key")
    setting_setter("onboarding", {"openai_token": "old-runtime-key"})
    watch_restarts(monkeypatch, alive=False)
    response = client.put("/api/settings", json={
        "openai_token": "new-runtime-key", "opencode_token": "zen-secret",
        "flint_token": "local-secret", "flint_base_url": "http://127.0.0.1:8080/v1"})
    assert response.status_code == 200
    assert os.environ["OPENAI_API_KEY"] == "new-runtime-key"
    assert os.environ["ARUI_FLINT_BASE_URL"] == "http://127.0.0.1:8080/v1"
    masked = client.get("/api/settings").json()
    assert masked["opencode_token"] == masked["flint_token"] == "••••••••"
    assert "new-runtime-key" not in response.text
    for name in list(api._RUNTIME_TOKEN_ENV):
        monkeypatch.delenv(name, raising=False)


def test_watchdog_and_form_save_atomically(client, setting_setter, monkeypatch):
    setting_setter("onboarding", {"email": "old@example.invalid"})
    response = client.put("/api/settings", json={"settings": {"email": "new@example.invalid"},
        "watchdog": {"no_metric_flow": {"enabled": "not-a-boolean"}}})
    assert response.status_code == 400
    assert saved()["email"] == "old@example.invalid"
    assert saved("watchdog.config") is None
    response = client.put("/api/settings", json={"settings": {"email": "new@example.invalid"},
        "watchdog": {"no_metric_flow": {"enabled": False}}})
    assert response.status_code == 200
    assert saved()["email"] == "new@example.invalid"
    assert saved("watchdog.config")["no_metric_flow"]["enabled"] is False


def test_project_rename_preserves_workspace_and_metadata(
        client, setting_setter, make_project, monkeypatch):
    from backend.app.config import WORKSPACE_DIR
    from backend.app.db import SessionLocal
    from backend.app.models import Project
    old = WORKSPACE_DIR / "original"
    old.mkdir(parents=True)
    (old / "checkpoint.txt").write_text("preserve")
    make_project(name="original", repo_path="")
    setting_setter("onboarding", {"repo_name": "original", "metric": "loss"})
    watch_restarts(monkeypatch, alive=False)
    response = client.put("/api/settings", json={"repo_name": "renamed", "metric": "accuracy", "purpose": "new purpose"})
    assert response.status_code == 200, response.text
    with SessionLocal() as db:
        project = db.query(Project).first()
        assert project.name == "renamed"
        assert project.repo_path == str(old.resolve())
        assert project.metric_direction == "maximize"
        assert project.purpose == "new purpose"
    assert (WORKSPACE_DIR / "renamed" / "checkpoint.txt").read_text() == "preserve"


def test_rename_rejects_existing_other_workspace(client, setting_setter, make_project):
    from backend.app.config import WORKSPACE_DIR
    (WORKSPACE_DIR / "taken").mkdir(parents=True)
    make_project(name="original")
    setting_setter("onboarding", {"repo_name": "original"})
    response = client.put("/api/settings", json={"repo_name": "taken"})
    assert response.status_code == 400
    assert saved()["repo_name"] == "original"


def test_database_failure_is_readable_json(client, monkeypatch):
    from backend.app import api
    def fail(_):
        raise OSError("Internal Server Error private database detail")
    monkeypatch.setattr(api, "_save_live_settings", fail)
    response = client.put("/api/settings", json={"purpose": "new"})
    assert response.status_code == 503
    assert "could not be saved" in response.json()["detail"]
    assert "private database detail" not in response.text


def test_passcode_change_refreshes_browser_cookie(client):
    response = client.put("/api/settings", json={"passcode": "new-passcode"})
    assert response.status_code == 200
    assert "new-passcode" in response.headers["set-cookie"]
    assert "new-passcode" not in response.text


@pytest.mark.parametrize("payload", [[], {"settings": []}, {"research_agent_model": "unknown-provider"}])
def test_invalid_payload_is_not_saved(client, payload):
    response = client.put("/api/settings", json=payload)
    assert response.status_code == 400
    assert saved() is None


def test_citation_notes_roundtrip_and_missing_validation(client):
    from backend.app.db import SessionLocal
    from backend.app.models import PaperCitation
    with SessionLocal() as db:
        db.add(PaperCitation(key="prior2026", title="Keep this title"))
        db.commit()
    notes = {"same_md": "Both use replay", "diff_md": "We adapt replay frequency"}
    assert client.put("/api/paper/citations/prior2026/update", json=notes).json()["ok"]
    rows = client.get("/api/paper/citations").json()["citations"]
    assert all(rows[0][key] == value for key, value in notes.items())
    assert rows[0]["title"] == "Keep this title"
    assert client.put("/api/paper/citations/missing/update", json=notes).status_code == 404
    assert client.put("/api/paper/citations/prior2026/update", json={"same_md": []}).status_code == 400
    assert client.put("/api/paper/citations/prior2026/update", json={"title": "overwrite"}).status_code == 400


def test_citation_notes_migrate_existing_database(arui_env):
    from backend.app import db
    with db.engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE paper_citation")
        conn.exec_driver_sql("CREATE TABLE paper_citation (key TEXT PRIMARY KEY, title TEXT)")
        conn.exec_driver_sql("INSERT INTO paper_citation VALUES ('prior', 'Existing title')")
    db._migrate_citation_notes()
    db._migrate_citation_notes()
    with db.engine.connect() as conn:
        row = conn.exec_driver_sql("SELECT title,same_md,diff_md FROM paper_citation").one()
        assert tuple(row) == ("Existing title", "", "")


def test_explicit_restart_cannot_bypass_pause(client, setting_setter, monkeypatch):
    from backend.app import realrun
    setting_setter("onboarding", {"research_paused": True})
    monkeypatch.setattr(realrun, "start_real", lambda *a, **kw: pytest.fail("must not launch while paused"))
    assert client.post("/api/agent/restart", json={"name": "research"}).status_code == 423


def test_explicit_restart_preserves_inline_training(client, monkeypatch):
    from backend.app import research_processes, realrun
    monkeypatch.setattr(research_processes, "active_session_workloads", lambda session: [123])
    monkeypatch.setattr(realrun, "start_real", lambda *a, **kw: pytest.fail("must not interrupt training"))
    response = client.post("/api/agent/restart", json={"name": "research"}).json()
    assert response["ok"] is False
    assert "experiments running" in response["error"]


def test_restart_reports_failed_research_launch(client, setting_setter, monkeypatch):
    from backend.app import realrun
    setting_setter("onboarding", {"research_agent_model": "gpt-5.6-sol", "openai_token": "test-key"})
    monkeypatch.setattr(realrun, "start_real", lambda *a, **kw: None)
    response = client.post("/api/agent/restart", json={"name": "research"}).json()
    assert response["ok"] is False
    assert response["results"]["research"]["ok"] is False


def test_restart_reports_failed_author_launch(client, monkeypatch):
    from backend.app import author_agent
    monkeypatch.setattr(author_agent, "start", lambda: {"status": "error", "error": "CLI missing"})
    response = client.post("/api/agent/restart", json={"name": "author"}).json()
    assert response["ok"] is False
    assert response["results"]["author"]["ok"] is False


def test_author_compat_restart_cannot_bypass_pause(client, setting_setter, monkeypatch):
    from backend.app import author_agent
    setting_setter("onboarding", {"research_paused": True})
    monkeypatch.setattr(author_agent, "start", lambda *a, **kw: pytest.fail("must not launch"))
    result = client.post("/api/paper/author/restart", json={}).json()
    assert not result["ok"] and "paused" in result["detail"]


def test_author_compat_restart_preserves_training(client, monkeypatch):
    from backend.app import author_agent, research_processes
    monkeypatch.setattr(research_processes, "active_session_workloads", lambda session: [123])
    monkeypatch.setattr(author_agent, "stop", lambda: pytest.fail("must not stop active experiment"))
    result = client.post("/api/paper/author/restart", json={}).json()
    assert not result["ok"] and "experiments" in result["detail"]


def test_missing_cli_reports_configuration_error_without_saving(client, setting_setter, monkeypatch):
    from backend.app import agent_cli
    setting_setter("onboarding", {"research_agent_model": "gpt-5.6-sol", "gemini_token": "test-key"})
    monkeypatch.delenv("ARUI_CLAUDE_BIN", raising=False)
    def missing(*args, **kwargs):
        raise agent_cli.AgentCLIUnavailable("gemini CLI is required for this autonomous agent")
    monkeypatch.setattr(agent_cli, "command", missing)
    response = client.put("/api/settings", json={"research_agent_model": "gemini-4-argon"})
    assert response.status_code == 400
    assert "gemini CLI is required" in response.json()["detail"]
    assert saved()["research_agent_model"] == "gpt-5.6-sol"
