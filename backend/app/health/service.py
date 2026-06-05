"""Health service — assembles a single HealthSnapshot from ground truth.

This is the source of truth for "is the loop OK?". The dashboard pill
reads the snapshot via GET /api/health, the modal renders the issues
list, and the PI nudge loop consumes the same snapshot when deciding
whether to message the agent. No more competing classifiers.
"""
from __future__ import annotations

import datetime as dt

from ..db import SessionLocal
from ..models import Run, Setting
from . import issues as detectors
from .schema import HealthSnapshot, Issue, Phase, SEV_INFO


def _iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


# Registry of detector callables. Each takes ``(db, **kwargs)`` and
# returns either an Issue, a list[Issue], or None. Adding a new
# detector is one import + one line here.
_SINGLE_DETECTORS = (
    ("idle_gpus", detectors.idle_gpus, "phase_aware_idle"),
    ("phase_stale", detectors.phase_stale, "phase_aware_stale"),
    ("directives_ignored", detectors.directives_ignored, "council"),
)
_MULTI_DETECTORS = (
    ("no_metric_flow", detectors.no_metric_flow),
)


def _read_phase(db) -> Phase:
    """Read the agent-reported phase from the ``orchestrator.phase``
    Setting row. Falls back to a DB-derived best guess when the agent
    hasn't reported (legacy projects)."""
    row = (db.query(Setting)
           .filter(Setting.key == "orchestrator.phase").first())
    if row and isinstance(row.value, dict) and row.value.get("phase"):
        return Phase(
            phase=row.value["phase"],
            at=row.value.get("at") or "",
            detail=row.value.get("detail") or {},
            fallback_used=False,
        )
    running = db.query(Run).filter(Run.status == "running").count()
    total = db.query(Run).count()
    if total == 0:
        ph = "bootstrap"
    elif running > 0:
        ph = "watching_runs"
    else:
        ph = "planning"
    return Phase(phase=ph, at="", detail={}, fallback_used=True)


def _read_watchdog_param(db, script: str, param: str, default):
    """Read a per-project watchdog tuning param.
    Until PR 4 (the watchdog package) writes these, returns ``default``.
    Settings key shape: ``watchdog.config = {script: {params: {...}}}``.
    """
    row = (db.query(Setting)
           .filter(Setting.key == "watchdog.config").first())
    if not row or not isinstance(row.value, dict):
        return default
    cfg = row.value.get(script) or {}
    params = cfg.get("params") or {}
    return params.get(param, default)


def compute() -> HealthSnapshot:
    """Build a fresh HealthSnapshot from the database.

    Never raises into the caller. Any detector that crashes is logged
    and skipped — one bad detector mustn't blank the whole modal.
    """
    db = SessionLocal()
    try:
        phase = _read_phase(db)
        all_issues: list[Issue] = []
        # Single-issue detectors
        for name, fn, kind in _SINGLE_DETECTORS:
            try:
                kwargs: dict = {}
                if kind == "phase_aware_idle":
                    kwargs["phase"] = phase.phase
                    kwargs["idle_after_sec"] = int(_read_watchdog_param(
                        db, "idle_gpus", "idle_after_sec", 600))
                elif kind == "phase_aware_stale":
                    kwargs["phase_at"] = phase.at
                    kwargs["stale_after_sec"] = int(_read_watchdog_param(
                        db, "phase_stale", "stale_after_sec", 30 * 60))
                got = fn(db, **kwargs)
                if got is not None:
                    all_issues.append(got)
            except Exception as e:                          # noqa: BLE001
                print(f"[health] detector {name} failed: {e}", flush=True)
        # Multi-issue detectors
        for name, fn in _MULTI_DETECTORS:
            try:
                kwargs: dict = {}
                if name == "no_metric_flow":
                    kwargs["timeout_sec"] = int(_read_watchdog_param(
                        db, "no_metric_flow", "timeout_sec", 600))
                got = fn(db, **kwargs)
                if got:
                    all_issues.extend(got)
            except Exception as e:                          # noqa: BLE001
                print(f"[health] detector {name} failed: {e}", flush=True)
        # Sort by severity desc, then by since asc (older issues first)
        all_issues.sort(
            key=lambda i: (-i.severity, i.since or ""))
        # Pill summary
        if all_issues:
            top = all_issues[0]
            summary = f"{phase.phase} — {top.summary}"
        else:
            summary = f"{phase.phase} — all systems nominal."
        facts = {
            "n_issues": len(all_issues),
            "phase_fallback_used": phase.fallback_used,
            "top_severity": (
                max(i.severity for i in all_issues)
                if all_issues else SEV_INFO),
        }
        return HealthSnapshot(phase=phase, summary=summary,
                              issues=all_issues, facts=facts)
    finally:
        db.close()


def tick() -> HealthSnapshot:
    """Recompute and persist. PR 6 will move the per-state-transition
    side effects (chat bubbles, emails) from stuck_detector to here."""
    snap = compute()
    db = SessionLocal()
    try:
        row = (db.query(Setting)
               .filter(Setting.key == "health.snapshot").first())
        payload = snap.as_dict()
        payload["at"] = _iso()
        if row is None:
            db.add(Setting(key="health.snapshot", value=payload))
        else:
            row.value = payload
        db.commit()
    except Exception as e:                                  # noqa: BLE001
        print(f"[health] persist failed: {e}", flush=True)
    finally:
        db.close()
    return snap
