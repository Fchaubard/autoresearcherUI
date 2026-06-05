"""Unit tests for backend.app.stuck_detector (PLAN item #8).

Each state transition + the no-op (healthy) path is covered, plus the
side-effects of on_state_transition (chat bubble, Event row, escalation
email gating). Tests use the standard `arui_env` fixture so the DB lives
in a tmp dir and no actual network calls leave the process.
"""
from __future__ import annotations

import datetime as dt


def _iso(seconds_ago: float = 0) -> str:
    return (dt.datetime.now(dt.timezone.utc)
            - dt.timedelta(seconds=seconds_ago)).isoformat()


def _emit_strategic_event(db, msg: str, seconds_ago: float):
    """Helper: write a `strategic_review` Event row at a fixed offset."""
    import os
    from backend.app.models import Event
    db.add(Event(id="ev-" + os.urandom(4).hex(),
                 type="strategic_review", severity="info",
                 actor="council:test", message=msg,
                 created_at=_iso(seconds_ago)))
    db.commit()


# ─────────────────────────── compute_state ────────────────────────────


def test_healthy_when_no_signals(arui_env, db_session):
    """No reviews, no runs, no project → healthy with healthy reason."""
    from backend.app import stuck_detector
    snap = stuck_detector.compute_state()
    assert snap["state"] == "healthy"
    assert "nominal" in snap["reason"].lower()


def test_nagged_after_three_consecutive_reviews(arui_env, db_session):
    """3 consecutive identical strategic reviews trip the nagged state."""
    from backend.app import stuck_detector
    # 3 identical reviews, oldest first — emitted with decreasing age so
    # the newest is at offset 0.
    for i in range(3):
        _emit_strategic_event(db_session,
                              "Top blocker: build trusted_eval gate",
                              seconds_ago=300 - i * 60)
    snap = stuck_detector.compute_state()
    assert snap["state"] == "nagged"
    # The detail field must record the streak so the email digest can
    # surface the exact count.
    assert snap["details"]["consecutive_unimplemented_reviews"] >= 3


def test_stalled_after_five_consecutive_reviews(arui_env, db_session):
    """5+ identical reviews escalate to stalled (and trigger immediate
    email-escalation side effects elsewhere)."""
    from backend.app import stuck_detector
    for i in range(5):
        _emit_strategic_event(db_session,
                              "Top blocker: build trusted_eval gate",
                              seconds_ago=600 - i * 60)
    snap = stuck_detector.compute_state()
    assert snap["state"] == "stalled"
    assert snap["details"]["consecutive_unimplemented_reviews"] >= 5


def test_two_reviews_not_enough_to_nag(arui_env, db_session):
    """Only 2 consecutive identical reviews → still healthy."""
    from backend.app import stuck_detector
    for i in range(2):
        _emit_strategic_event(db_session,
                              "Top blocker: build trusted_eval gate",
                              seconds_ago=600 - i * 60)
    snap = stuck_detector.compute_state()
    assert snap["state"] == "healthy"


def test_looping_when_collision_rate_above_threshold(arui_env, db_session,
                                                       make_project,
                                                       make_run):
    """>30% of last 20 finished runs sharing a novelty_hash → looping."""
    from backend.app import stuck_detector
    make_project()
    # 20 runs total; 10 share the same novelty hash (50% > 30%).
    for i in range(10):
        make_run(id=f"a{i}", status="kept",
                 config={"novelty_hash": "deadbeefcafebabe"},
                 created_at=_iso(seconds_ago=100 - i),
                 ended_at=_iso(seconds_ago=80 - i))
    for i in range(10):
        make_run(id=f"b{i}", status="kept",
                 config={"novelty_hash": f"unique-{i:016x}"},
                 created_at=_iso(seconds_ago=200 - i),
                 ended_at=_iso(seconds_ago=180 - i))
    snap = stuck_detector.compute_state()
    assert snap["state"] in ("looping", "dry", "stalled")
    # At least the collision details should pop above threshold.
    assert snap["details"]["collision_rate"] > 0.30


def test_dry_when_no_kept_novel_in_window(arui_env, db_session,
                                            make_project, make_run):
    """50 launched runs with no kept_novel-class run → dry state."""
    from backend.app import stuck_detector
    make_project()
    # 50 crashes — definitely no novel kept run.
    for i in range(50):
        make_run(id=f"c{i}", status="crashed",
                 created_at=_iso(seconds_ago=5000 - i * 10),
                 ended_at=_iso(seconds_ago=4990 - i * 10))
    snap = stuck_detector.compute_state()
    assert snap["state"] == "dry"
    assert snap["details"]["kept_novel_in_window"] == 0


def test_stalled_beats_dry_in_severity_ordering(arui_env, db_session,
                                                  make_project, make_run):
    """If both stalled AND dry fire, report stalled (most actionable)."""
    from backend.app import stuck_detector
    make_project()
    for i in range(6):
        _emit_strategic_event(db_session,
                              "Top blocker: same", seconds_ago=600 - i * 60)
    for i in range(50):
        make_run(id=f"c{i}", status="crashed",
                 created_at=_iso(seconds_ago=5000 - i * 10),
                 ended_at=_iso(seconds_ago=4990 - i * 10))
    snap = stuck_detector.compute_state()
    assert snap["state"] == "stalled"


def test_kept_novel_status_counts_directly(arui_env, db_session,
                                             make_project, make_run):
    """A real kept_novel run resets the dry signal."""
    from backend.app import stuck_detector
    make_project()
    # Lots of crashed runs to make dry threaten…
    for i in range(40):
        make_run(id=f"c{i}", status="crashed",
                 created_at=_iso(seconds_ago=5000 - i * 10))
    # …plus one kept_novel which should clear the dry signal.
    make_run(id="n0", status="kept_novel", headline_metric=0.5,
             created_at=_iso(seconds_ago=100), ended_at=_iso(seconds_ago=50))
    snap = stuck_detector.compute_state()
    assert snap["details"]["kept_novel_in_window"] >= 1
    assert snap["state"] != "dry"


def test_collision_rate_from_metric_proxy(arui_env, db_session, make_project,
                                            make_run):
    """When novelty_hash is missing, we fall back to metric equivalence."""
    from backend.app import stuck_detector
    make_project()
    # 5 runs all reporting exactly 0.5 → 4 collisions out of 5.
    for i in range(5):
        make_run(id=f"d{i}", status="kept", headline_metric=0.5,
                 config={},
                 created_at=_iso(seconds_ago=200 - i * 5),
                 ended_at=_iso(seconds_ago=190 - i * 5))
    snap = stuck_detector.compute_state()
    assert snap["details"]["collision_rate"] > 0.30


# ─────────────────────── on_state_transition ─────────────────────────


def test_on_state_transition_writes_chat_and_event(arui_env, db_session):
    """Worsening transition emits a ChatMessage + an Event."""
    from backend.app import stuck_detector
    from backend.app.models import ChatMessage, Event
    stuck_detector.on_state_transition(
        "nagged", "healthy",
        {"state": "nagged", "details": {}, "reason": "test"})
    db_session.expire_all()
    cm = db_session.query(ChatMessage).all()
    ev = db_session.query(Event).all()
    assert any("NAGGED" in (c.content or "") for c in cm)
    assert any(e.type == "research_health" for e in ev)


def test_on_state_transition_skips_improvement(arui_env, db_session):
    """healthy ← stalled is a relief — DON'T fire a bubble."""
    from backend.app import stuck_detector
    from backend.app.models import ChatMessage
    stuck_detector.on_state_transition(
        "healthy", "stalled",
        {"state": "healthy", "details": {}, "reason": "ok"})
    db_session.expire_all()
    assert db_session.query(ChatMessage).count() == 0


def test_on_state_transition_stalled_calls_notify(arui_env, db_session,
                                                    monkeypatch):
    """Crossing into stalled tries to send an email-immediate digest."""
    from backend.app import notify, stuck_detector
    calls = []
    monkeypatch.setattr(notify, "send",
                         lambda *a, **kw: calls.append((a, kw)) or True)
    stuck_detector.on_state_transition(
        "stalled", "nagged",
        {"state": "stalled",
         "details": {"top_directive": "build trusted_eval",
                     "consecutive_unimplemented_reviews": 5},
         "reason": "5 reviews ignored"})
    assert calls, "expected notify.send to fire on stalled transition"
    subject = calls[0][0][0]
    assert "STALLED" in subject


def test_tick_persists_and_fires_only_on_worsening(arui_env, db_session):
    """tick() fires once when state worsens; second tick at same state
    must NOT re-fire."""
    from backend.app import stuck_detector
    from backend.app.models import ChatMessage
    # Plant 3 identical reviews → nagged on first tick.
    for i in range(3):
        _emit_strategic_event(db_session,
                              "Top blocker: build trusted_eval gate",
                              seconds_ago=300 - i * 60)
    snap1 = stuck_detector.tick()
    db_session.expire_all()
    n1 = db_session.query(ChatMessage).count()
    assert snap1["state"] == "nagged"
    # Second tick — no new reviews, state unchanged, no new bubble.
    stuck_detector.tick()
    db_session.expire_all()
    n2 = db_session.query(ChatMessage).count()
    assert n1 == n2 == 1


def test_compute_state_returns_well_formed_payload(arui_env, db_session):
    """Every compute_state result has the contract documented in the
    module docstring — state, details, reason — even on a fresh DB."""
    from backend.app import stuck_detector
    snap = stuck_detector.compute_state()
    assert "state" in snap and "details" in snap and "reason" in snap
    assert snap["state"] in {"healthy", "setting_up", "nagged", "stalled",
                               "looping", "dry", "needs_direction"}
    assert isinstance(snap["details"], dict)


# ─────────────────────────── setting_up ───────────────────────────────


def test_setting_up_when_onboarded_with_no_runs_and_unblessed(
        arui_env, db_session, make_project):
    """Operator just finished onboarding (Project row has a purpose)
    AND no runs have been launched yet AND preflight hasn't blessed:
    that is the SOP phase, not 'needs_direction'."""
    from backend.app import stuck_detector
    make_project(purpose=("Investigate whether SPSA gradient noise floors "
                          "improve over Adam in toy MLPs."))
    snap = stuck_detector.compute_state()
    assert snap["state"] == "setting_up", snap
    assert "scaffold" in snap["reason"].lower() \
        or "preflight" in snap["reason"].lower()


def test_setting_up_falls_back_to_healthy_when_blessed(
        arui_env, db_session, make_project):
    """Once preflight is blessed and the first run hasn't yet started,
    the setting_up signal must stop firing (we don't want the cyan pill
    sticking around forever on a stalled-pre-launch project)."""
    import os
    from backend.app import stuck_detector
    from backend.app.models import Setting
    make_project(purpose="Some real research question that justifies SOP.")
    db_session.add(Setting(key="preflight_blessed",
                           value={"blessed": True,
                                  "at": _iso(seconds_ago=10)}))
    db_session.commit()
    snap = stuck_detector.compute_state()
    # With bless set + no runs + no holding cue, the detector falls
    # through to healthy (nothing wrong, just waiting on launch).
    assert snap["state"] in ("healthy", "needs_direction"), snap


def test_setting_up_skipped_when_no_project_onboarded(arui_env, db_session):
    """Fresh DB, NO Project row at all — must stay healthy. The
    setting_up state should only ever appear *after* onboarding.
    This protects the marketing-screen / pre-onboarding view from
    showing a misleading cyan 'Setting up' pill."""
    from backend.app import stuck_detector
    snap = stuck_detector.compute_state()
    assert snap["state"] == "healthy", snap
