"""Unit tests for the new agent-reported phase endpoint (PR 1 of the
state-control rewrite, 2026-06-05).

The agent calls ``POST /api/phase`` at every lifecycle transition. The
dashboard pill reads the persisted ``orchestrator.phase`` Setting
directly instead of inferring phase from tmux scrollback. These tests
pin the contract:

  * POST persists the value and emits a phase_changed Event ONLY on a
    real transition (re-posting the same phase mustn't flood the feed).
  * GET returns the persisted value when one exists.
  * GET falls back to a sensible derived phase when the agent has never
    called POST yet (legacy projects).
  * Unknown phases are accepted (forward-compat).
"""
from __future__ import annotations

import pytest


@pytest.fixture
def client(arui_env, fake_subprocess):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.app.api import router
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as c:
        yield c


def test_post_phase_persists_and_emits_event(client, db_session):
    from backend.app.models import Setting, Event
    r = client.post("/api/phase",
                    json={"phase": "planning",
                          "detail": {"idea_id": "sweep_lr_v2"}})
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["ok"] is True
    assert payload["phase"] == "planning"
    assert payload["transitioned"] is True
    db_session.expire_all()
    s = (db_session.query(Setting)
         .filter(Setting.key == "orchestrator.phase").first())
    assert s is not None
    assert s.value["phase"] == "planning"
    assert s.value["detail"] == {"idea_id": "sweep_lr_v2"}
    # phase_changed Event was emitted
    evs = db_session.query(Event).filter(
        Event.type == "phase_changed").all()
    assert len(evs) == 1
    assert "planning" in evs[0].message


def test_repeated_post_same_phase_does_not_emit_event(client, db_session):
    """Phase-changed Event MUST only fire on real transitions; otherwise
    the activity feed floods with one row per agent tick."""
    from backend.app.models import Event
    client.post("/api/phase", json={"phase": "planning"})
    client.post("/api/phase", json={"phase": "planning"})
    client.post("/api/phase", json={"phase": "planning"})
    db_session.expire_all()
    evs = db_session.query(Event).filter(
        Event.type == "phase_changed").all()
    assert len(evs) == 1, [e.message for e in evs]


def test_transition_emits_event_with_arrow(client, db_session):
    from backend.app.models import Event
    client.post("/api/phase", json={"phase": "planning"})
    client.post("/api/phase", json={"phase": "launching_runs"})
    db_session.expire_all()
    evs = (db_session.query(Event)
           .filter(Event.type == "phase_changed")
           .order_by(Event.created_at).all())
    assert len(evs) == 2
    assert "(none)" in evs[0].message and "planning" in evs[0].message
    assert "planning" in evs[1].message
    assert "launching_runs" in evs[1].message


def test_get_phase_returns_persisted(client):
    client.post("/api/phase",
                json={"phase": "watching_runs", "detail": {"n_runs": 3}})
    r = client.get("/api/phase")
    assert r.status_code == 200
    p = r.json()
    assert p["phase"] == "watching_runs"
    assert p["detail"] == {"n_runs": 3}
    assert p["fallback_used"] is False
    assert p["at"]   # ISO timestamp


def test_get_phase_falls_back_when_unreported(client, make_project, make_run):
    """Legacy projects (never called arui.phase()) get a DB-derived
    fallback so the pill is still useful."""
    # no Setting row exists; no runs → fallback says 'bootstrap'.
    r = client.get("/api/phase")
    assert r.json()["phase"] == "bootstrap"
    assert r.json()["fallback_used"] is True

    # one running run → fallback says 'watching_runs'.
    make_project()
    make_run(id="rr1", status="running")
    r = client.get("/api/phase")
    assert r.json()["phase"] == "watching_runs"
    assert r.json()["fallback_used"] is True


def test_unknown_phase_is_accepted(client):
    """Forward-compat: the SDK warns client-side; the backend stores
    whatever was POSTed, so a future phase like 'rebuttal_drafting'
    doesn't require a coordinated backend deploy."""
    r = client.post("/api/phase", json={"phase": "rebuttal_drafting"})
    assert r.status_code == 200
    assert r.json()["phase"] == "rebuttal_drafting"


def test_post_phase_requires_phase_field(client):
    r = client.post("/api/phase", json={})
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_phase_event_severity_is_info(client, db_session):
    """phase_changed is INFO — it's a normal lifecycle signal, not a
    warning, and shouldn't be styled as one in the activity feed."""
    from backend.app.models import Event
    client.post("/api/phase", json={"phase": "planning"})
    db_session.expire_all()
    ev = db_session.query(Event).filter(
        Event.type == "phase_changed").first()
    assert ev.severity == "info"


def test_phase_event_actor_is_agent(client, db_session):
    """The actor on phase_changed is 'agent' — distinguishes from PI,
    council, stuck_detector, etc. in the Summary feed filters."""
    from backend.app.models import Event
    client.post("/api/phase", json={"phase": "planning"})
    db_session.expire_all()
    ev = db_session.query(Event).filter(
        Event.type == "phase_changed").first()
    assert ev.actor == "agent"
