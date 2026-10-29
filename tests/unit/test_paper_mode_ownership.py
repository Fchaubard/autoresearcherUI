from __future__ import annotations


def test_paper_entry_clears_research_agent_expectation(arui_env, monkeypatch):
    """The dead-agent watchdog must not resurrect research in paper mode."""
    from backend.app import paper, realrun

    calls = []
    monkeypatch.setattr(realrun, "set_expected",
                        lambda value, reason="": calls.append((value, reason)))
    monkeypatch.setattr(paper, "project_mode", lambda: "research")
    monkeypatch.setattr(paper, "set_project_mode", lambda mode: None)
    monkeypatch.setattr(paper, "populate_claims_from_proposal", lambda pid: 0)
    monkeypatch.setattr(paper.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(paper, "_set_onboarding_key", lambda *a, **k: None)
    monkeypatch.setenv("ARUI_DISABLE_BG", "1")
    from backend.app import author_agent, paper_runner
    monkeypatch.setattr(author_agent, "start", lambda **k: {"status": "started"})
    monkeypatch.setattr(paper_runner, "start", lambda: None)
    monkeypatch.setattr(paper.threading, "Thread",
                        lambda *a, **k: type("T", (), {"start": lambda self: None})())

    paper.enter_paper_mode()

    assert calls == [(False, "paper mode owns the autonomous loop")]


def test_evidence_gap_stays_in_paper_mode(arui_env, monkeypatch):
    from fastapi.testclient import TestClient
    from backend.app import api, paper
    from backend.main import app

    paper.set_project_mode("paper")
    calls = []

    class ImmediateThread:
        def __init__(self, target, args=(), **kwargs):
            self.target, self.args = target, args
        def start(self):
            calls.append(self.args[0])

    monkeypatch.setattr(api.threading, "Thread", ImmediateThread)
    client = TestClient(app)
    response = client.post("/api/paper/phase", json={
        "phase": "paper.develop_evidence", "actor": "author",
        "detail": {"blocker": "More validation is required.",
                   "blocker_type": "evidence_gap"}})

    assert response.status_code == 200
    assert response.json()["developing_evidence"] is True
    assert calls == []


def test_explicit_fundamental_blocker_returns_to_research(arui_env,
                                                           monkeypatch):
    from fastapi.testclient import TestClient
    from backend.app import api, paper
    from backend.main import app
    paper.set_project_mode("paper")
    calls = []

    class ImmediateThread:
        def __init__(self, target, args=(), **kwargs):
            self.args = args
        def start(self):
            calls.append(self.args[0])

    monkeypatch.setattr(api.threading, "Thread", ImmediateThread)
    response = TestClient(app).post("/api/paper/phase", json={
        "phase": "paper.whittle_claims", "actor": "author",
        "detail": {"blocker": "Objective cannot be investigated here.",
                   "blocker_type": "fundamental",
                   "return_to_research": True}})
    assert response.json()["returning_to_research"] is True
    assert calls == ["Objective cannot be investigated here."]


def test_revert_repairs_stale_author_after_mode_already_changed(
        arui_env, monkeypatch, fake_subprocess):
    from backend.app import api, author_agent, lifecycle, paper
    stopped = []
    phases = []
    monkeypatch.setattr(paper, "project_mode", lambda: "research")
    monkeypatch.setattr(author_agent, "stop", lambda: stopped.append(True))
    monkeypatch.setattr(lifecycle, "set_phase",
                        lambda phase, reason="": phases.append((phase, reason)))
    from backend.app import tmux_safe
    monkeypatch.setattr(tmux_safe, "pane_alive", lambda name: name == "agent")

    out = api._revert_paper_to_research("already committed")

    assert out["status"] == "already_in_research"
    assert stopped == [True]
    assert phases == [(lifecycle.PHASE_RUNNING,
                       "autonomous research owns the loop")]


def test_revert_resumes_real_agent_in_original_workspace(
        arui_env, fake_subprocess, make_project, setting_setter, monkeypatch, tmp_path):
    from backend.app import api, paper, realrun, orchestrator
    from backend.app.db import SessionLocal
    from backend.app.models import Project
    workspace = tmp_path / "original-workspace"
    workspace.mkdir()
    (workspace / "checkpoint.txt").write_text("keep this checkpoint")
    make_project(name="renamed-display-name", repo_path=str(workspace))
    setting_setter("onboarding", {"repo_name": "renamed-display-name", "purpose": "continue work"})
    paper.set_project_mode("paper")
    calls = []
    def start(cfg, *, resume=False):
        with SessionLocal() as db:
            assert db.query(Project).first().repo_path == str(workspace)
        calls.append((cfg, resume))
        return object()
    monkeypatch.setattr(realrun, "start_real", start)
    monkeypatch.setattr(orchestrator, "start", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("The example FakeAgent orchestrator must not replace real research")))
    result = api._revert_paper_to_research("need more experiments")
    assert result["ok"] and result["status"] == "reverted"
    assert result["research_restarted"] is True
    assert calls[0][1] is True
    assert paper.project_mode() == "research"
    assert (workspace / "checkpoint.txt").read_text() == "keep this checkpoint"


def test_revert_reports_restart_failure_then_retry_repairs_it(
        arui_env, fake_subprocess, make_project, setting_setter, monkeypatch, tmp_path):
    from backend.app import api, paper, realrun
    from backend.app.db import SessionLocal
    from backend.app.models import ModeHistory
    workspace = tmp_path / "existing"
    workspace.mkdir()
    make_project(name="project", repo_path=str(workspace))
    setting_setter("onboarding", {"repo_name": "project"})
    paper.set_project_mode("paper")
    monkeypatch.setattr(realrun, "start_real", lambda *a, **kw: None)
    result = api._revert_paper_to_research("more research needed")
    assert result["status"] == "partial" and result["ok"] is False
    assert result["mode"] == "research" and not result["research_restarted"]
    assert "could not restart" in result["detail"]
    monkeypatch.setattr(realrun, "start_real", lambda *a, **kw: object())
    retry = api._revert_paper_to_research("retry agent launch")
    assert retry["ok"] and retry["research_restarted"]
    assert retry["status"] == "already_in_research"
    with SessionLocal() as db:
        assert db.query(ModeHistory).count() == 1


def test_revert_missing_workspace_preserves_paper_mode(
        arui_env, fake_subprocess, make_project, monkeypatch, tmp_path):
    from backend.app import api, paper, author_agent
    make_project(name="renamed", repo_path=str(tmp_path / "missing-workspace"))
    paper.set_project_mode("paper")
    stopped = []
    monkeypatch.setattr(author_agent, "stop", lambda: stopped.append(True))
    result = api._revert_paper_to_research("more research needed")
    assert result["status"] == "error"
    assert result["mode"] == "paper"
    assert paper.project_mode() == "paper"
    assert stopped == []


def test_revert_while_halted_does_not_launch_research(
        arui_env, fake_subprocess, make_project, monkeypatch):
    from backend.app import api, paper, realrun, notify
    make_project()
    paper.set_project_mode("paper")
    notify.set_research_halted(True, "operator halt")
    calls = []
    monkeypatch.setattr(realrun, "start_real", lambda *a, **kw: calls.append(True))
    result = api._revert_paper_to_research("return later after halt")
    assert result["ok"] and result["paused"]
    assert not result["research_restarted"] and calls == []
    assert notify.research_halted()[0]


def test_revert_preserves_actual_status_of_independent_paper_run(
        arui_env, fake_subprocess, make_project, make_run, monkeypatch):
    from backend.app import api, paper, tmux_safe
    from backend.app.db import SessionLocal
    from backend.app.models import Run
    make_project()
    run = make_run(context="paper", status="running")
    rid = run.id
    paper.set_project_mode("paper")
    monkeypatch.setattr(tmux_safe, "pane_alive", lambda role: role == "agent")
    result = api._revert_paper_to_research("use existing evidence")
    assert result["ok"] and result["paper_runs_continuing"] == 1
    with SessionLocal() as db:
        assert db.get(Run, rid).status == "running"


def test_revert_failure_to_stop_author_keeps_paper_mode(
        arui_env, fake_subprocess, make_project, monkeypatch):
    from backend.app import api, paper, tmux_safe
    make_project()
    paper.set_project_mode("paper")
    monkeypatch.setattr(tmux_safe, "pane_alive", lambda role: True)
    result = api._revert_paper_to_research("need research again")
    assert not result["ok"]
    assert result["status"] == "error"
    assert paper.project_mode() == "paper"
