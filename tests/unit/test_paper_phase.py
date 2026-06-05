"""Unit tests for backend.app.paper_phase (2026-06-05 paper rebuild)."""
from __future__ import annotations
import pytest


def test_set_and_get_phase_round_trip(arui_env, db_session):
    from backend.app import paper_phase as pp
    pp.set_phase("paper.draft_v0", actor="author",
                  progress={"draft": {"sections": 6}})
    p = pp.get_phase()
    assert p["phase"] == "paper.draft_v0"
    assert p["actor"] == "author"
    assert p["fallback_used"] is False


def test_get_phase_default_when_unreported(arui_env):
    from backend.app import paper_phase as pp
    p = pp.get_phase()
    assert p["phase"] == "paper.whittle_claims"
    assert p["fallback_used"] is True


def test_set_phase_only_emits_event_on_transition(arui_env, db_session):
    from backend.app import paper_phase as pp
    from backend.app.models import Event
    pp.set_phase("paper.lit_review", actor="author")
    pp.set_phase("paper.lit_review", actor="author")
    pp.set_phase("paper.lit_review", actor="author")
    n = db_session.query(Event).filter(
        Event.type == "phase_changed").count()
    assert n == 1


def test_request_and_approve_plan(arui_env, db_session, make_project,
                                      make_run):
    """Approving the plan must (a) set gate=approved and (b) transition
    Run.status from 'proposed' to 'queued'."""
    from backend.app import paper_phase as pp
    make_project()
    make_run(id="ab1", status="proposed", context="paper")
    make_run(id="ab2", status="proposed", context="paper")
    pp.request_plan_approval(note="3 datasets × 5 seeds")
    g = pp.get_gate()
    assert g["plan"]["status"] == "pending"
    out = pp.approve_plan(by="alice")
    assert out["queued_count"] == 2
    db_session.expire_all()
    from backend.app.models import Run
    statuses = {r.id: r.status for r in db_session.query(Run).all()}
    assert statuses == {"ab1": "queued", "ab2": "queued"}


def test_plan_approved_helper(arui_env, db_session):
    from backend.app import paper_phase as pp
    assert pp.plan_approved() is False
    pp.request_plan_approval()
    assert pp.plan_approved() is False
    pp.approve_plan()
    assert pp.plan_approved() is True


def test_status_overview_includes_phase_and_issues_when_pending_approval(
        arui_env, db_session, make_project, make_run):
    from backend.app import paper_phase as pp
    make_project()
    make_run(id="ab1", status="proposed", context="paper")
    pp.set_phase("paper.operator_review", actor="author")
    pp.request_plan_approval(note="please review")
    snap = pp.get_status_overview()
    assert snap["phase"]["phase"] == "paper.operator_review"
    codes = [i["code"] for i in (snap["issues"] or [])]
    assert "operator_approval_required" in codes
