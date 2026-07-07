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

_AGENT_SESSION = "agent"
_AGENT_IDLE_GRACE_SEC = 45          # parked-at-prompt this long before nudging
_AGENT_IDLE_COOLDOWN_SEC = 90       # min gap between nudges
_AGENT_IDLE_MAX_STRIKES = 3         # after N nudges with no progress -> human
_AGENT_IDLE_KEY = "research_agent_idle_watch"

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
_AGENT_SPINNER_RE = _re.compile(r"\(\d+\s*[ms]\b")
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
    "Re-read your mandate now: the project purpose, directives.jsonl, and "
    "ideas.md. Then do exactly ONE of these immediately: (a) pick the single "
    "best remaining on-mandate experiment and launch it, or (b) if you are "
    "confident the research question is fully answered, POST your conclusion "
    "to /api/research/conclude with your evidence. Decide and act - never wait "
    "for a human."
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
    try:
        return _sp.run(["tmux", "has-session", "-t", session],
                       capture_output=True, timeout=4).returncode == 0
    except Exception:                                   # noqa: BLE001
        return False


def _agent_busy(pane_low: str) -> bool:
    if any(m in pane_low for m in _AGENT_BUSY_MARKERS):
        return True
    return bool(_AGENT_SPINNER_RE.search(pane_low))


def _agent_boot_screen(pane_low: str) -> bool:
    return any(m in pane_low for m in _AGENT_BOOT_MARKERS)


def _agent_idle_prompt(pane_low: str) -> bool:
    """A live REPL waiting for input shows the prompt box + the "bypass
    permissions on" footer (or a bare ❯ prompt)."""
    if not pane_low:
        return False
    # A parked REPL shows EITHER the plain prompt (bypass-permissions footer /
    # bare ❯) OR an interactive selection menu ("enter to select · ↑/↓ to
    # navigate · esc to cancel") where the agent is waiting for a human to pick
    # an option. Both mean "idle, waiting on a human that isn't there".
    return ("bypass permissions on" in pane_low
            or "enter to select" in pane_low
            or "\n❯ " in pane_low
            or pane_low.rstrip().endswith("❯"))


def _should_nudge_idle_agent(disable_bg: bool, alive: bool, halted: bool,
                             paused: bool, concluding: bool,
                             boot_screen: bool, busy: bool, idle_prompt: bool,
                             idle_age: float, nudge_age: float, strikes: int,
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
    if busy or not idle_prompt:
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
        _sp.run(["tmux", "send-keys", "-t", session, "Enter"],
                capture_output=True, timeout=4)
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

    pane_low = _agent_pane_low() if alive else ""
    busy = _agent_busy(pane_low)
    boot_screen = _agent_boot_screen(pane_low)
    idle_prompt = _agent_idle_prompt(pane_low)

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

    # First time we see the agent parked, idle_since is unset -> treat idle_age
    # as 0 (clock just started) so we set idle_since and WAIT one grace window
    # before the first nudge, instead of firing immediately. last_nudge keeps
    # the 1e9 "never nudged" default so the first nudge isn't cooldown-blocked.
    idle_age = _age("idle_since") if state.get("idle_since") else 0.0
    nudge_age = _age("last_nudge")
    strikes = int(state.get("strikes", 0))

    decision = _should_nudge_idle_agent(
        disable_bg, alive, halted, paused, concluding, boot_screen, busy,
        idle_prompt, idle_age, nudge_age, strikes)

    if decision in ("skip", "reset"):
        # Agent is working (reset) or a guard is active (skip) -> forget any
        # idle tracking so the next genuine park starts fresh.
        if state:
            _agent_idle_save(None)
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
