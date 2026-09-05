"""Supervisor — the deterministic, lifecycle-level watchdog.

Called from monitor.py's loop (~6s). PURELY ADDITIVE: it never changes how the
agent, council, or runs behave. It only makes sure the research is never left
blocked because a background worker timed out, crashed, or got orphaned by a
backend restart, and it keeps `lifecycle` up to date so the activity feed +
emails can explain WHY we're idle.

Design rules (from the Gemini-3-Pro review):
  • LOCAL + FAST only. No LLM calls, no network. It talks to SQLite + the
    lifecycle leases. (Re-triggering a council review spawns a background
    thread; we do not block on it.)
  • BOUNDED remediation. Every re-trigger increments a per-key counter; after
    3 strikes the phase flips to HARD_STALLED (a human is needed) instead of
    retrying a doomed operation forever.
  • Observable. Every remediation emits an Event to the feed.

PR-1 scope: the research-conclusion review (the exact deadlock we hit — gpt-5
timed out, the worker was orphaned by a restart, and the agent polled a verdict
that never came for ~2h45m). The structure generalises to the other phases
(bless gate, idle GPUs, scaffolding, paper) — each is just another
`_supervise_*` checker added to `tick()`.
"""
from __future__ import annotations

# How long to let a fresh completion-review worker run before we consider a
# still-"pending" conclusion to be stuck. The worker itself is now bounded (a
# slow reviewer fails fast at the 240s socket timeout, ≤2 reviewers), so a
# healthy review settles well under this. Past it with no live worker = stuck.
_COMPLETION_GRACE_SEC = 360


def tick() -> None:
    """One supervisor pass. Best-effort; never raises into the monitor loop."""
    try:
        _supervise_completion_review()
    except Exception as e:                              # noqa: BLE001
        print(f"[supervisor] tick error: {e}", flush=True)
    try:
        _supervise_paper_mode()
    except Exception as e:                              # noqa: BLE001
        print(f"[supervisor] paper tick error: {e}", flush=True)
    try:
        _supervise_research_agent()
    except Exception as e:                              # noqa: BLE001
        print(f"[supervisor] research-agent tick error: {e}", flush=True)
    try:
        _supervise_stuck_runs()
    except Exception as e:                              # noqa: BLE001
        print(f"[supervisor] stuck-run tick error: {e}", flush=True)


# Paper-mode phases where the Author Agent is supposed to be actively working
# (so its tmux session dying is a stall). submission_ready/error are
# terminal/manual. (Autopilot: there is no operator_review wait phase anymore.)
_PAPER_WORKING_PHASES = {
    "paper.whittle_claims", "paper.lit_review", "paper.draft_v0",
    "paper.plan_ablations", "paper.build_gantt", "paper.run_ablations",
    "paper.reviewer_simulator",
}


# How long to let a freshly (re)spawned author boot + report its first phase
# before "alive but no phase + idle pane" counts as a parked boot needing the
# brief re-fed. A normal boot + first phase report lands well under this.
_AUTHOR_BOOT_GRACE_SEC = 180


def _should_refeed(fallback_used: bool, alive: bool, busy: bool,
                   spawn_age: float, feed_remediations: int,
                   grace: float = _AUTHOR_BOOT_GRACE_SEC,
                   max_rem: int = 3) -> bool:
    """Pure decision (testable): is the author parked at boot (alive, idle
    pane, never reported a phase, past the boot grace) so we should re-feed
    its brief? Bounded by a 3-strike circuit breaker."""
    if not alive or busy:
        return False                 # dead (handled elsewhere) or working
    if not fallback_used:
        return False                 # it reported a phase -> it started fine
    if spawn_age < grace:
        return False                 # still within a normal boot window
    return feed_remediations < max_rem


def _paper_action(phase: str, fallback_used: bool, author_alive: bool,
                  remediations: int):
    """Pure decision (testable): what should the PI do about paper mode now?
    Returns (action, reason) where action is None | 'restart' | 'hard_stall'."""
    if fallback_used or phase not in _PAPER_WORKING_PHASES:
        return (None, "")                # paper mode idle / waiting on human / done
    if author_alive:
        return (None, "")                # author is working — nothing to do
    label = phase.replace("paper.", "")
    if remediations >= 3:                # MAX_REMEDIATION
        return ("hard_stall",
                f"the author agent keeps dying during {label}")
    return ("restart", f"the author agent died during {label}")


def _supervise_paper_mode() -> None:
    """Keep PAPER mode unblocked the same way the research loop is: if the
    paper is in an active author phase but the 'author' tmux session has died,
    restart it (3-strike circuit breaker -> HARD_STALLED). The author then
    resumes from its phase + the persisted decisions, so a crashed author
    never silently strands the paper."""
    from . import author_agent, lifecycle, paper_phase
    st = paper_phase.get_phase()
    phase = st.get("phase", "")
    try:
        alive = author_agent._tmux_alive("author")
    except Exception:                                   # noqa: BLE001
        alive = True                                    # can't tell -> don't act
    # Boot-parking: author ALIVE but never started working (no phase reported,
    # idle pane) past the boot grace -> re-feed the brief rather than leave it
    # parked at the Claude Code welcome screen forever.
    try:
        busy = author_agent._looks_busy("author")
    except Exception:                                   # noqa: BLE001
        busy = True
    if _should_refeed(bool(st.get("fallback_used", True)), alive, busy,
                      author_agent.spawn_age_sec(),
                      lifecycle.remediation_count("paper_author_feed")):
        lifecycle.set_phase(lifecycle.PHASE_PAPER)
        lifecycle.record_remediation(
            "paper_author_feed",
            "author booted but never started working -- re-feeding the brief")
        try:
            author_agent.refeed_if_idle()
        except Exception as e:                          # noqa: BLE001
            lifecycle.emit_event("supervisor_error",
                                 f"author re-feed failed: {e}",
                                 severity="warning")
        return
    action, reason = _paper_action(
        phase, bool(st.get("fallback_used", True)), alive,
        lifecycle.remediation_count("paper_author"))
    if action == "restart":
        lifecycle.set_phase(lifecycle.PHASE_PAPER)
        lifecycle.record_remediation("paper_author",
                                     reason + " -- restarting it")
        try:
            author_agent.start()
        except Exception as e:                          # noqa: BLE001
            lifecycle.emit_event("supervisor_error",
                                 f"author restart failed: {e}",
                                 severity="warning")
    elif action == "hard_stall":
        lifecycle.set_phase(lifecycle.PHASE_PAPER)
        lifecycle.set_health(lifecycle.HARD_STALLED, reason + " -- needs you")


def _supervise_completion_review() -> None:
    """If the agent submitted a conclusion and the council review is stuck
    'pending' with no live worker, re-trigger it so the agent never waits
    forever on a verdict that will never come."""
    from . import council, lifecycle

    st = council.conclusion_state()
    if st.get("status") != "pending":
        return                                   # resolved, or nothing submitted
    cv = st.get("council_verdict") or {}
    if cv.get("reviewed_at"):
        return                                   # a verdict already landed

    # We are in the conclusion-review phase.
    lifecycle.set_phase(lifecycle.PHASE_CONCLUSION_REVIEW)

    # Is a completion-review worker actually alive and recent?
    if lifecycle.lease_alive("completion_review", max_age_sec=_COMPLETION_GRACE_SEC):
        lifecycle.set_health(lifecycle.HEALTHY,
                             "council reviewing the conclusion")
        return

    # No live worker. How long has the conclusion been pending?
    age = lifecycle._age(st.get("conclude_at") or st.get("updated_at") or "")
    if age < _COMPLETION_GRACE_SEC:
        # Within grace — the worker may just be starting / slow. Note it.
        lifecycle.set_health(lifecycle.HEALTHY,
                             "council reviewing the conclusion")
        return

    if lifecycle.remediation_count("completion_review") >= lifecycle.MAX_REMEDIATION:
        lifecycle.set_health(
            lifecycle.HARD_STALLED,
            "completion review keeps failing — submit a tighter conclusion or "
            "check the council API keys")
        return

    # Re-trigger — NEVER give up because of a timeout / crash / restart.
    lifecycle.record_remediation(
        "completion_review",
        f"completion review orphaned/stalled {int(age)}s — re-triggering")
    try:
        council.review_completion_async(
            st.get("evidence") or [], st.get("summary") or "",
            st.get("answer_to_purpose") or "", st.get("recommendation") or "")
    except Exception as e:                              # noqa: BLE001
        lifecycle.emit_event("supervisor_error",
                             f"completion-review re-trigger failed: {e}",
                             severity="warning")


# ── research-agent idle-park watchdog ─────────────────────────────────────
# The autonomous research agent is a Claude Code REPL. When it finishes a
# line of work it can PARK at the prompt asking the operator "want me to try
# X or Y? — your call" and, with no human watching, sit idle forever. Paper
# mode already has refeed_if_idle for exactly this; the research loop did NOT,
# so the only thing that could ever un-park the agent was the PI's HOURLY
# nudge (and on a CPU node even the PI's idle-GPU signal is absent). This gives
# the research agent the same fast, deterministic un-parker the author has.
#
# It NEVER answers the agent's question for it — it re-anchors the mandate and
# tells the agent to decide: launch the next on-mandate experiment, or, if the
# question is truly answered, POST a conclusion. A human is never in the loop.

import os as _os
import re as _re
import subprocess as _sp
import datetime as _dt
import hashlib as _hashlib

_AGENT_SESSION = "agent"
_AGENT_IDLE_GRACE_SEC = 420         # 7 min: don't thrash long thinking turns
_AGENT_IDLE_COOLDOWN_SEC = 420      # min gap between nudges (7 min)
_AGENT_IDLE_MAX_STRIKES = 3         # after N nudges with no progress -> human
_AGENT_FROZEN_PENDING_SEC = int(_os.environ.get(
    "ARUI_AGENT_FROZEN_PENDING_SEC", "1200"))  # 20m; long reasoning is valid
_AGENT_IDLE_KEY = "research_agent_idle_watch"
_AGENT_DEAD_KEY = "research_agent_dead_watch"
_AGENT_DEAD_GRACE_SEC = 15
_AGENT_RESTART_MAX_BACKOFF_SEC = 300

# Substrings that mean Claude Code is actively working (do NOT nudge).
# Claude Code shows "esc to interrupt" (and a live "(Ns · ↑/↓ N tokens …)"
# stream) ONLY while it is actively generating. When it finishes it prints a
# PAST-TENSE completion line — "Cogitated for 5m 15s", "Sautéed for 39s" — and
# drops back to the idle prompt whose footer reads "bypass permissions on
# (shift+tab to cycle)". So we must NOT key "busy" off the verb stems
# (cogitat/improvis/sauté/…): those appear in the finished-and-parked pane too
# and would make the watchdog think a parked agent is still working (it never
# nudges). Only the active-generation markers below are reliable.
# "esc to interrupt" is shown ONLY while Claude Code is actively generating —
# even the live token stream renders it as "(Ns · ↓ N tokens · esc to
# interrupt)", so this one substring covers all active states. We deliberately
# do NOT match the ↑/↓ arrows: those also appear in the interactive selection
# menu's "↑/↓ to navigate" hint and in plain scrollback, which would make a
# parked agent look busy and never get un-parked.
_AGENT_BUSY_MARKERS = (
    "esc to interrupt",
    "compacting conversation",
    "press up to edit queued",   # messages queued while the agent is working
)

# The live spinner ALWAYS carries an elapsed-time counter in parens —
# "(35s · thinking some more)", "(2m 3s · ↓ 1.3k tokens)", "(37s · esc to
# interrupt)". A finished/parked pane instead shows a PAST-TENSE line with NO
# paren ("Cogitated for 5m 15s"). So "(<n>s" / "(<n>m" is the single most
# reliable "actively generating" signal, catching active states that don't
# happen to render "esc to interrupt" in the captured tail.
# The elapsed time ALWAYS ends in seconds — "(35s", "(2m 3s", "(5m 15s" — and
# is followed by " · ". We must NOT match Claude Code's model-context label
# "opus 4.8 (1m context)" in the "Welcome back" chrome: "(1m" with no trailing
# seconds. Requiring a seconds field (optionally preceded by a minutes field)
# excludes it. "esc to interrupt" remains a substring backstop for any format.
_AGENT_SPINNER_RE = _re.compile(r"\(\d+s\b|\(\d+m\s+\d+s\b")
# Boot / consent / auth screens — handled by realrun spawn + agent_watcher's
# auth-zombie recovery, NOT by this watchdog. Don't nudge over them.
# ONLY strings that appear during real boot / consent / auth — NOT the
# "Welcome back!" chrome, which Claude Code keeps in scrollback for the whole
# session and would permanently suppress the watchdog.
_AGENT_BOOT_MARKERS = (
    "do you trust", "yes, i accept", "not logged in", "please run /login",
    "run /login",
)

_AGENT_NUDGE = (
    "[AUTONOMY - no human is watching this session] Do not stop and ask for "
    "confirmation or say \"your call\". You are the autonomous research agent. "
    "Re-read _setup_prompt.txt, the project purpose, directives.jsonl, "
    "ideas.md, results.md, and lessons.md. Inspect the authoritative run "
    "ledger and currently active processes, preserve valid in-flight work, "
    "then continue the highest-priority unfinished work that directly serves "
    "this workspace's research objective. Generate and launch a diverse batch "
    "of new on-mandate experiments when compute is available; choose methods, "
    "replication, and concurrency from the actual brief and evidence rather "
    "than assumptions from another project. NEVER idle or wait for a human "
    "decision. Keep researching until the operator halts you or the harness's "
    "explicit terminal protocol is satisfied."
)


def _agent_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _agent_pane_low(session: str = _AGENT_SESSION, lines: int = 40) -> str:
    """Lowercased tail of the agent tmux pane. "" on any failure."""
    try:
        out = _sp.run(["tmux", "capture-pane", "-t", session, "-p",
                       "-S", str(-lines)],
                      capture_output=True, text=True, timeout=4)
        return (out.stdout or "").lower() if out.returncode == 0 else ""
    except Exception:                                   # noqa: BLE001
        return ""


def _agent_alive(session: str = _AGENT_SESSION) -> bool:
    """True only when tmux exists *and* its foreground pane is not dead."""
    try:
        from . import tmux_safe
        return tmux_safe.pane_alive(session)
    except Exception:                                   # noqa: BLE001
        return False


def _dead_agent_state() -> dict:
    from .db import SessionLocal
    from .models import Setting
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(Setting.key == _AGENT_DEAD_KEY).first()
        return (dict(row.value) if row and isinstance(row.value, dict) else {})
    finally:
        db.close()


def _dead_agent_save(value: dict | None) -> None:
    from .db import SessionLocal
    from .models import Setting
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(Setting.key == _AGENT_DEAD_KEY).first()
        if value is None:
            if row is not None:
                db.delete(row)
                db.commit()
            return
        if row is None:
            db.add(Setting(key=_AGENT_DEAD_KEY, value=value))
        else:
            row.value = value
        db.commit()
    finally:
        db.close()


def _dead_restart_due(state: dict, now: float,
                      grace: float = _AGENT_DEAD_GRACE_SEC) -> bool:
    """Bound restart frequency without ever permanently giving up."""
    missing_at = float(state.get("missing_at", now))
    if now - missing_at < grace:
        return False
    attempts = max(0, int(state.get("attempts", 0)))
    last = float(state.get("last_attempt", 0.0))
    backoff = min(_AGENT_RESTART_MAX_BACKOFF_SEC,
                  15 * (2 ** min(attempts, 5)))
    return not last or now - last >= backoff


def _supervise_dead_research_agent(*, alive: bool, halted: bool,
                                   paused: bool, concluding: bool) -> bool:
    """Recover an expected research REPL whose process/session vanished.

    Returns True when the dead-agent path owns this tick, so the idle-prompt
    watchdog does not also act. Retries use capped backoff forever: a provider
    outage is observable and rate-limited, but can never silently strand the
    orchestrator in ``planning``.
    """
    from . import lifecycle, realrun
    expected = realrun.expected()
    if not expected or halted or paused or concluding:
        _dead_agent_save(None)
        return False
    if alive:
        state = _dead_agent_state()
        if state:
            _dead_agent_save(None)
            lifecycle.set_health(lifecycle.HEALTHY,
                                 "research agent is running")
            lifecycle.emit_event(
                "agent_auto_recovered",
                "Research agent is running after automatic recovery.",
                severity="info", actor="supervisor")
        return False

    state = _dead_agent_state()
    now = _dt.datetime.now(_dt.timezone.utc).timestamp()
    if not state:
        state = {"missing_at": now, "attempts": 0, "last_attempt": 0.0}
        _dead_agent_save(state)
        lifecycle.set_health(lifecycle.RECOVERING,
                             "research agent exited; automatic restart pending")
        lifecycle.emit_event(
            "agent_exit_detected",
            "Research agent exited unexpectedly; automatic recovery started.",
            severity="warning", actor="supervisor")
        return True
    if not _dead_restart_due(state, now):
        return True

    state["attempts"] = int(state.get("attempts", 0)) + 1
    state["last_attempt"] = now
    _dead_agent_save(state)
    lifecycle.set_health(
        lifecycle.RECOVERING,
        f"restarting research agent (attempt {state['attempts']})")
    try:
        from .agent_watcher import _restart_session
        # agent_watcher may have recovered the same missing session while this
        # supervisor tick was waiting. The shared restart transaction rechecks
        # under its lock and never kills a newly healthy replacement.
        ok = _restart_session("agent", resume=True, only_if_missing=True)
    except Exception as e:                              # noqa: BLE001
        ok = False
        from .safe_errors import describe
        print(f"[supervisor] research-agent restart error: {describe(e)}",
              flush=True)
    lifecycle.emit_event(
        "agent_restart_started" if ok else "agent_restart_failed",
        ("Automatically restarted the research agent from persisted state."
         if ok else
         "Research-agent restart failed; supervisor will retry automatically."),
        severity="warning" if ok else "error", actor="supervisor")
    return True


def _agent_busy(pane_low: str) -> bool:
    if any(m in pane_low for m in _AGENT_BUSY_MARKERS):
        return True
    return bool(_AGENT_SPINNER_RE.search(pane_low))


def _agent_boot_screen(pane_low: str) -> bool:
    return any(m in pane_low for m in _AGENT_BOOT_MARKERS)


def _agent_has_draft(pane_low: str) -> bool:
    """True if the agent has typed a non-trivial next-step into the prompt (a
    line beginning with a Claude (❯) or Codex (›) marker with real content).
    We must NOT nudge then — clearing its own plan with C-u is exactly the
    counterproductive thrash we saw (148 nudges wiping an in-progress plan)."""
    for ln in pane_low.splitlines():
        t = ln.strip()
        # Codex renders its generic placeholder as literal capture-pane text,
        # unlike a browser placeholder. It is not a user draft and must not
        # suppress autonomous recovery.
        placeholder = t.lower() == "› ask codex to do anything"
        if (t.startswith(("❯ ", "› ")) and len(t) > 8
                and 'try "' not in t and not placeholder):
            return True
    return False


def _agent_idle_prompt(pane_low: str) -> bool:
    """A live REPL waiting for input shows the prompt box + the "bypass
    permissions on" footer (or a bare Claude/Codex prompt)."""
    if not pane_low:
        return False
    # A parked REPL shows EITHER the plain prompt (bypass-permissions footer /
    # bare ❯) OR an interactive selection menu ("enter to select · ↑/↓ to
    # navigate · esc to cancel") where the agent is waiting for a human to pick
    # an option. Both mean "idle, waiting on a human that isn't there".
    return ("shift+tab to cycle" in pane_low       # idle footer, any mode
            or "bypass permissions on" in pane_low
            or "auto mode on" in pane_low
            or "enter to select" in pane_low
            or "\n❯ " in pane_low
            or "\n› " in pane_low
            or pane_low.rstrip().endswith(("❯", "›")))


def _agent_pending_turn(pane_low: str) -> bool:
    """Codex accepted input but is not rendering an active spinner.

    This footer means new input would be queued, so it is not an idle prompt.
    A provider/network hang can leave it unchanged forever; idle detection
    therefore cannot recover it without separate progress tracking.
    """
    return "tab to queue message" in pane_low


def _should_nudge_idle_agent(disable_bg: bool, alive: bool, halted: bool,
                             paused: bool, concluding: bool,
                             boot_screen: bool, busy: bool, idle_prompt: bool,
                             idle_age: float, nudge_age: float, strikes: int,
                             has_draft: bool = False,
                             grace: float = _AGENT_IDLE_GRACE_SEC,
                             cooldown: float = _AGENT_IDLE_COOLDOWN_SEC,
                             max_strikes: int = _AGENT_IDLE_MAX_STRIKES) -> str:
    """Pure, testable decision. Returns one of:
      - "skip"       — a guard says do nothing right now (paused/halted/boot/…)
      - "reset"      — the agent is working; clear idle tracking + strikes
      - "wait"       — parked, but not long enough / cooling down
      - "nudge"      — parked past grace + cooldown, under the strike cap -> nudge
      - "hard_stall" — nudged max_strikes times with no progress -> get a human
    """
    if disable_bg or not alive or halted or paused or concluding or boot_screen:
        return "skip"
    if busy or not idle_prompt or has_draft:
        return "reset"
    if idle_age < grace:
        return "wait"
    if strikes >= max_strikes:
        return "hard_stall"
    if nudge_age < cooldown:
        return "wait"
    return "nudge"


def _agent_idle_state() -> dict:
    from .db import SessionLocal
    from .models import Setting
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(Setting.key == _AGENT_IDLE_KEY).first()
        if row and isinstance(row.value, dict):
            return dict(row.value)
        return {}
    finally:
        db.close()


def _agent_idle_save(v: dict | None) -> None:
    from .db import SessionLocal
    from .models import Setting
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(Setting.key == _AGENT_IDLE_KEY).first()
        if v is None:
            if row is not None:
                db.delete(row)
                db.commit()
            return
        if row is None:
            db.add(Setting(key=_AGENT_IDLE_KEY, value=v))
        else:
            row.value = v
        db.commit()
    finally:
        db.close()


def _send_agent_nudge(text: str = _AGENT_NUDGE,
                      session: str = _AGENT_SESSION) -> bool:
    """Clear any half-typed draft in the prompt (C-u), then type + submit the
    nudge. Best-effort — returns False on any tmux failure."""
    try:
        import time as _t
        # Escape cancels any open interactive selection menu (the agent's
        # "enter to select · esc to cancel" prompt) so we land back on the text
        # prompt; harmless at a plain prompt. Then C-u clears any half-typed
        # draft, and we type + submit the nudge.
        _sp.run(["tmux", "send-keys", "-t", session, "Escape"],
                capture_output=True, timeout=4)
        _t.sleep(0.3)
        _sp.run(["tmux", "send-keys", "-t", session, "C-u"],
                capture_output=True, timeout=4)
        _sp.run(["tmux", "send-keys", "-t", session, "-l", text],
                capture_output=True, timeout=4)
        _t.sleep(0.2)
        submitted = _sp.run(["tmux", "send-keys", "-t", session, "Enter"],
                            capture_output=True, timeout=4)
        if submitted.returncode != 0:
            return False

        # tmux occasionally delivers the literal text but loses the following
        # Enter while the TUI is repainting.  That leaves an apparently alive
        # agent parked forever with our nudge in its input box.  Verify the
        # visible state after submission and retry Enter exactly once only
        # when the distinctive nudge marker is still an editable prompt.
        _t.sleep(1.0)
        pane = _sp.run(["tmux", "capture-pane", "-t", session, "-p",
                        "-S", "-40"], capture_output=True, timeout=4)
        pane_low = (pane.stdout or b"").decode(
            "utf-8", errors="ignore").lower()
        marker = "[autonomy - no human is watching this session]"
        if (pane.returncode == 0 and marker in pane_low
                and not _agent_busy(pane_low)
                and (_agent_has_draft(pane_low)
                     or _agent_idle_prompt(pane_low))):
            retried = _sp.run(["tmux", "send-keys", "-t", session, "Enter"],
                              capture_output=True, timeout=4)
            if retried.returncode != 0:
                return False
        return True
    except Exception:                                   # noqa: BLE001
        return False


def _supervise_research_agent() -> None:
    """Un-park a research agent that is ALIVE but idling at its prompt while
    research is supposed to be running. Deterministic, LOCAL + FAST (tmux +
    SQLite only). Mirrors paper-mode's refeed_if_idle with a 3-strike breaker."""
    from . import lifecycle, notify

    disable_bg = bool(_os.environ.get("ARUI_DISABLE_BG"))
    alive = _agent_alive()
    try:
        halted, _r = notify.research_halted()
    except Exception:                                   # noqa: BLE001
        halted = False
    try:
        paused = notify.research_paused()
    except Exception:                                   # noqa: BLE001
        paused = False
    # Legit "the agent is waiting on the council, not on a human" states.
    concluding = False
    try:
        from . import council
        cs = (council.conclusion_state() or {}).get("status", "none")
        concluding = cs in ("pending", "approved")
    except Exception:                                   # noqa: BLE001
        concluding = False

    if _supervise_dead_research_agent(
            alive=alive, halted=halted, paused=paused,
            concluding=concluding):
        return

    pane_low = _agent_pane_low() if alive else ""
    busy = _agent_busy(pane_low)
    boot_screen = _agent_boot_screen(pane_low)
    idle_prompt = _agent_idle_prompt(pane_low)
    has_draft = _agent_has_draft(pane_low)

    state = _agent_idle_state()
    now = _dt.datetime.now(_dt.timezone.utc).timestamp()

    def _age(key: str) -> float:
        iso = state.get(key)
        if not iso:
            return 1e9
        try:
            return max(0.0, now - _dt.datetime.fromisoformat(iso).timestamp())
        except Exception:                               # noqa: BLE001
            return 1e9

    # A Codex pending-turn footer without a live spinner is neither "busy" nor
    # "idle" in the ordinary TUI contract. Track exact visible-pane movement.
    # If it remains byte-identical for 20 minutes, cancel the wedged provider
    # turn and refeed through the verified nudge path. Any output change resets
    # the clock, preserving arbitrarily long but visibly progressing work.
    pending = _agent_pending_turn(pane_low) and not busy
    if (not disable_bg and alive and not halted and not paused and
            not concluding and not boot_screen and pending):
        fingerprint = _hashlib.sha256(pane_low.encode()).hexdigest()
        if state.get("pending_fingerprint") != fingerprint:
            _agent_idle_save({"pending_fingerprint": fingerprint,
                              "pending_since": _agent_iso(),
                              "strikes": int(state.get("strikes", 0))})
            return
        frozen_age = _age("pending_since")
        if frozen_age < _AGENT_FROZEN_PENDING_SEC:
            return
        try:
            _sp.run(["tmux", "send-keys", "-t", _AGENT_SESSION, "Escape"],
                    capture_output=True, timeout=4)
        except Exception:                               # noqa: BLE001
            pass
        ok = _send_agent_nudge()
        _agent_idle_save({"last_nudge": _agent_iso(), "strikes": 1})
        lifecycle.emit_event(
            "agent_frozen_turn_recovered",
            (f"Research-agent provider turn showed no visible progress for "
             f"{int(frozen_age)}s; cancelled it and submitted a fresh "
             "autonomy directive."),
            severity="warning", actor="supervisor")
        if not ok:
            lifecycle.emit_event("supervisor_error",
                                 "frozen-turn recovery send failed",
                                 severity="warning")
        return

    # First time we see the agent parked, idle_since is unset -> treat idle_age
    # as 0 (clock just started) so we set idle_since and WAIT one grace window
    # before the first nudge, instead of firing immediately. last_nudge keeps
    # the 1e9 "never nudged" default so the first nudge isn't cooldown-blocked.
    idle_age = _age("idle_since") if state.get("idle_since") else 0.0
    nudge_age = _age("last_nudge")
    strikes = int(state.get("strikes", 0))

    decision = _should_nudge_idle_agent(
        disable_bg, alive, halted, paused, concluding, boot_screen, busy,
        idle_prompt, idle_age, nudge_age, strikes, has_draft=has_draft)

    if decision in ("skip", "reset"):
        # Agent is working (reset) or a guard is active (skip) -> forget any
        # idle tracking so the next genuine park starts fresh.
        if state:
            was_escalated = bool(state.get("escalated"))
            _agent_idle_save(None)
            # Self-heal: if WE had hard-stalled the agent and it is now making
            # progress again, clear the stall so the dashboard doesn't sit red
            # forever. Only recover a stall WE set (match the blocker text) so
            # we never clobber another subsystem's HARD_STALLED.
            if decision == "reset" and was_escalated:
                try:
                    st = lifecycle.status()
                    if (st.get("health") == lifecycle.HARD_STALLED
                            and "research agent parked at its prompt"
                            in (st.get("blocker_reason") or "")):
                        lifecycle.set_health(
                            lifecycle.HEALTHY,
                            "research agent resumed work after auto-continue")
                        lifecycle.emit_event(
                            "agent_auto_recovered",
                            "Research agent resumed making progress on its own "
                            "— cleared the earlier hard-stall.",
                            severity="info", actor="supervisor")
                except Exception:                           # noqa: BLE001
                    pass
        return

    if decision == "wait":
        if not state.get("idle_since"):
            _agent_idle_save({"idle_since": _agent_iso(),
                              "last_nudge": state.get("last_nudge"),
                              "strikes": strikes})
        return

    if decision == "nudge":
        ok = _send_agent_nudge()
        new_strikes = strikes + 1
        _agent_idle_save({"idle_since": state.get("idle_since") or _agent_iso(),
                          "last_nudge": _agent_iso(),
                          "strikes": new_strikes})
        lifecycle.emit_event(
            "agent_auto_continue",
            (f"Research agent was idle at its prompt for {int(idle_age)}s "
             f"(waiting for a human that isn't there) — auto-continued it "
             f"(nudge {new_strikes}/{_AGENT_IDLE_MAX_STRIKES}): told it to "
             f"launch the next on-mandate experiment or POST a conclusion."),
            severity="info", actor="supervisor")
        if not ok:
            lifecycle.emit_event("supervisor_error",
                                 "research-agent auto-continue send failed",
                                 severity="warning")
        return

    if decision == "hard_stall":
        if not state.get("escalated"):
            lifecycle.set_health(
                lifecycle.HARD_STALLED,
                (f"research agent parked at its prompt through "
                 f"{_AGENT_IDLE_MAX_STRIKES} auto-continues without making "
                 f"progress — it needs a human directive or a decision to "
                 f"conclude"))
            lifecycle.emit_event(
                "agent_hard_stall",
                (f"Research agent ignored {_AGENT_IDLE_MAX_STRIKES} "
                 f"auto-continue nudges — escalating to you."),
                severity="critical", actor="supervisor")
            st = dict(state)
            st["escalated"] = True
            _agent_idle_save(st)
        return


# ── hung-run reaper ───────────────────────────────────────────────────────
# BACKSTOP for the primary wall-clock kill. monitor.py already applies
# kill_criteria (default "1 hour") to every run — BUT only to runs it can match
# to a LIVE tmux session (`if not alive: continue`). Agent runs record an empty
# `tmux_session`, so a run whose arun session name != its run id is invisible to
# that killer and can hang forever (we saw a dummy-mean SMOKE test run 158 min).
# This backstop acts on DB state ALONE — no session match required — so such a
# run still gets reaped. Cap sits ABOVE the 1h primary (2h default) so it never
# preempts a legitimately-configured long run; it only catches what the primary
# missed. Honours est_time_sec (3x). Tune via ARUI_RUN_TIMEOUT_SEC.

_RUN_TIMEOUT_SEC = int(_os.environ.get("ARUI_RUN_TIMEOUT_SEC", "7200"))  # 2h backstop
_RUN_OVERRUN_FACTOR = 3


def _run_cap_sec(est_time_sec, default: int = _RUN_TIMEOUT_SEC,
                 factor: int = _RUN_OVERRUN_FACTOR) -> int:
    """Wall-clock a RUNNING run is allowed before we reap it: the larger of the
    global default and (the agent's estimate x a headroom factor)."""
    return max(int(default), int(est_time_sec or 0) * int(factor))


def _should_reap_run(status: str, elapsed_sec, est_time_sec,
                     default: int = _RUN_TIMEOUT_SEC,
                     factor: int = _RUN_OVERRUN_FACTOR) -> bool:
    """Pure, testable: should this run be reaped for overrunning its cap?"""
    if status != "running":
        return False
    if elapsed_sec is None:
        return False
    return float(elapsed_sec) > _run_cap_sec(est_time_sec, default, factor)


def _run_elapsed_sec(started_at, now) -> float | None:
    if not started_at:
        return None
    try:
        st = _dt.datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
        if st.tzinfo is None:
            st = st.replace(tzinfo=_dt.timezone.utc)
        return max(0.0, (now - st).total_seconds())
    except Exception:                                   # noqa: BLE001
        return None


def _supervise_stuck_runs() -> None:
    """Kill + mark-crashed any RUNNING run that has overrun its wall-clock cap.
    Deterministic, LOCAL + FAST (SQLite + tmux only)."""
    from .db import SessionLocal
    from .models import Run
    from . import tmux_safe, lifecycle
    from sqlalchemy.orm.attributes import flag_modified

    now = _dt.datetime.now(_dt.timezone.utc)
    db = SessionLocal()
    try:
        running = db.query(Run).filter(Run.status == "running").all()
        for r in running:
            elapsed = _run_elapsed_sec(r.started_at, now)
            if not _should_reap_run(r.status, elapsed, r.est_time_sec):
                continue
            cap = _run_cap_sec(r.est_time_sec)
            # Best-effort kill: arun names the session after the run id/name.
            for cand in (r.tmux_session, r.run_name, r.id):
                if cand and tmux_safe.valid_name(cand):
                    try:
                        tmux_safe.kill_session(cand)
                    except Exception:                   # noqa: BLE001
                        pass
            r.status = "crashed"
            r.ended_at = now.isoformat()
            cfg = dict(r.config) if isinstance(r.config, dict) else {}
            cfg["killed_by"] = "supervisor:timeout"
            cfg["killed_reason"] = (
                f"exceeded {cap}s wall-clock cap ({int(elapsed)}s elapsed)")
            r.config = cfg
            flag_modified(r, "config")
            db.commit()
            lifecycle.emit_event(
                "run_reaped",
                (f"Killed hung run '{r.run_name}' — ran {int(elapsed // 60)}m, "
                 f"over the {cap // 60}m cap. Marked crashed so the loop moves "
                 f"on."),
                severity="warning", actor="supervisor")
    finally:
        db.close()
