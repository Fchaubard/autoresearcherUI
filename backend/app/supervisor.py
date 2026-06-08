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
