"""Issue detectors — pure functions ``DB → Issue | None``.

Each detector reads ground-truth facts (DB rows, GPU rows, metric
timestamps) and returns an Issue when something is wrong. ``service``
calls them all and aggregates.

Detectors live here (not in ``service``) so they're independently
testable and so adding a new detector is just a new function + a
single line in ``service.DETECTORS``.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy.orm import Session

from ..models import Event, Gpu, Idea, Run, Setting
from .schema import Issue, SEV_CRITICAL, SEV_WARNING, SEV_INFO


def _iso(seconds_ago: float = 0) -> str:
    return (dt.datetime.now(dt.timezone.utc)
            - dt.timedelta(seconds=seconds_ago)).isoformat()


def _age_seconds(iso: str) -> Optional[float]:
    if not iso:
        return None
    try:
        return (dt.datetime.now(dt.timezone.utc)
                - dt.datetime.fromisoformat(iso)).total_seconds()
    except Exception:                                       # noqa: BLE001
        return None


# ─────────────────────────── detectors ────────────────────────────────


def idle_gpus(db: Session, *, phase: str,
              idle_after_sec: int = 600) -> Optional[Issue]:
    """All GPUs idle for >= ``idle_after_sec`` AND the agent's phase
    suggests work should be happening.

    Persists ``health.idle_since`` (shared with the email-rate-limiter
    in ``pi``) so we don't drift from the email's clock.
    """
    gpus = db.query(Gpu).all()
    if not gpus:
        return None
    working = [g for g in gpus
               if (g.util_pct or 0) >= 5 or (g.vram_used_mb or 0) >= 1024]
    if working:
        # Clear the idle window if it was open.
        row = (db.query(Setting)
               .filter(Setting.key == "health.idle_since").first())
        if row is not None:
            db.delete(row)
            db.commit()
        return None
    # All idle. Track when it started.
    row = (db.query(Setting)
           .filter(Setting.key == "health.idle_since").first())
    if row is None:
        db.add(Setting(key="health.idle_since",
                        value={"since": _iso()}))
        db.commit()
        return None
    since_iso = (row.value or {}).get("since") or _iso()
    age = _age_seconds(since_iso) or 0
    if age < idle_after_sec:
        return None
    # Only an issue if the agent's reported phase is one where GPUs
    # SHOULD be doing work. During council_review / concluding /
    # idle_waiting_direction etc., idle is expected.
    if phase not in ("planning", "launching_runs", "watching_runs"):
        return None
    mins = int(age // 60)
    sev = SEV_CRITICAL if age >= 1800 else SEV_WARNING
    return Issue(
        code="idle_gpus",
        severity=sev,
        summary=f"{len(gpus)} GPU(s) idle for {mins} min in phase '{phase}'",
        evidence={
            "gpu_count": len(gpus),
            "idle_minutes": mins,
            "since": since_iso,
            "phase": phase,
        },
        since=since_iso,
        actions=[
            {"label": "Open agent terminal", "kind": "scroll_to_rail"},
            {"label": "Pause research",
             "method": "POST", "href": "/api/research/pause"},
        ],
    )


def no_metric_flow(db: Session, *,
                   timeout_sec: int = 600) -> list[Issue]:
    """RUNNING runs whose newest metric point is older than
    ``timeout_sec`` (default 10 min — overridden per-project by the
    watchdog config landed in PR 4).

    Returns ALL matches, not just one, so the modal can list every
    stuck run with its own kill button.
    """
    from .. import metrics
    out: list[Issue] = []
    running_runs = db.query(Run).filter(Run.status == "running").all()
    if not running_runs:
        return out
    for r in running_runs:
        try:
            last = metrics.last_activity(r.id)
        except Exception:                                   # noqa: BLE001
            last = None
        # Use started_at as the floor — a run that JUST started shouldn't
        # be flagged for not logging yet.
        baseline_iso = r.started_at or r.created_at or _iso()
        baseline_age = _age_seconds(baseline_iso) or 0
        if baseline_age < timeout_sec:
            continue
        if last is None:
            last_age = baseline_age   # never logged anything
            last_human = "(never logged)"
        else:
            last_age = (dt.datetime.now(dt.timezone.utc).timestamp()
                        - float(last))
            last_human = f"{int(last_age // 60)}m ago"
        if last_age < timeout_sec:
            continue
        sev = SEV_CRITICAL if last_age >= 2 * timeout_sec else SEV_WARNING
        out.append(Issue(
            code="no_metric_flow",
            severity=sev,
            summary=(f"Run {r.run_name} hasn't logged a metric in "
                     f"{int(last_age // 60)} min (threshold "
                     f"{timeout_sec // 60} min)"),
            evidence={
                "run_id": r.id,
                "run_name": r.run_name,
                "last_metric_age_sec": int(last_age),
                "last_metric_age_human": last_human,
                "threshold_sec": timeout_sec,
                "started_at": r.started_at,
            },
            since=r.started_at or "",
            actions=[
                {"label": "View run", "kind": "open_drawer",
                 "run_id": r.id},
                {"label": "Kill run", "method": "POST",
                 "href": f"/api/runs/{r.id}/kill"},
            ],
        ))
    return out


def directives_ignored(db: Session, *,
                       streak_threshold: int = 3) -> Optional[Issue]:
    """Top open directive has been carried over ``streak_threshold``
    consecutive strategic reviews without implementation. This used to
    trigger the dreaded ESCALATION_HALT; in the new design it's just
    a warning issue with a "view council history" action.
    """
    # Streak count is computed directly from Event rows now (PR 6 of
    # state-control rewrite removed stuck_detector). We look for the
    # most recent ``strategic_review`` Events tagged with a directive
    # id and count consecutive "implemented=NO" entries.
    try:
        from ..models import Event
        rows = (db.query(Event)
                .filter(Event.type == "strategic_review")
                .order_by(Event.created_at.desc())
                .limit(20).all())
        streak = 0
        top_sig = ""
        for r in rows:
            msg = (r.message or "")
            # Best-effort parse — councils write structured strings here.
            # Bail if we can't even identify the directive id.
            if "implemented=NO" not in msg.lower() and \
                    "unimplemented" not in msg.lower():
                break
            # First row sets the signature; mismatch breaks the streak.
            sig = ""
            for token in msg.split():
                if token.startswith("d-") and len(token) >= 6:
                    sig = token.rstrip(",.;:")
                    break
            if not top_sig:
                top_sig = sig
            elif sig and sig != top_sig:
                break
            streak += 1
    except Exception:                                       # noqa: BLE001
        return None
    if streak < streak_threshold:
        return None
    sev = SEV_WARNING if streak < 5 else SEV_CRITICAL
    return Issue(
        code="directives_ignored",
        severity=sev,
        summary=(f"Top directive carried over {streak} consecutive "
                 "reviews without implementation"),
        evidence={"streak": streak, "top_signature": top_sig},
        since="",
        actions=[
            {"label": "Open council history", "kind": "open_council"},
        ],
    )


def phase_stale(db: Session, *, phase_at: str,
                stale_after_sec: int = 30 * 60) -> Optional[Issue]:
    """The agent hasn't reported a new phase in ``stale_after_sec``.
    Either the agent crashed, or the loop is actually stuck in one
    phase too long. Either way, the operator wants to know.
    """
    age = _age_seconds(phase_at)
    if age is None or age < stale_after_sec:
        return None
    mins = int(age // 60)
    return Issue(
        code="phase_stale",
        severity=SEV_WARNING,
        summary=(f"Agent hasn't reported a new phase in {mins} min — "
                 "may have crashed or be stuck"),
        evidence={"age_minutes": mins, "phase_at": phase_at},
        since=phase_at,
        actions=[
            {"label": "Restart agent",
             "method": "POST", "href": "/api/agent/restart"},
        ],
    )
