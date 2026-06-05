"""Unit tests for the hard-halt API + gate (RESEARCH_IMPROVEMENT_PLAN #6).

/api/halt sets the research_halted flag; /api/track/run then 423s every
run including probes; /api/halt/resume lifts the halt (passcode-gated
when configured); banner event is emitted.
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


@pytest.fixture
def auto_bless(setting_setter):
    setting_setter("code_bless", {"status": "approved"})


def test_halt_status_default_false(client):
    r = client.get("/api/halt/status").json()
    assert r["halted"] is False


def test_halt_sets_flag(client):
    r = client.post("/api/halt",
                     json={"reason": "Council escalated"})
    assert r.json()["halted"] is True
    s = client.get("/api/halt/status").json()
    assert s["halted"] is True
    assert "Council escalated" in s["reason"]


def test_halt_blocks_real_run(client, auto_bless):
    client.post("/api/halt", json={"reason": "stop"})
    r = client.post("/api/track/run",
                     json={"name": "real_run_a", "config": {}})
    assert r.status_code == 423
    assert r.json()["reason"] == "research_halted"


def test_halt_blocks_probe_too(client, auto_bless):
    """Hard halt is harder than just blocker — probes/smokes are blocked."""
    client.post("/api/halt", json={"reason": "all stop"})
    r = client.post("/api/track/run",
                     json={"name": "_probe_smoke", "config": {}})
    assert r.status_code == 423


def test_halt_resume_without_passcode_when_unset(client, auto_bless):
    """If no passcode is configured, resume is open (dev path)."""
    client.post("/api/halt", json={"reason": "stop"})
    r = client.post("/api/halt/resume", json={})
    assert r.json()["halted"] is False
    # Track-run now works again.
    r2 = client.post("/api/track/run",
                      json={"name": "real_run_b", "config": {}})
    assert r2.status_code == 200


def test_halt_resume_requires_passcode_when_set(client, setting_setter):
    setting_setter("onboarding", {"passcode": "letmein"})
    client.post("/api/halt", json={"reason": "stop"})
    bad = client.post("/api/halt/resume",
                       json={"passcode": "wrong"})
    assert bad.status_code == 401
    # Still halted
    assert client.get("/api/halt/status").json()["halted"] is True
    good = client.post("/api/halt/resume",
                        json={"passcode": "letmein"})
    assert good.json()["halted"] is False


def test_halt_emits_event(client):
    client.post("/api/halt", json={"reason": "ev test"})
    # Events show up on /api/runs? Use the events list via DB.
    from backend.app.db import SessionLocal
    from backend.app.models import Event
    db = SessionLocal()
    try:
        ev = (db.query(Event)
              .filter(Event.type == "research_halted")
              .order_by(Event.created_at.desc()).first())
        assert ev is not None
        assert "ev test" in (ev.message or "")
        assert ev.severity == "critical"
    finally:
        db.close()


def test_halt_resume_event_emitted(client):
    client.post("/api/halt", json={"reason": "x"})
    client.post("/api/halt/resume", json={})
    from backend.app.db import SessionLocal
    from backend.app.models import Event
    db = SessionLocal()
    try:
        ev = (db.query(Event)
              .filter(Event.type == "research_resumed")
              .order_by(Event.created_at.desc()).first())
        assert ev is not None
    finally:
        db.close()


def test_halt_is_idempotent_updates_reason(client):
    client.post("/api/halt", json={"reason": "first"})
    client.post("/api/halt", json={"reason": "second"})
    s = client.get("/api/halt/status").json()
    assert "second" in s["reason"]


def test_pi_no_auto_halt_on_escalation_event(arui_env, monkeypatch):
    """REGRESSION (2026-06-05): pi.cycle() MUST NOT auto-halt when an
    escalation_halt Event sits in the DB. Legacy behaviour froze the
    GPUs for 7 hours overnight while the agent had actually already
    answered the directive via a sibling experiment. The new contract
    is: never halt; let deliberation continue."""
    import datetime as dt
    from backend.app import notify, pi
    from backend.app.db import SessionLocal
    from backend.app.models import Event
    import os
    db = SessionLocal()
    try:
        db.add(Event(id="ev-" + os.urandom(4).hex(),
                     type="escalation_halt",
                     severity="critical", actor="council",
                     message="ESCALATION_HALT",
                     created_at=dt.datetime.now(
                         dt.timezone.utc).isoformat()))
        db.commit()
    finally:
        db.close()
    # Run a cycle; without API keys it returns None at the model check —
    # that's fine, the assertion we care about is that research_halted
    # was NOT set as a side-effect of the escalation_halt event.
    pi.cycle(force=True)
    halted, _reason = notify.research_halted()
    assert halted is False, ("PI must no longer auto-halt on "
                              "escalation_halt — see council.py 2026-06-05")
