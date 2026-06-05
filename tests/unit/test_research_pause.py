"""Unit tests for the Pause/Resume research feature (Task #1).

The pause flag is stored on the onboarding settings row as
`research_paused: bool` and is read by:

  - /api/research/{status,pause,resume} — REST surface that the Settings
    modal hits.
  - /api/track/run — rejects new runs with HTTP 423 while paused, in
    the same shape as the council bless gate.
  - orchestrator._execute — skips launching new runs while paused.
  - pi.cycle / pi.cycle_paper — skips nudging while paused.

These tests cover all three surfaces.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def client(arui_env, fake_subprocess):
    """A FastAPI TestClient bound to ONLY the api router (no static
    files, no startup events) so the tests stay fast and focused."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.app.api import router
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as c:
        yield c


# ─────────────────────────── endpoint round-trip ─────────────────────


def test_research_pause_toggle(client):
    """POST /api/research/pause flips the flag on; /api/research/status
    reports it; /api/research/resume flips it back. The onboarding row
    is the single source of truth so the orchestrator + PI + ingest see
    the same value."""
    # Default: not paused (no setting row exists yet).
    r = client.get("/api/research/status")
    assert r.status_code == 200
    assert r.json() == {"paused": False}

    # Pause → flag on, status reports it.
    r = client.post("/api/research/pause")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["paused"] is True
    assert client.get("/api/research/status").json() == {"paused": True}

    # And the helper agrees (this is what orchestrator + PI read).
    from backend.app import notify
    assert notify.research_paused() is True

    # Resume → flag off.
    r = client.post("/api/research/resume")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["paused"] is False
    assert client.get("/api/research/status").json() == {"paused": False}
    assert notify.research_paused() is False


def test_research_pause_persists_on_onboarding_row(client, db_session):
    """Pause should write `research_paused: True` ON the onboarding
    Setting row — same row that holds emails_paused, model choices,
    etc. This is the contract the rest of the system depends on."""
    from backend.app.models import Setting
    # Seed an existing onboarding row so we can confirm pause merges
    # (rather than clobbering) the existing keys.
    db_session.add(Setting(key="onboarding",
                            value={"email": "me@x.com",
                                   "emails_paused": False}))
    db_session.commit()

    client.post("/api/research/pause")

    db_session.expire_all()
    row = db_session.query(Setting).filter(Setting.key == "onboarding").first()
    assert row is not None
    assert row.value.get("research_paused") is True
    # Untouched keys survive.
    assert row.value.get("email") == "me@x.com"
    assert row.value.get("emails_paused") is False


# ─────────────────────────── /api/track/run gate ─────────────────────


def test_track_run_rejected_when_paused(client):
    """While research is paused, /api/track/run returns HTTP 423 Locked
    with reason=research_paused — same envelope shape as the council
    bless gate so the arui SDK and the agent can detect it uniformly."""
    client.post("/api/research/pause")

    r = client.post("/api/track/run", json={"name": "my-run", "config": {}})
    assert r.status_code == 423
    body = r.json()
    assert body["ok"] is False
    assert body["blocked"] is True
    assert body["reason"] == "research_paused"
    assert "Resume" in body["hint"] or "resume" in body["hint"]


def test_track_run_allows_smoke_probe_when_paused(client):
    """The _probe / _smoke whitelist exists so the agent can prove the
    code runs at all before bless — pause should NOT block those for the
    same reason (otherwise the agent can't diagnose anything while
    paused, which defeats the 'pause to debug' use case)."""
    client.post("/api/research/pause")

    r = client.post("/api/track/run",
                    json={"name": "_probe_001", "config": {}})
    assert r.status_code == 200
    assert "run_id" in r.json()


def test_track_run_allowed_after_resume(client):
    """After /api/research/resume the gate lifts immediately — the next
    POST /api/track/run goes through."""
    client.post("/api/research/pause")
    assert client.post("/api/track/run",
                       json={"name": "blocked", "config": {}}).status_code == 423

    client.post("/api/research/resume")
    r = client.post("/api/track/run", json={"name": "after", "config": {}})
    assert r.status_code == 200
    assert r.json()["run_id"] == "after"


# ─────────────────────────── orchestrator gate ───────────────────────


def test_orchestrator_skips_when_paused(arui_env, monkeypatch, tmp_path):
    """orchestrator._execute should early-return (sleep + recheck) while
    research is paused. We patch notify.research_paused to flip True
    once, then False, and confirm the launch path is never invoked."""
    import asyncio
    from backend.app import orchestrator, notify
    from backend.app.db import SessionLocal
    from backend.app.models import Idea, Project

    # Seed a project + a baseline idea so _execute has something to
    # look up. We use the baseline branch so the result-formatting path
    # doesn't need a prior baseline to compare against.
    db = SessionLocal()
    db.add(Project(id="proj-test", name="t", status="running",
                    validation_metric="val_mse"))
    db.add(Idea(id="idea-x", idea_id="baseline", project_id="proj-test",
                description="d", status="not_implemented", hpps={}))
    db.commit()
    db.close()

    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    orch = orchestrator.Orchestrator(str(proj_dir), n_slots=1)
    orch.running = True

    # Sentinel: track every call to the subprocess-launching path.
    launched = []

    async def fake_launch(self, run_id, gpu_index, cfg):
        launched.append(run_id)
        return 1.0, True, "val_mse: 1.0\n"

    monkeypatch.setattr(orchestrator.Orchestrator, "_launch", fake_launch)

    # Pause sequence: paused, paused, paused, unpaused. We need enough
    # True returns that the while-loop spins at least once, then the
    # False unblocks it and the (mocked) launch runs to completion.
    pause_calls = {"n": 0}

    def fake_paused():
        pause_calls["n"] += 1
        # Stay paused for the first 2 polls, then unblock and let it
        # proceed past the gate. We also stop the orchestrator after
        # the launch so the test terminates quickly.
        return pause_calls["n"] <= 2

    monkeypatch.setattr(notify, "research_paused", fake_paused)

    # Speed up the gate's poll loop so the test runs in <1s rather
    # than 5s+.
    real_sleep = asyncio.sleep

    async def fast_sleep(_secs):
        await real_sleep(0)

    monkeypatch.setattr(orchestrator.asyncio, "sleep", fast_sleep)

    async def run_it():
        await orch._execute("idea-x", 0)

    asyncio.run(run_it())

    # The gate polled research_paused multiple times BEFORE the launch
    # ever ran. That's the contract.
    assert pause_calls["n"] >= 3, pause_calls
    # And once unpaused, the launch did happen exactly once.
    assert launched == ["baseline"], launched


def test_orchestrator_aborts_if_stopped_while_paused(arui_env, monkeypatch,
                                                      tmp_path):
    """If the user resets / cancels the loop while research is paused,
    _execute should bail out without ever launching the run."""
    import asyncio
    from backend.app import orchestrator, notify
    from backend.app.db import SessionLocal
    from backend.app.models import Idea, Project

    db = SessionLocal()
    db.add(Project(id="proj-test", name="t", status="running",
                    validation_metric="val_mse"))
    db.add(Idea(id="idea-x", idea_id="x", project_id="proj-test",
                description="d", status="not_implemented", hpps={}))
    db.commit()
    db.close()

    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    orch = orchestrator.Orchestrator(str(proj_dir), n_slots=1)
    orch.running = False  # cancelled before _execute runs

    launched = []

    async def fake_launch(self, run_id, gpu_index, cfg):
        launched.append(run_id)
        return 1.0, True, ""

    monkeypatch.setattr(orchestrator.Orchestrator, "_launch", fake_launch)
    monkeypatch.setattr(notify, "research_paused", lambda: True)

    async def fast_sleep(_secs):
        return None

    monkeypatch.setattr(orchestrator.asyncio, "sleep", fast_sleep)

    asyncio.run(orch._execute("idea-x", 0))
    assert launched == []


# ─────────────────────────── PI agent gate ────────────────────────────


def test_pi_cycle_skips_when_paused(arui_env, monkeypatch):
    """pi.cycle() must early-return when research_paused is True — no
    LLM call, no nudges, no chat-message row. force=True still
    bypasses (manual override is intentional)."""
    from backend.app import notify, pi
    monkeypatch.setattr(notify, "research_paused", lambda: True)

    # If the gate didn't fire, _call would be invoked and (without API
    # keys) explode loudly. We sentinel it just in case.
    sentinel = {"called": False}

    def boom(*a, **kw):
        sentinel["called"] = True
        raise RuntimeError("pi.cycle should not reach _call when paused")

    monkeypatch.setattr(pi, "_call", boom)

    # Make sure pi_agent_enabled defaults to True so the ONLY reason
    # the cycle exits is the research-paused gate (not the enabled
    # gate).
    monkeypatch.setattr(pi, "_settings",
                        lambda: {"pi_agent_enabled": True,
                                 "pi_agent_model": "gemini-2.5-pro",
                                 "pi_cadence_minutes": 60})

    # Also make paper.project_mode return non-paper so cycle() takes
    # the research branch.
    from backend.app import paper
    monkeypatch.setattr(paper, "project_mode", lambda: "research")

    out = pi.cycle()
    assert out is None
    assert sentinel["called"] is False
