"""Unit tests for the PI "auto-propose next move" branch (Piece #5).

When stuck_detector has been in ``needs_direction`` for >= 15 minutes
AND research is not paused/halted AND no conclusion is in flight, the PI
cycle proactively kicks the council to propose the next move.

Tests:
  - Below the 15-minute threshold → no propose.
  - Above the 15-minute threshold → propose fires.
  - Auto-propose is suppressed if research is paused/halted.
  - Auto-propose is suppressed if a conclusion is in flight (pending /
    approved).
  - Audit trail: a chat bubble + Event is recorded so the operator can
    see WHY the PI didn't nag.
"""
from __future__ import annotations

import datetime as dt


def _iso(seconds_ago: float = 0) -> str:
    return (dt.datetime.now(dt.timezone.utc)
            - dt.timedelta(seconds=seconds_ago)).isoformat()


def _set_stuck(setting_setter, state: str, at: str):
    setting_setter("stuck_detector_state",
                   {"state": state, "details": {}, "reason": "", "at": at})


def test_no_propose_when_below_threshold(arui_env, db_session,
                                              setting_setter, monkeypatch):
    """needs_direction for only 5 minutes → don't auto-propose."""
    from backend.app import pi
    calls = []
    def fake_propose():
        calls.append(1)
        return {"started": True, "reason": "ok"}
    monkeypatch.setattr("backend.app.council.propose_next_move_async",
                         fake_propose)
    _set_stuck(setting_setter, "needs_direction", _iso(seconds_ago=300))
    assert pi._maybe_propose_next_move() is False
    assert calls == []


def test_propose_fires_above_threshold(arui_env, db_session,
                                              setting_setter, monkeypatch):
    """needs_direction for 20 minutes → propose is called."""
    from backend.app import pi
    calls = []
    def fake_propose():
        calls.append(1)
        return {"started": True, "reason": "ok"}
    monkeypatch.setattr("backend.app.council.propose_next_move_async",
                         fake_propose)
    _set_stuck(setting_setter, "needs_direction",
               _iso(seconds_ago=20 * 60))
    assert pi._maybe_propose_next_move() is True
    assert calls == [1]


def test_propose_suppressed_when_paused(arui_env, db_session,
                                            setting_setter, monkeypatch):
    """If research is paused, the propose branch is muted (operator
    explicitly stopped the loop)."""
    from backend.app import pi
    calls = []
    monkeypatch.setattr("backend.app.council.propose_next_move_async",
                         lambda: (calls.append(1) or
                                  {"started": True, "reason": "ok"}))
    monkeypatch.setattr("backend.app.notify.research_paused",
                         lambda: True)
    _set_stuck(setting_setter, "needs_direction",
               _iso(seconds_ago=20 * 60))
    assert pi._maybe_propose_next_move() is False
    assert calls == []


def test_propose_suppressed_when_conclusion_pending(
        arui_env, db_session, setting_setter, monkeypatch):
    """An in-flight conclusion review means the agent has already taken
    a position; don't step on it by proposing more work."""
    from backend.app import pi
    calls = []
    monkeypatch.setattr("backend.app.council.propose_next_move_async",
                         lambda: (calls.append(1) or
                                  {"started": True, "reason": "ok"}))
    _set_stuck(setting_setter, "needs_direction",
               _iso(seconds_ago=20 * 60))
    setting_setter("research_conclusion", {"status": "pending"})
    assert pi._maybe_propose_next_move() is False


def test_audit_bubble_and_event_emitted(arui_env, db_session,
                                              setting_setter, monkeypatch):
    """When the propose fires, a chat bubble + Event are written so the
    operator sees it in the Summary feed."""
    from backend.app import pi
    from backend.app.models import ChatMessage, Event
    monkeypatch.setattr("backend.app.council.propose_next_move_async",
                         lambda: {"started": True, "reason": "ok"})
    _set_stuck(setting_setter, "needs_direction",
               _iso(seconds_ago=20 * 60))
    assert pi._maybe_propose_next_move() is True
    db_session.expire_all()
    cm = db_session.query(ChatMessage).all()
    ev = db_session.query(Event).all()
    assert any("proactively asking council" in (c.content or "")
               for c in cm)
    assert any(e.type == "pi_auto_propose_next_move" for e in ev)


def test_cooldown_prevents_double_fire(arui_env, db_session,
                                              setting_setter, monkeypatch):
    """After firing once, a second call within the cooldown window is a
    no-op."""
    from backend.app import pi
    calls = []
    monkeypatch.setattr("backend.app.council.propose_next_move_async",
                         lambda: (calls.append(1) or
                                  {"started": True, "reason": "ok"}))
    _set_stuck(setting_setter, "needs_direction",
               _iso(seconds_ago=20 * 60))
    assert pi._maybe_propose_next_move() is True
    assert pi._maybe_propose_next_move() is False
    assert calls == [1]
