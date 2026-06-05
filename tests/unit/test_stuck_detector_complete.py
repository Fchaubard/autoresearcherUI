"""Unit tests for the conclusion-flow states in backend.app.stuck_detector.

Covers:
  - ``awaiting_completion_review`` (purple/indigo): agent has POSTed
    /api/research/conclude, council is reviewing.
  - ``complete`` (green/trophy): council approved the conclusion.
  - Short-circuit ordering: conclusion-flow states dominate stale
    nagged/looping signals from the queue.
  - Reset behaviour: a rejected/cleared conclusion goes back to the
    pre-conclusion classification.
  - Transition side-effects emit chat bubble + Event.
"""
from __future__ import annotations

import datetime as dt


def _iso(seconds_ago: float = 0) -> str:
    return (dt.datetime.now(dt.timezone.utc)
            - dt.timedelta(seconds=seconds_ago)).isoformat()


def _set_conclusion(setting_setter, status: str, **extras):
    """Helper: write a research_conclusion Setting row at status."""
    val = dict(extras)
    val["status"] = status
    setting_setter("research_conclusion", val)


# ──────────────────────── compute_state ───────────────────────────────


def test_awaiting_completion_review_pending(arui_env, db_session,
                                              setting_setter):
    """status=pending → awaiting_completion_review with the agent's
    summary + evidence carried in details."""
    from backend.app import stuck_detector
    _set_conclusion(setting_setter, "pending",
                    summary="We hit val_acc 0.92 on cifar.",
                    answer_to_purpose="YES_CONCLUSIVELY",
                    evidence=["r1", "r2"],
                    recommendation="WRITE_PAPER",
                    conclude_at=_iso())
    snap = stuck_detector.compute_state()
    assert snap["state"] == "awaiting_completion_review"
    assert "council reviewing" in snap["reason"].lower()
    c = snap["details"]["conclusion"]
    assert c["status"] == "pending"
    assert c["evidence"] == ["r1", "r2"]
    assert c["answer_to_purpose"] == "YES_CONCLUSIVELY"


def test_complete_when_council_approved(arui_env, db_session,
                                          setting_setter):
    """status=approved → state==complete with the summary in the reason."""
    from backend.app import stuck_detector
    _set_conclusion(setting_setter, "approved",
                    summary="Beat baseline by 7 points across 3 seeds.",
                    answer_to_purpose="YES_CONCLUSIVELY",
                    evidence=["r1", "r2", "r3"],
                    recommendation="WRITE_PAPER",
                    council_verdict={"verdict": "APPROVED",
                                       "reasons": ["sound"],
                                       "missing_evidence": [],
                                       "summary": "approved"})
    snap = stuck_detector.compute_state()
    assert snap["state"] == "complete"
    assert "ready to write the paper" in snap["reason"].lower()
    assert ("Beat baseline" in snap["reason"]
            or "beat baseline" in snap["reason"].lower())


def test_conclusion_short_circuits_nagged(arui_env, db_session,
                                            setting_setter):
    """Even if 3 consecutive strategic reviews on the same top directive
    would normally nag, an in-flight conclusion review wins."""
    from backend.app import stuck_detector
    from backend.app.models import Event
    import os
    # Plant 3 identical strategic_review events that would normally trip
    # nagged.
    for i in range(3):
        db_session.add(Event(
            id="ev-" + os.urandom(4).hex(),
            type="strategic_review", severity="info",
            actor="council:test",
            message="Top blocker: build trusted_eval gate",
            created_at=_iso(seconds_ago=300 - i * 60)))
    db_session.commit()
    _set_conclusion(setting_setter, "pending",
                    summary="…", answer_to_purpose="YES_CONCLUSIVELY",
                    evidence=[], recommendation="WRITE_PAPER")
    snap = stuck_detector.compute_state()
    assert snap["state"] == "awaiting_completion_review"


def test_conclusion_approved_short_circuits_looping(arui_env, db_session,
                                                       setting_setter,
                                                       make_project,
                                                       make_run):
    """approved conclusion beats looping signal — the dashboard MUST
    surface "complete" not "looping" when the user hits done."""
    from backend.app import stuck_detector
    make_project()
    # Build a looping history.
    for i in range(20):
        make_run(id=f"x{i}", status="kept", headline_metric=0.5,
                 created_at=_iso(seconds_ago=200 - i * 5))
    _set_conclusion(setting_setter, "approved",
                    summary="conclusively answered",
                    answer_to_purpose="YES_CONCLUSIVELY",
                    evidence=["x0"], recommendation="WRITE_PAPER",
                    council_verdict={"verdict": "APPROVED"})
    snap = stuck_detector.compute_state()
    assert snap["state"] == "complete"


def test_rejected_conclusion_falls_through(arui_env, db_session,
                                              setting_setter):
    """status=rejected → don't short-circuit; the dashboard returns to
    the normal classification (here, healthy — no other signals)."""
    from backend.app import stuck_detector
    _set_conclusion(setting_setter, "rejected",
                    summary="not good enough",
                    answer_to_purpose="YES_CONCLUSIVELY",
                    evidence=[], recommendation="WRITE_PAPER",
                    council_verdict={"verdict": "REJECTED",
                                       "missing_evidence": ["x", "y"]})
    snap = stuck_detector.compute_state()
    assert snap["state"] == "healthy"   # nothing else firing


def test_none_status_does_not_short_circuit(arui_env, db_session,
                                                setting_setter):
    """A row with status=none must NOT trip the conclusion branch."""
    from backend.app import stuck_detector
    _set_conclusion(setting_setter, "none")
    snap = stuck_detector.compute_state()
    assert snap["state"] == "healthy"


# ──────────────────── on_state_transition side-effects ───────────────


def test_transition_into_complete_emits_bubble(arui_env, db_session):
    """healthy → complete is a sev-0 → sev-0 transition but we MUST
    fire the chat bubble (it's the celebratory event the user must
    see)."""
    from backend.app import stuck_detector
    from backend.app.models import ChatMessage, Event
    stuck_detector.on_state_transition(
        "complete", "healthy",
        {"state": "complete", "details": {}, "reason":
         "Research complete: cifar SOTA. Ready to write the paper."})
    db_session.expire_all()
    cm = db_session.query(ChatMessage).all()
    ev = db_session.query(Event).all()
    assert any("COMPLETE" in (c.content or "") for c in cm)
    assert any(e.type == "research_complete" for e in ev)


def test_transition_into_awaiting_emits_bubble(arui_env, db_session):
    """healthy → awaiting_completion_review also fires a bubble."""
    from backend.app import stuck_detector
    from backend.app.models import ChatMessage, Event
    stuck_detector.on_state_transition(
        "awaiting_completion_review", "healthy",
        {"state": "awaiting_completion_review", "details": {},
         "reason": "agent declared done"})
    db_session.expire_all()
    cm = db_session.query(ChatMessage).all()
    ev = db_session.query(Event).all()
    assert any("AWAITING_COMPLETION_REVIEW" in (c.content or "")
               for c in cm)
    assert any(e.type == "research_awaiting_completion_review"
               for e in ev)
