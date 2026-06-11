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
