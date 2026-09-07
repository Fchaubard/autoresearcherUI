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
        arui_env, monkeypatch):
    from backend.app import api, author_agent, lifecycle, paper
    stopped = []
    phases = []
    monkeypatch.setattr(paper, "project_mode", lambda: "research")
    monkeypatch.setattr(author_agent, "stop", lambda: stopped.append(True))
    monkeypatch.setattr(lifecycle, "set_phase",
                        lambda phase, reason="": phases.append((phase, reason)))

    out = api._revert_paper_to_research("already committed")

    assert out["status"] == "already_in_research"
    assert stopped == [True]
    assert phases == [(lifecycle.PHASE_RUNNING,
                       "autonomous research owns the loop")]
