"""Unit tests for the /api/research/conclude flow + council completion
review (Pieces #1, #2 of the conclude-or-propose redesign).

Covers:
  - Happy path POST /api/research/conclude validates + persists +
    fires async review.
  - Validation: missing summary / bad answer_to_purpose / bad
    recommendation all 400.
  - Council APPROVED → Setting row flips to approved.
  - Council REJECTED → Setting row flips to rejected + missing_evidence
    captured.
  - GET /api/research/conclusion returns the live state.
  - Operator clear: /api/research/conclusion/clear wipes the row +
    optionally upserts a BLOCKER directive.
  - Auto-approve when no reviewers configured (Claude-only / e2e env).
  - Dashboard short-circuit: pending → awaiting_completion_review;
    approved → complete (sanity-check the integration).
"""
from __future__ import annotations

import json


def _post_json(client, path, body):
    """FastAPI test-client POST helper that returns the parsed body."""
    r = client.post(path, json=body)
    return r.status_code, r.json()


def _get_json(client, path):
    r = client.get(path)
    return r.status_code, r.json()


def _client(arui_env):
    """FastAPI TestClient bound to the backend app."""
    from fastapi.testclient import TestClient
    from backend.app.api import router
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


# ────────────────────────── happy path ────────────────────────────────


def test_conclude_happy_path_persists_pending(arui_env, db_session,
                                                  make_project,
                                                  monkeypatch):
    """POST /api/research/conclude sets status=pending + Setting row."""
    from backend.app import council as _c
    make_project()
    # Stub the async worker so we don't actually call LLMs.
    monkeypatch.setattr(
        _c, "_completion_review_worker", lambda *a, **kw: None)
    client = _client(arui_env)
    code, body = _post_json(client, "/api/research/conclude", {
        "summary": "We hit 92% on CIFAR-10 across 3 seeds (mean 0.918, std 0.004).",
        "answer_to_purpose": "YES_CONCLUSIVELY",
        "evidence": ["r1", "r2", "r3"],
        "recommendation": "WRITE_PAPER",
    })
    assert code == 200, body
    assert body["ok"] is True
    conc = body["conclusion"]
    assert conc["status"] == "pending"
    assert conc["answer_to_purpose"] == "YES_CONCLUSIVELY"
    assert conc["evidence"] == ["r1", "r2", "r3"]


def test_conclude_emits_event(arui_env, db_session, make_project,
                                  monkeypatch):
    """The endpoint emits a `research_concluded` Event (severity=info)."""
    from backend.app import council as _c
    from backend.app.models import Event
    make_project()
    monkeypatch.setattr(
        _c, "_completion_review_worker", lambda *a, **kw: None)
    client = _client(arui_env)
    code, body = _post_json(client, "/api/research/conclude", {
        "summary": "x",
        "answer_to_purpose": "YES_CONCLUSIVELY",
        "evidence": [],
        "recommendation": "WRITE_PAPER",
    })
    assert code == 200, body
    db_session.expire_all()
    evs = db_session.query(Event).all()
    assert any(e.type == "research_concluded"
               and e.severity == "info" for e in evs)


# ────────────────────────── validation ────────────────────────────────


def test_conclude_rejects_missing_summary(arui_env, make_project):
    make_project()
    client = _client(arui_env)
    code, body = _post_json(client, "/api/research/conclude", {
        "summary": "",
        "answer_to_purpose": "YES_CONCLUSIVELY",
        "evidence": [], "recommendation": "WRITE_PAPER",
    })
    assert code == 400
    assert body["ok"] is False
    assert "summary" in body["error"].lower()


def test_conclude_rejects_bad_answer(arui_env, make_project):
    make_project()
    client = _client(arui_env)
    code, body = _post_json(client, "/api/research/conclude", {
        "summary": "x", "answer_to_purpose": "MAYBE",
        "evidence": [], "recommendation": "WRITE_PAPER",
    })
    assert code == 400
    assert "answer_to_purpose" in body["error"]


def test_conclude_rejects_bad_recommendation(arui_env, make_project):
    make_project()
    client = _client(arui_env)
    code, body = _post_json(client, "/api/research/conclude", {
        "summary": "x", "answer_to_purpose": "YES_CONCLUSIVELY",
        "evidence": [], "recommendation": "PARTY",
    })
    assert code == 400
    assert "recommendation" in body["error"]


# ──────────────────────── council worker outcomes ─────────────────────


def test_completion_worker_approved_when_reviewers_say_so(
        arui_env, db_session, make_project, make_run, monkeypatch):
    """All working reviewers vote APPROVED → final status=approved."""
    from backend.app import council as _c
    make_project()
    make_run(id="r1", status="kept", headline_metric=0.92)
    # Force at least one reviewer to be "available" and intercept the
    # actual call so the worker sees its decision.
    monkeypatch.setattr(_c, "_available_reviewers",
                         lambda cfg: ["openai"])
    monkeypatch.setattr(_c, "_call_reviewer",
                         lambda *a, **kw: {
                             "verdict": "APPROVED",
                             "reasons": ["evidence is solid"],
                             "missing_evidence": [],
                             "summary": "fine to write",
                         })
    _c._completion_review_worker(["r1"], "we won", "YES_CONCLUSIVELY",
                                   "WRITE_PAPER")
    cs = _c.conclusion_state()
    assert cs["status"] == "approved"
    assert cs["council_verdict"]["verdict"] == "APPROVED"


def test_completion_worker_rejected_collects_missing_evidence(
        arui_env, db_session, make_project, make_run, monkeypatch):
    """Any REJECTED verdict → final status=rejected and
    missing_evidence is collected (prefixed with reviewer name)."""
    from backend.app import council as _c
    make_project()
    make_run(id="r1", status="kept", headline_metric=0.5)
    monkeypatch.setattr(_c, "_available_reviewers",
                         lambda cfg: ["openai"])
    monkeypatch.setattr(_c, "_call_reviewer",
                         lambda *a, **kw: {
                             "verdict": "REJECTED",
                             "reasons": ["single seed"],
                             "missing_evidence": ["run 3 more seeds",
                                                   "compare to baseline"],
                             "summary": "not yet",
                         })
    _c._completion_review_worker(["r1"], "we won", "YES_CONCLUSIVELY",
                                   "WRITE_PAPER")
    cs = _c.conclusion_state()
    assert cs["status"] == "rejected"
    assert cs["council_verdict"]["verdict"] == "REJECTED"
    missing = cs["council_verdict"]["missing_evidence"]
    assert any("3 more seeds" in m for m in missing)
    # Reviewer name is prefixed so the agent knows which reviewer asked.
    assert all(m.startswith("[") for m in missing)


def test_completion_worker_auto_approves_when_no_reviewers(
        arui_env, db_session, make_project, make_run, monkeypatch):
    """No reviewers configured → auto-approved + recorded honestly."""
    from backend.app import council as _c
    make_project()
    monkeypatch.setattr(_c, "_available_reviewers", lambda cfg: [])
    # Set initial state as pending so the worker has something to mutate.
    _c._conclusion_state_set({
        "status": "pending", "summary": "x",
        "answer_to_purpose": "YES_CONCLUSIVELY",
        "evidence": [], "recommendation": "WRITE_PAPER"})
    _c._completion_review_worker([], "x", "YES_CONCLUSIVELY",
                                   "WRITE_PAPER")
    cs = _c.conclusion_state()
    assert cs["status"] == "approved"
    assert cs["council_verdict"]["auto"] is True


# ──────────────────────── GET + clear endpoints ───────────────────────


def test_get_conclusion_returns_none_by_default(arui_env, make_project):
    make_project()
    client = _client(arui_env)
    code, body = _get_json(client, "/api/research/conclusion")
    assert code == 200
    assert body["status"] == "none"


def test_get_conclusion_returns_live_state(arui_env, db_session,
                                                make_project, monkeypatch):
    from backend.app import council as _c
    make_project()
    monkeypatch.setattr(_c, "_completion_review_worker",
                         lambda *a, **kw: None)
    client = _client(arui_env)
    _post_json(client, "/api/research/conclude", {
        "summary": "x", "answer_to_purpose": "YES_CONCLUSIVELY",
        "evidence": ["r1"], "recommendation": "WRITE_PAPER"})
    code, body = _get_json(client, "/api/research/conclusion")
    assert code == 200
    assert body["status"] == "pending"
    assert body["evidence"] == ["r1"]


def test_clear_conclusion_wipes_state(arui_env, db_session, make_project,
                                         monkeypatch):
    from backend.app import council as _c
    make_project()
    monkeypatch.setattr(_c, "_completion_review_worker",
                         lambda *a, **kw: None)
    client = _client(arui_env)
    _post_json(client, "/api/research/conclude", {
        "summary": "x", "answer_to_purpose": "YES_CONCLUSIVELY",
        "evidence": [], "recommendation": "WRITE_PAPER"})
    code, body = _post_json(client, "/api/research/conclusion/clear", {
        "reason": "operator disagreed"})
    assert code == 200
    assert body["ok"] is True
    assert body["conclusion"]["status"] == "none"


def test_clear_conclusion_upserts_blocker_directive(
        arui_env, db_session, make_project, monkeypatch, tmp_path):
    """When the operator includes a blocker_directive, it lands in
    directives.jsonl as an open BLOCKER."""
    from backend.app import council as _c
    from backend.app import directives as _d
    make_project()
    # Point directives at a tmp file so the unit test doesn't need a
    # real workspace dir.
    _d.set_path_override(tmp_path / "directives.jsonl")
    monkeypatch.setattr(_c, "_completion_review_worker",
                         lambda *a, **kw: None)
    client = _client(arui_env)
    _post_json(client, "/api/research/conclude", {
        "summary": "x", "answer_to_purpose": "YES_CONCLUSIVELY",
        "evidence": [], "recommendation": "WRITE_PAPER"})
    code, body = _post_json(client, "/api/research/conclusion/clear", {
        "reason": "need 3-seed runs",
        "blocker_directive": {
            "type": "BLOCKER_INFRA", "priority": 1000,
            "what": "Run 3 seeds for every kept config.",
            "acceptance": "seeds 1/2/3 logged for top-3 kept runs.",
            "idea_class": "INFRA", "why": "ops rejected",
            "author": "operator",
        }})
    assert code == 200
    assert body["blocker"] is not None
    assert body["blocker"]["type"] == "BLOCKER_INFRA"
    items = _d.read_all()
    assert any(d.get("type") == "BLOCKER_INFRA"
               and d.get("status") == "open"
               for d in items)


# ───────────────────── dashboard integration sanity ──────────────────


def test_compute_state_sees_pending_after_post(arui_env, db_session,
                                                    make_project,
                                                    monkeypatch):
    from backend.app import council as _c
    from backend.app import stuck_detector
    make_project()
    monkeypatch.setattr(_c, "_completion_review_worker",
                         lambda *a, **kw: None)
    client = _client(arui_env)
    _post_json(client, "/api/research/conclude", {
        "summary": "x", "answer_to_purpose": "YES_CONCLUSIVELY",
        "evidence": [], "recommendation": "WRITE_PAPER"})
    snap = stuck_detector.compute_state()
    assert snap["state"] == "awaiting_completion_review"


def test_compute_state_sees_approved_after_worker(
        arui_env, db_session, make_project, make_run, monkeypatch):
    from backend.app import council as _c
    from backend.app import stuck_detector
    make_project()
    make_run(id="r1", status="kept", headline_metric=0.92)
    monkeypatch.setattr(_c, "_available_reviewers",
                         lambda cfg: ["openai"])
    monkeypatch.setattr(_c, "_call_reviewer", lambda *a, **kw: {
        "verdict": "APPROVED", "reasons": [], "missing_evidence": [],
        "summary": "ok"})
    _c.review_completion_async(["r1"], "won", "YES_CONCLUSIVELY",
                                  "WRITE_PAPER")
    # review_completion_async spawns a thread; in tests we monkeypatched
    # the LLM, but the thread runs the SAME _completion_review_worker.
    # Call it synchronously to keep the test deterministic.
    _c._completion_review_worker(["r1"], "won", "YES_CONCLUSIVELY",
                                   "WRITE_PAPER")
    snap = stuck_detector.compute_state()
    assert snap["state"] == "complete"
