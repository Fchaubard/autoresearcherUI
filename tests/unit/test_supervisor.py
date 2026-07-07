"""Unit tests for the PI supervisor + lifecycle (the "never blocked" watchdog).

These cover the EXACT failure that wedged the research before: a conclusion
review left "pending" with no live worker (the worker was orphaned by a backend
restart / crashed / timed out), and the agent polling a verdict that never
came. The supervisor must detect that and re-trigger the review — while a
3-strike circuit breaker stops it retrying a doomed operation forever.
"""
import datetime as dt

import pytest


def _iso_ago(seconds: float) -> str:
    return (dt.datetime.now(dt.timezone.utc)
            - dt.timedelta(seconds=seconds)).isoformat()


# ── lifecycle: status + remediation circuit breaker ─────────────────────────
def test_set_phase_transition_resets_remediation(arui_env):
    from backend.app import lifecycle as lc
    lc.set_phase(lc.PHASE_RUNNING)
    lc.record_remediation("foo", "stuck")
    assert lc.remediation_count("foo") == 1
    # a REAL transition wipes the counters + marks healthy
    lc.set_phase(lc.PHASE_CONCLUSION_REVIEW)
    assert lc.remediation_count("foo") == 0
    assert lc.status()["health"] == lc.HEALTHY


def test_set_phase_same_phase_keeps_health(arui_env):
    from backend.app import lifecycle as lc
    lc.set_phase(lc.PHASE_CONCLUSION_REVIEW)
    lc.set_health(lc.RECOVERING, "working on it")
    # calling set_phase with the SAME phase must NOT reset health
    lc.set_phase(lc.PHASE_CONCLUSION_REVIEW)
    assert lc.status()["health"] == lc.RECOVERING


def test_record_remediation_three_strikes_hard_stalls(arui_env):
    from backend.app import lifecycle as lc
    lc.set_phase(lc.PHASE_CONCLUSION_REVIEW)
    lc.record_remediation("k", "stuck")
    assert lc.status()["health"] == lc.RECOVERING
    lc.record_remediation("k", "stuck")
    assert lc.status()["health"] == lc.RECOVERING
    lc.record_remediation("k", "stuck")          # 3rd strike
    assert lc.status()["health"] == lc.HARD_STALLED
    assert "needs you" in lc.status()["blocker_reason"]


def test_summary_line_reports_blocker(arui_env):
    from backend.app import lifecycle as lc
    lc.set_phase(lc.PHASE_CONCLUSION_REVIEW)
    lc.set_health(lc.HARD_STALLED, "council keys missing")
    line = lc.summary_line()
    assert "HARD STALLED" in line and "council keys missing" in line


# ── lifecycle: worker leases ─────────────────────────────────────────────────
def test_lease_acquire_alive_then_release(arui_env):
    from backend.app import lifecycle as lc
    lc.lease_acquire("w")
    assert lc.lease_alive("w", max_age_sec=999) is True
    lc.lease_release("w")
    assert lc.lease_get("w") is None
    assert lc.lease_alive("w", max_age_sec=999) is False


def test_lease_dead_pid_is_not_alive(arui_env):
    """Simulates an orphaned worker after a backend restart: the lease's pid no
    longer maps to a live process, so the lease is dead even if recent."""
    from backend.app import lifecycle as lc
    lc._set(lc._LEASE_KEY, {"w": {"started_at": _iso_ago(1),
                                  "heartbeat_at": _iso_ago(1),
                                  "pid": 2 ** 30}})   # a pid that won't exist
    assert lc.lease_alive("w", max_age_sec=999) is False


def test_lease_stale_heartbeat_is_not_alive(arui_env):
    import os
    from backend.app import lifecycle as lc
    lc._set(lc._LEASE_KEY, {"w": {"started_at": _iso_ago(9999),
                                  "heartbeat_at": _iso_ago(9999),
                                  "pid": os.getpid()}})
    assert lc.lease_alive("w", max_age_sec=60) is False


# ── supervisor: the recovery behaviours ─────────────────────────────────────
def _set_pending(council, *, conclude_at, verdict=None):
    state = {"status": "pending", "summary": "we improved val_loss",
             "answer_to_purpose": "yes", "evidence": ["run-1", "run-2"],
             "recommendation": "ship", "conclude_at": conclude_at}
    if verdict is not None:
        state["council_verdict"] = verdict
    council._conclusion_state_set(state)


def test_supervisor_noop_when_no_conclusion(arui_env, monkeypatch):
    from backend.app import council, supervisor
    calls = []
    monkeypatch.setattr(council, "review_completion_async",
                        lambda *a, **k: calls.append(a))
    supervisor.tick()                       # status defaults to "none"
    assert calls == []


def test_supervisor_noop_when_verdict_present(arui_env, monkeypatch):
    from backend.app import council, supervisor
    calls = []
    monkeypatch.setattr(council, "review_completion_async",
                        lambda *a, **k: calls.append(a))
    _set_pending(council, conclude_at=_iso_ago(99999),
                 verdict={"verdict": "APPROVED", "reviewed_at": _iso_ago(10)})
    supervisor.tick()
    assert calls == []                      # already resolved — don't re-trigger


def test_supervisor_noop_when_worker_alive(arui_env, monkeypatch):
    from backend.app import council, lifecycle, supervisor
    calls = []
    monkeypatch.setattr(council, "review_completion_async",
                        lambda *a, **k: calls.append(a))
    _set_pending(council, conclude_at=_iso_ago(99999))
    lifecycle.lease_acquire("completion_review")   # a live worker holds it
    supervisor.tick()
    assert calls == []
    assert lifecycle.status()["health"] == lifecycle.HEALTHY


def test_supervisor_within_grace_does_not_retrigger(arui_env, monkeypatch):
    from backend.app import council, supervisor
    calls = []
    monkeypatch.setattr(council, "review_completion_async",
                        lambda *a, **k: calls.append(a))
    _set_pending(council, conclude_at=_iso_ago(5))   # just submitted
    supervisor.tick()
    assert calls == []                      # give the fresh worker time


def test_supervisor_retriggers_orphaned_review(arui_env, monkeypatch):
    """The flagship case: pending, no live worker, well past grace → re-trigger
    + record a remediation + emit an Event."""
    from backend.app import council, lifecycle, supervisor
    calls = []
    monkeypatch.setattr(council, "review_completion_async",
                        lambda *a, **k: calls.append(a))
    _set_pending(council, conclude_at=_iso_ago(99999))   # stuck for ages
    supervisor.tick()
    assert len(calls) == 1
    # it re-triggered with the SAME evidence/summary
    assert calls[0][0] == ["run-1", "run-2"]
    assert lifecycle.status()["health"] == lifecycle.RECOVERING
    assert lifecycle.remediation_count("completion_review") == 1


def test_supervisor_orphaned_after_restart_deadpid(arui_env, monkeypatch):
    """A lease exists but its pid is dead (backend restarted) → treated as no
    live worker → re-trigger."""
    from backend.app import council, lifecycle, supervisor
    calls = []
    monkeypatch.setattr(council, "review_completion_async",
                        lambda *a, **k: calls.append(a))
    _set_pending(council, conclude_at=_iso_ago(99999))
    lifecycle._set(lifecycle._LEASE_KEY,
                   {"completion_review": {"started_at": _iso_ago(1),
                                          "heartbeat_at": _iso_ago(1),
                                          "pid": 2 ** 30}})
    supervisor.tick()
    assert len(calls) == 1


def test_supervisor_circuit_breaker_stops_after_three(arui_env, monkeypatch):
    from backend.app import council, lifecycle, supervisor
    calls = []
    monkeypatch.setattr(council, "review_completion_async",
                        lambda *a, **k: calls.append(a))
    _set_pending(council, conclude_at=_iso_ago(99999))
    supervisor.tick()   # strike 1
    supervisor.tick()   # strike 2
    supervisor.tick()   # strike 3 → HARD_STALLED
    assert lifecycle.status()["health"] == lifecycle.HARD_STALLED
    n_after_three = len(calls)
    supervisor.tick()   # must NOT re-trigger a doomed operation forever
    assert len(calls) == n_after_three
    assert lifecycle.status()["health"] == lifecycle.HARD_STALLED


# ── council resilience: worker ALWAYS writes a terminal verdict ──────────────
def test_completion_worker_always_writes_verdict_on_crash(arui_env, monkeypatch):
    from backend.app import council, lifecycle
    council._conclusion_state_set({"status": "pending", "evidence": []})

    def _boom(*a, **k):
        raise RuntimeError("reviewer exploded")

    monkeypatch.setattr(council, "_completion_review_worker_inner", _boom)
    council._completion_review_worker([], "s", "a", "r")
    st = council.conclusion_state()
    # never left 'pending' — a terminal verdict was written
    assert st["status"] == "needs_more"
    assert st["council_verdict"]["verdict"] == "NEEDS_MORE"
    # the lease was released even though the inner worker raised
    assert lifecycle.lease_get("completion_review") is None


def test_completion_worker_holds_then_releases_lease(arui_env, monkeypatch):
    from backend.app import council, lifecycle
    seen = {}

    def _inner(*a, **k):
        seen["alive_during"] = lifecycle.lease_alive(
            "completion_review", max_age_sec=999)

    monkeypatch.setattr(council, "_completion_review_worker_inner", _inner)
    council._completion_review_worker([], "s", "a", "r")
    assert seen["alive_during"] is True          # lease held while running
    assert lifecycle.lease_get("completion_review") is None   # released after


# ── council observability: launch emits an Event + sets the phase ───────────
def test_review_completion_async_emits_launch_event(arui_env, monkeypatch):
    from backend.app import council, lifecycle
    from backend.app.db import SessionLocal
    from backend.app.models import Event
    # don't spawn a real review thread
    monkeypatch.setattr(council, "_completion_review_worker",
                        lambda *a, **k: None)
    council.review_completion_async(["run-1"], "summary", "yes", "ship")
    assert lifecycle.status()["phase"] == lifecycle.PHASE_CONCLUSION_REVIEW
    db = SessionLocal()
    try:
        ev = db.query(Event).filter(Event.type == "council_launch").first()
    finally:
        db.close()
    assert ev is not None and "Council review launched" in ev.message


# ── research-agent idle-park watchdog (autonomy: never wait on a human) ──────
def test_idle_nudge_decision_fires_when_parked():
    from backend.app import supervisor as sv
    # alive, running, parked at prompt past grace, no strikes -> nudge
    assert sv._should_nudge_idle_agent(
        disable_bg=False, alive=True, halted=False, paused=False,
        concluding=False, boot_screen=False, busy=False, idle_prompt=True,
        idle_age=120, nudge_age=1e9, strikes=0) == "nudge"


def test_idle_nudge_waits_within_grace():
    from backend.app import supervisor as sv
    assert sv._should_nudge_idle_agent(
        False, True, False, False, False, False, False, True,
        idle_age=10, nudge_age=1e9, strikes=0) == "wait"


def test_idle_nudge_waits_during_cooldown():
    from backend.app import supervisor as sv
    assert sv._should_nudge_idle_agent(
        False, True, False, False, False, False, False, True,
        idle_age=999, nudge_age=5, strikes=1) == "wait"


def test_idle_nudge_hard_stalls_after_max_strikes():
    from backend.app import supervisor as sv
    assert sv._should_nudge_idle_agent(
        False, True, False, False, False, False, False, True,
        idle_age=999, nudge_age=999, strikes=3) == "hard_stall"


def test_idle_nudge_resets_when_busy():
    from backend.app import supervisor as sv
    # working -> clear tracking, never nudge
    assert sv._should_nudge_idle_agent(
        False, True, False, False, False, False, busy=True, idle_prompt=False,
        idle_age=999, nudge_age=999, strikes=0) == "reset"


def test_idle_nudge_resets_when_not_at_prompt():
    from backend.app import supervisor as sv
    assert sv._should_nudge_idle_agent(
        False, True, False, False, False, False, False, idle_prompt=False,
        idle_age=999, nudge_age=999, strikes=0) == "reset"


@pytest.mark.parametrize("halted,paused,concluding,boot,alive,dbg", [
    (True, False, False, False, True, False),   # halted -> never nudge
    (False, True, False, False, True, False),   # paused
    (False, False, True, False, True, False),   # council concluding -> legit wait
    (False, False, False, True, True, False),   # boot/consent screen
    (False, False, False, False, False, False), # dead (handled elsewhere)
    (False, False, False, False, True, True),   # bg disabled (unit/test env)
])
def test_idle_nudge_skips_on_guards(halted, paused, concluding, boot, alive, dbg):
    from backend.app import supervisor as sv
    assert sv._should_nudge_idle_agent(
        disable_bg=dbg, alive=alive, halted=halted, paused=paused,
        concluding=concluding, boot_screen=boot, busy=False, idle_prompt=True,
        idle_age=999, nudge_age=999, strikes=0) == "skip"


def test_idle_prompt_detection():
    from backend.app import supervisor as sv
    parked = ("some output\n"
              "──────────\n"
              "❯ \n"
              "  ⏵⏵ bypass permissions on (shift+tab to cycle")
    assert sv._agent_idle_prompt(parked.lower()) is True
    working = "✻ cogitated for 5m 15s\n  esc to interrupt"
    # no prompt box -> not "idle at prompt"; and busy wins upstream anyway
    assert sv._agent_idle_prompt(working.lower()) is False
    assert sv._agent_busy(working.lower()) is True
    assert sv._agent_idle_prompt("") is False


def test_busy_and_boot_markers():
    from backend.app import supervisor as sv
    assert sv._agent_busy("✳ improvising… (39s · esc to interrupt)".lower())
    assert sv._agent_boot_screen("Do you trust the files in this folder?".lower())
    assert not sv._agent_busy("❯ bypass permissions on")


def test_past_tense_completion_line_is_not_busy():
    """The parked pane shows a PAST-TENSE completion line + the bypass footer.
    That must read as idle (not busy) or the watchdog would never un-park it."""
    from backend.app import supervisor as sv
    parked = ("  call.\n"
              "✻ Cogitated for 5m 15s\n"
              "──────────\n"
              "❯ \n"
              "  ⏵⏵ bypass permissions on (shift+tab to cycle ·")
    low = parked.lower()
    assert sv._agent_busy(low) is False        # 'cogitated' must NOT be busy
    assert sv._agent_idle_prompt(low) is True
    # and the active form still reads busy:
    assert sv._agent_busy("improvising… (39s · esc to interrupt)".lower()) is True


def test_selection_menu_is_idle_not_busy():
    """The agent's INTERACTIVE SELECTION MENU ('enter to select · ↑/↓ to
    navigate · esc to cancel') is a park state waiting on a human. Its "↑/↓ to
    navigate" hint must NOT read as busy, and a stray "Welcome back!" in the
    Claude Code chrome must NOT read as a boot screen — either bug made the
    watchdog skip a genuinely-parked agent (observed live 2026-07-07)."""
    from backend.app import supervisor as sv
    menu = ("  ✻ Welcome back!\n"
            "  1. run the sweep\n"
            "  3. skip it, write up the final conclusion instead\n"
            "  4. type something.\n"
            "──────────\n"
            "  enter to select · ↑/↓ to navigate · esc to cancel\n")
    low = menu.lower()
    assert sv._agent_busy(low) is False          # "↑/↓ to navigate" != busy
    assert sv._agent_boot_screen(low) is False   # "Welcome back!" != boot
    assert sv._agent_idle_prompt(low) is True     # menu == parked/awaiting human
    # end-to-end decision: alive+running+parked past grace -> nudge
    assert sv._should_nudge_idle_agent(
        disable_bg=False, alive=True, halted=False, paused=False,
        concluding=False, boot_screen=sv._agent_boot_screen(low),
        busy=sv._agent_busy(low), idle_prompt=sv._agent_idle_prompt(low),
        idle_age=120, nudge_age=1e9, strikes=0) == "nudge"


def test_real_boot_consent_still_detected():
    from backend.app import supervisor as sv
    assert sv._agent_boot_screen("do you trust the files in this folder?".lower())
    assert sv._agent_boot_screen("not logged in · please run /login".lower())


def test_live_spinner_reads_busy_completion_does_not():
    """The live elapsed-time spinner '(35s · …)' means actively generating even
    when 'esc to interrupt' isn't in the captured tail (e.g. 'Elucidating…').
    The past-tense 'for 5m 15s' completion line must NOT read busy."""
    from backend.app import supervisor as sv
    assert sv._agent_busy("· elucidating… (35s · thinking some more)".lower())
    assert sv._agent_busy("(2m 3s · ↓ 1.3k tokens)".lower())
    assert sv._agent_busy("press up to edit queued messages".lower())
    assert sv._agent_busy("✻ cogitated for 5m 15s".lower()) is False


def test_first_park_tick_waits_then_nudges():
    """Grace must actually apply: idle_age 0 (just parked) -> wait; only after
    the grace window -> nudge."""
    from backend.app import supervisor as sv
    common = dict(disable_bg=False, alive=True, halted=False, paused=False,
                  concluding=False, boot_screen=False, busy=False,
                  idle_prompt=True, nudge_age=1e9, strikes=0)
    assert sv._should_nudge_idle_agent(idle_age=0, **common) == "wait"
    assert sv._should_nudge_idle_agent(idle_age=46, **common) == "nudge"
