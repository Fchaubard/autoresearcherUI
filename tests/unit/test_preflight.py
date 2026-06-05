"""Unit tests for the 3-step pre-flight SOP (task #6).

Every major code change MUST do three things before any real run is
allowed:

  1. Train the model on a static batch of data and achieve ~0 train
     loss. If not, there's a bug in train.py.
  2. The first init must produce uniform distribution at the
     classification head. If not, there's a bug in the architecture.
  3. ONLY THEN invoke council bless to confirm the code is doing the
     'purpose' research correctly.

These tests pin:
  - the two new POST endpoints write Setting key "preflight" and emit
    timestamped flags
  - /api/council/bless refuses to even start a review unless steps 1+2
    are already recorded fresh (status becomes
    "blocked_on_preflight")
  - /api/council/bless DOES proceed once all three are passed
  - the preflight_changed_at marker invalidates older preflight rows
    (stale-detection)
"""
from __future__ import annotations

import datetime as dt

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


def _preflight_row(db_session):
    from backend.app.models import Setting
    row = db_session.query(Setting).filter(
        Setting.key == "preflight").first()
    return dict(row.value) if row and isinstance(row.value, dict) else {}


# ───────────────── endpoint 1: static_overfit ──────────────────────────

def test_preflight_static_overfit_endpoint(client, db_session):
    """POST /api/preflight/static_overfit records step-1 evidence and a
    timestamp into Setting('preflight'), and returns the summary with
    static_overfit_passed=True."""
    r = client.post("/api/preflight/static_overfit",
                    json={"evidence": "overfit_smoke memorised 16 ex "
                                       "in 200 steps",
                          "final_loss": 0.0008})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    pf = body["preflight"]
    assert pf["static_overfit_passed"] is True
    assert pf["uniform_init_passed"] is False
    assert pf["blessed"] is False
    assert pf["static_overfit_final_loss"] == 0.0008
    assert pf["static_overfit_at_iso"]
    # And the row is on disk for the next worker / endpoint to see.
    db_session.expire_all()
    saved = _preflight_row(db_session)
    assert saved.get("static_overfit_at_iso")
    assert (saved.get("static_overfit_evidence") or "").startswith(
        "overfit_smoke")


def test_preflight_static_overfit_tolerant_to_missing_loss(client):
    """final_loss is optional — the endpoint should still record the
    timestamp + evidence even if the agent forgot to include it."""
    r = client.post("/api/preflight/static_overfit",
                    json={"evidence": "just trust me"})
    assert r.status_code == 200
    body = r.json()
    assert body["preflight"]["static_overfit_passed"] is True
    assert body["preflight"]["static_overfit_final_loss"] is None


# ───────────────── endpoint 2: uniform_init ────────────────────────────

def test_preflight_uniform_init_endpoint(client, db_session):
    """POST /api/preflight/uniform_init records step-2 evidence and a
    timestamp; static_overfit stays unaffected."""
    r = client.post("/api/preflight/uniform_init",
                    json={"evidence":
                          "head probs within 0.5% of 1/1000; "
                          "entropy 6.905 vs log(1000)=6.908",
                          "entropy": 6.905})
    assert r.status_code == 200
    pf = r.json()["preflight"]
    assert pf["uniform_init_passed"] is True
    assert pf["uniform_init_entropy"] == 6.905
    # Step 1 still missing — exposed via the OTHER pill.
    assert pf["static_overfit_passed"] is False


def test_preflight_status_get_reflects_both(client):
    """GET /api/preflight/status returns the same shape regardless of
    which steps have been recorded."""
    client.post("/api/preflight/static_overfit",
                json={"evidence": "x", "final_loss": 0.0001})
    client.post("/api/preflight/uniform_init",
                json={"evidence": "y", "entropy": 6.9})
    pf = client.get("/api/preflight/status").json()
    assert pf["static_overfit_passed"] is True
    assert pf["uniform_init_passed"] is True
    assert pf["blessed"] is False  # bless hasn't been called yet


# ───────────────── bless gate: must reject without preflight ───────────

def test_bless_rejected_when_preflight_missing(client, monkeypatch):
    """POST /api/council/bless refuses to even spawn a worker when
    steps 1 + 2 are missing. The returned status is
    'blocked_on_preflight' and the blockers list names which steps are
    missing. Critically: no council reviewer should have been called."""
    # If anything tries to call into a real reviewer, blow up so the
    # test fails loudly.
    from backend.app import council as _c
    sentinel = {"called": False}

    def boom(*a, **kw):
        sentinel["called"] = True
        return {"approved": True}
    monkeypatch.setattr(_c, "_call_reviewer", boom)

    r = client.post("/api/council/bless", json={"workspace": "/tmp/x"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "blocked_on_preflight"
    assert sentinel["called"] is False, (
        "council bless must NOT call into reviewers when preflight "
        "steps 1+2 are missing — wastes tokens + gives meaningless "
        "verdicts")
    blockers = body.get("blockers") or []
    joined = " ".join(blockers).lower()
    assert "static" in joined and "uniform" in joined, (
        f"both missing steps should be named in the blocker list — "
        f"got {blockers!r}")
    pf = body["preflight"]
    assert pf["static_overfit_passed"] is False
    assert pf["uniform_init_passed"] is False
    assert pf["blessed"] is False


def test_bless_rejected_when_only_one_preflight_passed(client, monkeypatch):
    """Half-finished SOP (step 1 done, step 2 missing) is still
    blocked."""
    from backend.app import council as _c
    sentinel = {"called": False}

    def boom(*a, **kw):
        sentinel["called"] = True
        return {"approved": True}
    monkeypatch.setattr(_c, "_call_reviewer", boom)

    client.post("/api/preflight/static_overfit",
                json={"evidence": "ok", "final_loss": 0.0005})
    r = client.post("/api/council/bless", json={"workspace": "/tmp/x"})
    assert r.json()["status"] == "blocked_on_preflight"
    assert sentinel["called"] is False
    blockers = " ".join(r.json().get("blockers") or []).lower()
    assert "uniform" in blockers
    assert "static" not in blockers, (
        "static_overfit already passed — should NOT be in blocker list")


# ───────────────── bless gate: allowed when all 3 are passed ───────────

def test_bless_allowed_when_all_three_passed(client, monkeypatch, tmp_path):
    """When steps 1 + 2 are recorded, /api/council/bless proceeds
    normally. Since there are no reviewer API keys in this unit-test
    environment, _bless_worker takes the 'no reviewers' branch and
    auto-approves — which is the contract for Claude-only setups."""
    from backend.app import council as _c
    # Make the worker synchronous so the test can assert on the result
    # without timing flakes.
    import threading as _t

    class _SyncThread:
        def __init__(self, target, args=(), kwargs=None, daemon=None,
                     name=None):
            self.target = target
            self.args = args
            self.kwargs = kwargs or {}

        def start(self):
            self.target(*self.args, **self.kwargs)
    monkeypatch.setattr(_t, "Thread", _SyncThread)
    monkeypatch.setattr(_c.threading, "Thread", _SyncThread)

    # Make sure there are no reviewer keys — _bless_worker takes its
    # auto-approve branch.
    for k in ("GEMINI_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)

    # Record both preflight steps.
    r1 = client.post("/api/preflight/static_overfit",
                     json={"evidence": "ok", "final_loss": 0.0001})
    assert r1.status_code == 200
    r2 = client.post("/api/preflight/uniform_init",
                     json={"evidence": "ok", "entropy": 6.9})
    assert r2.status_code == 200

    # Now bless. Use the workspace override so the codebase-collection
    # step doesn't reject for empty-workspace.
    r = client.post("/api/council/bless",
                    json={"workspace": str(tmp_path)})
    assert r.status_code == 200
    body = r.json()
    # NOT blocked_on_preflight any more — the SOP gate let it through.
    assert body["status"] in ("approved", "pending"), (
        f"bless should proceed once both preflight steps are recorded "
        f"— got status={body['status']!r}")
    # The bless_status also surfaces the preflight summary back to the
    # dashboard, so the 3-pill banner can render in one round-trip.
    pf = body["preflight"]
    assert pf["static_overfit_passed"] is True
    assert pf["uniform_init_passed"] is True


# ───────────────── stale-detection: code_changed invalidates them ──────

def test_preflight_code_changed_invalidates_prior_steps(client):
    """POST /api/preflight/code_changed bumps changed_at_iso so any
    preflight step recorded BEFORE that moment is treated as stale.
    Bless then refuses again."""
    # Step 1 + 2 first.
    client.post("/api/preflight/static_overfit",
                json={"evidence": "ok", "final_loss": 0.0})
    client.post("/api/preflight/uniform_init",
                json={"evidence": "ok", "entropy": 6.9})
    # Both pills green.
    pf = client.get("/api/preflight/status").json()
    assert pf["static_overfit_passed"] and pf["uniform_init_passed"]

    # Force the changed_at marker to be strictly AFTER the existing
    # preflight timestamps. We can't rely on real wall-clock ticks at
    # millisecond resolution inside a single test, so reach into the
    # state directly and bump.
    from backend.app import council as _c
    future = (dt.datetime.now(dt.timezone.utc)
              + dt.timedelta(seconds=5)).isoformat()
    st = _c._preflight_state_get()
    st["changed_at_iso"] = future
    _c._preflight_state_set(st)

    pf2 = client.get("/api/preflight/status").json()
    assert pf2["static_overfit_passed"] is False, (
        "preflight recorded BEFORE the code_changed marker must be "
        "treated as stale")
    assert pf2["uniform_init_passed"] is False


def test_preflight_code_changed_endpoint_resets_approval(client):
    """POST /api/preflight/code_changed also clears any prior bless
    approval — re-running it is mandatory."""
    from backend.app import council as _c
    # Pretend the council had previously approved.
    _c._bless_state_set({"status": "approved", "summary": "ok"})
    r = client.post("/api/preflight/code_changed",
                    json={"reason": "rewrote train.py"})
    assert r.status_code == 200
    # Approval cleared.
    bless = _c._bless_state_get()
    assert bless["status"] == "not_requested"
