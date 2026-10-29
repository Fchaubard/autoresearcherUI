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
def test_dead_expected_agent_restarts_from_persisted_state(arui_env, monkeypatch):
    """A clean CLI exit must never strand the orchestrator in planning."""
    from backend.app import supervisor as sv, realrun, lifecycle as lc
    import backend.app.agent_watcher as aw

    realrun.set_expected(True, reason="test launch")
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    sv._dead_agent_save({"missing_at": now - 60, "attempts": 0,
                         "last_attempt": 0.0})
    calls = []
    monkeypatch.setattr(
        aw, "_restart_session",
        lambda session, resume=False, only_if_missing=False:
            calls.append((session, resume, only_if_missing)) or True)

    owned = sv._supervise_dead_research_agent(
        alive=False, halted=False, paused=False, concluding=False)

    assert owned is True
    assert calls == [("agent", True, True)]
    assert lc.status()["health"] == lc.RECOVERING
    assert sv._dead_agent_state()["attempts"] == 1


def test_dead_agent_retries_forever_with_capped_backoff(arui_env, monkeypatch):
    """Repeated launch failures are rate-limited, never converted to a
    permanent HARD_STALLED state that requires a human click."""
    from backend.app import supervisor as sv, realrun, lifecycle as lc
    import backend.app.agent_watcher as aw

    realrun.set_expected(True, reason="test launch")
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    sv._dead_agent_save({"missing_at": now - 9999, "attempts": 20,
                         "last_attempt": now - 9999})
    monkeypatch.setattr(aw, "_restart_session",
                        lambda session, resume=False: False)

    sv._supervise_dead_research_agent(
        alive=False, halted=False, paused=False, concluding=False)

    assert sv._dead_agent_state()["attempts"] == 21
    assert lc.status()["health"] == lc.RECOVERING
    assert lc.status()["health"] != lc.HARD_STALLED


def test_dead_agent_recovery_disabled_for_intentional_states(arui_env):
    from backend.app import supervisor as sv, realrun
    realrun.set_expected(True, reason="test launch")
    for guards in ((True, False, False), (False, True, False),
                   (False, False, True)):
        halted, paused, concluding = guards
        assert sv._supervise_dead_research_agent(
            alive=False, halted=halted, paused=paused,
            concluding=concluding) is False


def test_agent_alive_rejects_dead_remain_on_exit_pane(monkeypatch):
    from backend.app import supervisor as sv, tmux_safe

    class Result:
        returncode = 0
        stdout = "1\n"

    monkeypatch.setattr(tmux_safe.subprocess, "run", lambda *a, **k: Result())
    assert sv._agent_alive() is False


def test_idle_nudge_decision_fires_when_parked():
    from backend.app import supervisor as sv
    # alive, running, parked at prompt past grace, no strikes -> nudge
    assert sv._should_nudge_idle_agent(
        disable_bg=False, alive=True, halted=False, paused=False,
        concluding=False, boot_screen=False, busy=False, idle_prompt=True,
        idle_age=500, nudge_age=1e9, strikes=0) == "nudge"


class NudgeTerminal:
    """A tiny editable prompt model; capture reflects actual key effects."""
    def __init__(self, success_after=2, marker="›", failed_key=None):
        self.calls, self.draft, self.history = [], "", ""
        self.enters, self.clears, self.busy = 0, 0, False
        self.success_after, self.marker, self.failed_key = success_after, marker, failed_key
        self.edit_after_enter, self.capture_failure, self.cursor_override = None, None, None

    def screen(self):
        if self.busy:
            return self.history + "Working (3s · esc to interrupt)\n"
        return self.history + self.marker + " " + self.draft + "\nbypass permissions on\n"

    def __call__(self, cmd, **kwargs):
        from subprocess import CompletedProcess
        self.calls.append(cmd)
        if cmd[:2] == ["tmux", "capture-pane"]:
            if self.enters and self.capture_failure is not None:
                return CompletedProcess(cmd, self.capture_failure, stdout=b"")
            return CompletedProcess(cmd, 0, stdout=self.screen().encode())
        if cmd[:2] == ["tmux", "display-message"]:
            cursor = len(self.history.splitlines()) + max(1, len(self.draft.splitlines())) - 1
            return CompletedProcess(cmd, 0, stdout=str(cursor if self.cursor_override is None else self.cursor_override).encode())
        failed = ("-l" in cmd if self.failed_key == "-l" else cmd[-1] == self.failed_key)
        if failed:
            return CompletedProcess(cmd, 1, stdout=b"")
        if "-l" in cmd:
            self.draft = cmd[-1]
        elif cmd[-1] == "Enter":
            self.enters += 1
            if self.edit_after_enter is not None:
                self.draft = self.edit_after_enter
            elif self.success_after is not None and self.enters >= self.success_after:
                self.draft, self.busy = "", True
        elif cmd[-1] == "C-c":
            self.draft = ""
            self.clears += 1
        return CompletedProcess(cmd, 0, stdout=b"")


@pytest.mark.parametrize("marker", ["›", "❯"])
def test_nudge_retries_enter_when_text_remains_in_prompt(monkeypatch, marker):
    from backend.app import supervisor as sv
    terminal = NudgeTerminal(marker=marker)
    monkeypatch.setattr(sv._sp, "run", terminal)
    monkeypatch.setattr("time.sleep", lambda _: None)
    assert sv._send_agent_nudge() is True
    assert terminal.enters == 2 and terminal.clears == 0
    assert any("-J" in cmd for cmd in terminal.calls if cmd[:2] == ["tmux", "capture-pane"])


@pytest.mark.parametrize("failed_key", ["Escape", "C-u", "-l", "Enter"])
def test_nudge_never_claims_delivery_after_failed_tmux_send(monkeypatch, failed_key):
    from backend.app import supervisor as sv
    terminal = NudgeTerminal(failed_key=failed_key)
    monkeypatch.setattr(sv._sp, "run", terminal)
    monkeypatch.setattr("time.sleep", lambda _: None)
    assert sv._send_agent_nudge("[RESEARCH EVENT] r1 completed") is False
    assert not terminal.busy


@pytest.mark.parametrize("capture_failure", [0, 1])
def test_nudge_unverified_delivery_is_not_success(monkeypatch, capture_failure):
    from backend.app import supervisor as sv
    terminal = NudgeTerminal()
    terminal.capture_failure = capture_failure
    monkeypatch.setattr(sv._sp, "run", terminal)
    monkeypatch.setattr("time.sleep", lambda _: None)
    assert sv._send_agent_nudge("[RESEARCH EVENT] r1 completed") is False
    assert terminal.enters == 1 and terminal.clears == 0


def test_event_nudge_retries_lost_enter_and_verifies_acceptance(monkeypatch):
    from backend.app import supervisor as sv
    terminal = NudgeTerminal()
    monkeypatch.setattr(sv._sp, "run", terminal)
    monkeypatch.setattr("time.sleep", lambda _: None)
    assert sv._send_agent_nudge("[RESEARCH EVENT] r1 completed") is True
    assert terminal.enters == 2


def test_nudge_does_not_double_submit_once_agent_is_busy(monkeypatch):
    from backend.app import supervisor as sv
    terminal = NudgeTerminal(success_after=1)
    monkeypatch.setattr(sv._sp, "run", terminal)
    monkeypatch.setattr("time.sleep", lambda _: None)
    assert sv._send_agent_nudge() is True
    assert terminal.enters == 1 and terminal.clears == 0


@pytest.mark.parametrize("marker", ["›", "❯"])
def test_unsent_multiline_app_draft_is_cleared_after_two_lost_enters(monkeypatch, marker):
    from backend.app import supervisor as sv
    terminal = NudgeTerminal(success_after=None, marker=marker)
    monkeypatch.setattr(sv._sp, "run", terminal)
    monkeypatch.setattr("time.sleep", lambda _: None)
    assert not sv._send_agent_nudge("[RESEARCH EVENT] r1 completed\nKeep training intact.")
    assert terminal.enters == 2 and terminal.clears == 1 and not terminal.draft
    assert not sv._agent_has_draft(terminal.screen().lower())


@pytest.mark.parametrize("edited", ["My unrelated operator instruction", "[RESEARCH EVENT] r1 completed BUT DO NOT SEND THIS"])
def test_historical_marker_or_edited_draft_never_qualifies_for_retry_or_cleanup(monkeypatch, edited):
    from backend.app import supervisor as sv
    terminal = NudgeTerminal(success_after=None)
    terminal.history = "› [RESEARCH EVENT] r1 completed\nOld response\n"
    terminal.edit_after_enter = edited
    monkeypatch.setattr(sv._sp, "run", terminal)
    monkeypatch.setattr("time.sleep", lambda _: None)
    assert not sv._send_agent_nudge("[RESEARCH EVENT] r1 completed")
    assert terminal.enters == 1 and terminal.clears == 0 and terminal.draft == edited


def test_nudge_preserves_draft_written_after_supervisor_idle_check(monkeypatch):
    from backend.app import supervisor as sv
    terminal = NudgeTerminal()
    terminal.draft = "Operator is still editing this"
    monkeypatch.setattr(sv._sp, "run", terminal)
    assert not sv._send_agent_nudge("[RESEARCH EVENT] r1 completed")
    assert not any(cmd[:2] == ["tmux", "send-keys"] for cmd in terminal.calls)
    assert terminal.draft == "Operator is still editing this"


def test_matching_scrollback_without_cursor_ownership_is_never_cleared(monkeypatch):
    from backend.app import supervisor as sv
    terminal = NudgeTerminal(success_after=None)
    terminal.history = "Historical output\n"
    terminal.cursor_override = 0
    monkeypatch.setattr(sv._sp, "run", terminal)
    monkeypatch.setattr("time.sleep", lambda _: None)
    assert not sv._send_agent_nudge("[RESEARCH EVENT] r1 completed")
    assert terminal.enters == 1 and terminal.clears == 0


def test_installed_codex_clears_failed_multiline_draft_without_inference(arui_env, tmp_path, monkeypatch):
    """Exercise real native editing while deliberately dropping both Enters."""
    import shlex
    import shutil
    import subprocess
    import time
    if not shutil.which('codex'):
        pytest.skip('optional installed Codex integration')
    from backend.app.agent_cli import command
    from backend.app import supervisor as sv
    workspace, home = tmp_path / 'project', tmp_path / 'private-codex'
    workspace.mkdir(); home.mkdir()
    _, cli = command('gpt-6-astra', 'medium', workspace=workspace)
    cli += ' -c ' + shlex.quote('model_providers.arui_openai.base_url="http://127.0.0.1:9/v1"')
    run = subprocess.run
    run(['tmux', 'new-session', '-d', '-s', 'nudge-probe', '-x', '100', '-y', '35',
         '-c', str(workspace), '-e', f'CODEX_HOME={home}', '-e', 'OPENAI_API_KEY=unused-fixture-key', cli],
        capture_output=True, check=True)
    start = time.monotonic()
    while time.monotonic() - start < 12:
        pane = run(['tmux', 'capture-pane', '-p', '-t', '=nudge-probe:'], capture_output=True, text=True).stdout
        if 'Do you trust the contents' in pane:
            run(['tmux', 'send-keys', '-t', '=nudge-probe:', 'Enter'], capture_output=True)
        if 'Ask Codex to do anything' in pane and time.monotonic() - start > 3:
            break
        time.sleep(.1)
    else:
        pytest.fail('private Codex prompt did not become ready')
    enters = []
    def lose_enter(cmd, **kwargs):
        if cmd[:4] == ['tmux', 'send-keys', '-t', '=nudge-probe:'] and cmd[-1] == 'Enter':
            enters.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout=b'')
        return run(cmd, **kwargs)
    monkeypatch.setattr(sv._sp, 'run', lose_enter)
    message = '[RESEARCH EVENT] synthetic result completed\nKeep all valid training running.\nInspect the saved result next.'
    assert not sv._send_agent_nudge(message, session='nudge-probe')
    deadline, prompt = time.monotonic() + 3, None
    while time.monotonic() < deadline:
        pane = sv._nudge_pane('=nudge-probe:')
        prompt = sv._nudge_prompt(pane or '')
        if prompt and not prompt[0]:
            break
        time.sleep(.1)
    assert len(enters) == 2 and prompt and not prompt[0]
    assert run(['tmux', 'has-session', '-t', '=nudge-probe'], capture_output=True).returncode == 0


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


def test_agent_pane_capture_uses_exact_pane_target(monkeypatch):
    from backend.app import supervisor as sv
    calls = []

    class Result:
        returncode = 0
        stdout = "› Ask Codex to do anything\n"

    monkeypatch.setattr(sv._sp, "run",
                        lambda cmd, **kwargs: calls.append(cmd) or Result())
    assert sv._agent_pane_low() == "› ask codex to do anything\n"
    assert calls[0][3] == "=agent:"


def test_codex_idle_prompt_and_draft_detection():
    """Current Codex uses ›, not Claude's ❯, for its input prompt."""
    from backend.app import supervisor as sv
    parked = ("  completed the current batch\n"
              "──────────\n"
              "› Ask Codex to do anything\n"
              "  gpt-5.6-sol · working directory")
    low = parked.lower()
    assert sv._agent_idle_prompt(low) is True
    assert sv._agent_has_draft(low) is False
    assert sv._agent_has_draft("result\n› launch the next experiment") is True
    assert sv._agent_has_draft("result\n› \n  gpt-5.6-sol") is False


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
        idle_age=500, nudge_age=1e9, strikes=0) == "nudge"


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


def test_codex_queue_footer_is_pending_not_idle():
    from backend.app import supervisor as sv
    pane = "previous output\n\ntab to queue message          35% context left"
    assert sv._agent_pending_turn(pane) is True
    assert sv._agent_idle_prompt(pane) is False
    assert sv._agent_busy(pane) is False


def test_autonomy_nudge_is_objective_neutral():
    from backend.app import supervisor as sv
    low = sv._AGENT_NUDGE.lower()
    assert "_setup_prompt.txt" in low
    assert "research objective" in low
    assert "authoritative run" in low
    for leaked_domain in ("scikit-learn", "estimator families",
                          "target transforms", "feature sets"):
        assert leaked_domain not in low


def test_frozen_codex_pending_turn_is_cancelled_and_refed(
        arui_env, monkeypatch):
    import datetime as dt
    import backend.app.supervisor as sv
    from backend.app import notify, council
    monkeypatch.delenv("ARUI_DISABLE_BG", raising=False)
    monkeypatch.setattr(notify, "research_halted", lambda: (False, ""))
    monkeypatch.setattr(notify, "research_paused", lambda: False)
    monkeypatch.setattr(council, "conclusion_state", lambda: {"status": "none"})
    monkeypatch.setattr(sv, "_agent_alive", lambda: True)
    pane = "unchanged output\n\ntab to queue message   35% context left"
    monkeypatch.setattr(sv, "_agent_pane_low", lambda: pane)
    fingerprint = sv._hashlib.sha256(pane.encode()).hexdigest()
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat()
    sv._agent_idle_save({"pending_fingerprint": fingerprint,
                         "pending_since": old, "strikes": 0})
    sent = []
    monkeypatch.setattr(sv, "_send_agent_nudge",
                        lambda: sent.append("nudge") or True)
    tmux = []
    monkeypatch.setattr(sv._sp, "run",
                        lambda cmd, **kwargs: tmux.append(cmd))
    sv._supervise_research_agent()
    assert sent == ["nudge"]
    assert any(cmd[-1] == "Escape" for cmd in tmux)


def test_first_park_tick_waits_then_nudges():
    """Grace must actually apply: idle_age 0 (just parked) -> wait; only after
    the grace window -> nudge."""
    from backend.app import supervisor as sv
    common = dict(disable_bg=False, alive=True, halted=False, paused=False,
                  concluding=False, boot_screen=False, busy=False,
                  idle_prompt=True, nudge_age=1e9, strikes=0)
    assert sv._should_nudge_idle_agent(idle_age=0, **common) == "wait"
    assert sv._should_nudge_idle_agent(idle_age=500, **common) == "nudge"


def test_hard_stall_health_self_heals_when_agent_resumes(arui_env, monkeypatch):
    """A HARD_STALLED set by the idle watchdog must clear back to HEALTHY once
    the agent is working again — otherwise a transient/false stall pins the
    dashboard red forever."""
    import backend.app.supervisor as sv
    from backend.app import lifecycle as lc, notify, council
    # reach the real "reset" branch: bg enabled, not halted/paused/concluding,
    # agent alive, pane BUSY (active spinner).
    monkeypatch.delenv("ARUI_DISABLE_BG", raising=False)
    monkeypatch.setattr(notify, "research_halted", lambda: (False, ""))
    monkeypatch.setattr(notify, "research_paused", lambda: False)
    monkeypatch.setattr(council, "conclusion_state", lambda: {"status": "none"})
    monkeypatch.setattr(sv, "_agent_alive", lambda *a, **k: True)
    monkeypatch.setattr(sv, "_agent_pane_low",
                        lambda *a, **k: "improvising… (12s · esc to interrupt)")
    # simulate: we escalated the agent to hard-stall
    sv._agent_idle_save({"idle_since": "2020-01-01T00:00:00+00:00",
                         "last_nudge": "2020-01-01T00:00:00+00:00",
                         "strikes": 3, "escalated": True})
    lc.set_health(lc.HARD_STALLED,
                  "research agent parked at its prompt through 3 auto-continues")
    assert lc.status()["health"] == lc.HARD_STALLED
    sv._supervise_research_agent()
    assert lc.status()["health"] == lc.HEALTHY   # self-healed
    assert sv._agent_idle_state() == {}          # idle row cleared


def test_model_context_label_is_not_a_spinner():
    """Claude Code's 'Welcome back' chrome shows 'opus 4.8 (1m context)'. The
    '(1m' there must NOT be mistaken for the live spinner '(35s · …)', or the
    watchdog treats a parked agent (with that chrome in view) as busy forever."""
    from backend.app import supervisor as sv
    chrome = "│  opus 4.8 (1m     │ what's new                 │"
    assert sv._agent_busy(chrome.lower()) is False
    # real spinners still detected:
    assert sv._agent_busy("(35s · thinking)".lower()) is True
    assert sv._agent_busy("(2m 3s · ↓ 11k tokens)".lower()) is True
    # a genuinely parked pane that also contains the chrome reads idle:
    parked = ("│ opus 4.8 (1m context) │\n❯ \n"
              "  ⏵⏵ bypass permissions on (shift+tab to cycle")
    assert sv._agent_busy(parked.lower()) is False
    assert sv._agent_idle_prompt(parked.lower()) is True


# ── hung-run reaper ─────────────────────────────────────────────────────────
def test_run_cap_honours_estimate_and_default():
    from backend.app import supervisor as sv
    # no estimate -> the global default
    assert sv._run_cap_sec(0, default=1800, factor=3) == 1800
    # long estimate -> 3x headroom wins
    assert sv._run_cap_sec(1000, default=1800, factor=3) == 3000


def test_should_reap_only_running_overrun():
    from backend.app import supervisor as sv
    D, F = 1800, 3
    # running, well past the default cap -> reap
    assert sv._should_reap_run("running", 2000, 0, D, F) is True
    # running but within cap -> keep
    assert sv._should_reap_run("running", 100, 0, D, F) is False
    # running, within its estimate's headroom -> keep
    assert sv._should_reap_run("running", 2500, 1000, D, F) is False  # cap=3000
    # finished / queued runs are never reaped
    assert sv._should_reap_run("kept_novel", 999999, 0, D, F) is False
    assert sv._should_reap_run("queued", 999999, 0, D, F) is False
    # missing started_at (elapsed None) -> can't judge -> keep
    assert sv._should_reap_run("running", None, 0, D, F) is False


def test_reaper_kills_and_marks_crashed(arui_env, make_project, make_run, monkeypatch):
    """Integration: a RUNNING run past its cap is killed + marked crashed."""
    import datetime as dt
    import backend.app.supervisor as sv
    from backend.app import tmux_safe
    from backend.app.db import SessionLocal
    from backend.app.models import Run
    make_project()
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=3)).isoformat()
    make_run(id="hung1", run_name="hung1", status="running",
             started_at=old, est_time_sec=10)
    killed = []
    monkeypatch.setattr(tmux_safe, "kill_session",
                        lambda name, **k: killed.append(name) or (True, ""))
    monkeypatch.setattr(tmux_safe, "valid_name", lambda n: True)
    sv._supervise_stuck_runs()
    db = SessionLocal()
    try:
        r = db.query(Run).filter(Run.id == "hung1").first()
        assert r.status == "crashed"
        assert r.ended_at
        assert r.config.get("killed_by") == "supervisor:timeout"
    finally:
        db.close()
    assert "hung1" in killed   # its tmux session was killed


def test_auto_mode_footer_reads_as_idle_prompt():
    """New Claude Code defaults to 'auto mode on' (not 'bypass permissions on').
    The mode-independent '(shift+tab to cycle)' footer must be detected as an
    idle prompt, or a boot-parked agent is invisible to the watchdog."""
    from backend.app import supervisor as sv
    welcome = ('  Opus 4.8 (1M context) · API Usage Billing\n'
               '❯ Try "how does <filepath> work?"\n'
               '  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents')
    low = welcome.lower()
    assert sv._agent_idle_prompt(low) is True
    assert sv._agent_busy(low) is False
    assert sv._agent_boot_screen(low) is False
