"""Lifecycle — the single source of truth for "where is the research and is it
healthy", plus worker leases so a dead / orphaned background worker is
detectable instantly.

This module is PURELY ADDITIVE. It does not change how the research agent, the
council, or the runs behave. It only OBSERVES the pipeline and gives the
supervisor (the watchdog in monitor.py) the primitives to keep things unblocked
and to explain the state in the activity feed + email digests — so "idle" is
never a mystery and a timeout / crash / orphaned worker never silently wedges
the whole research.

Two primitives:
  • status  — one Setting row (`lifecycle_status`): phase, health, blocker
    reason, when the phase started, and per-key remediation counters with a
    3-strike circuit breaker → HARD_STALLED (needs a human).
  • leases  — one Setting row (`worker_leases`): a background worker records a
    lease on start (pid + timestamps) and clears it on finish. A lease whose
    process is gone (backend restarted) or whose heartbeat is stale = an
    orphaned worker the supervisor can re-trigger.
"""
from __future__ import annotations

import datetime as dt
import os

from .db import SessionLocal
from .models import Event, Setting

_STATUS_KEY = "lifecycle_status"
_LEASE_KEY = "worker_leases"

# health values
HEALTHY = "HEALTHY"
RECOVERING = "RECOVERING"
HARD_STALLED = "HARD_STALLED"
MAX_REMEDIATION = 3

# canonical phases (extend as the supervisor learns to watch more of the
# pipeline). Strings, not an Enum, so they round-trip cleanly through JSON.
PHASE_IDLE = "idle"
PHASE_SCOPING = "scoping"
PHASE_SCAFFOLDING = "res_scaffolding"
PHASE_CODE_BLESS = "res_code_bless"
PHASE_RUNNING = "res_running"
PHASE_COUNCIL_REVIEW = "res_council_review"
PHASE_CONCLUSION_REVIEW = "res_conclusion_review"
PHASE_PAPER = "paper"
PHASE_DONE = "done"

_PHASE_LABELS = {
    PHASE_IDLE: "Idle",
    PHASE_SCOPING: "Scoping (literature review + plan)",
    PHASE_SCAFFOLDING: "Scaffolding the research code",
    PHASE_CODE_BLESS: "Council code review (bless gate)",
    PHASE_RUNNING: "Running experiments",
    PHASE_COUNCIL_REVIEW: "Council reviewing experiments",
    PHASE_CONCLUSION_REVIEW: "Council reviewing the conclusion",
    PHASE_PAPER: "Writing the paper",
    PHASE_DONE: "Done",
}


def _iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# ── tiny Setting helpers (best-effort, never raise into callers) ────────────
def _get(key: str, default: dict) -> dict:
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(Setting.key == key).first()
        if row and isinstance(row.value, dict):
            return dict(row.value)
        return dict(default)
    finally:
        db.close()


def _set(key: str, value: dict) -> None:
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(Setting.key == key).first()
        if row:
            row.value = value
        else:
            db.add(Setting(key=key, value=value))
        db.commit()
    finally:
        db.close()


# ── status ──────────────────────────────────────────────────────────────────
def status() -> dict:
    return _get(_STATUS_KEY, {
        "phase": PHASE_IDLE, "health": HEALTHY, "blocker_reason": "",
        "phase_started_at": "", "remediation": {}})


def set_phase(phase: str, reason: str = "") -> dict:
    """Move to `phase`. Only a REAL transition resets the per-key remediation
    counters (a new phase gets a fresh 3 strikes) and marks HEALTHY — so the
    supervisor can call this every tick without clobbering a RECOVERING /
    HARD_STALLED health it set for the current phase."""
    st = status()
    if st.get("phase") != phase:
        st["phase"] = phase
        st["phase_started_at"] = _iso()
        st["remediation"] = {}
        st["health"] = HEALTHY
        st["blocker_reason"] = reason or ""
    st["updated_at"] = _iso()
    _set(_STATUS_KEY, st)
    return st


def set_health(health: str, blocker_reason: str = "") -> dict:
    st = status()
    st["health"] = health
    st["blocker_reason"] = blocker_reason or ""
    st["updated_at"] = _iso()
    _set(_STATUS_KEY, st)
    return st


def remediation_count(key: str) -> int:
    return int((status().get("remediation") or {}).get(key, 0))


def record_remediation(key: str, reason: str) -> dict:
    """Bump the remediation counter for `key`, mark RECOVERING, emit an Event.
    On the 3rd strike, flip to HARD_STALLED (a human is needed) instead of
    retrying forever — the guardrail against an infinite remediation loop."""
    st = status()
    rem = dict(st.get("remediation") or {})
    rem[key] = int(rem.get(key, 0)) + 1
    st["remediation"] = rem
    n = rem[key]
    if n >= MAX_REMEDIATION:
        st["health"] = HARD_STALLED
        st["blocker_reason"] = f"{reason} — failed {n}x, needs you"
        emit_event("hard_stalled", st["blocker_reason"], severity="critical")
    else:
        st["health"] = RECOVERING
        st["blocker_reason"] = f"{reason} (attempt {n}/{MAX_REMEDIATION})"
        emit_event("remediation", st["blocker_reason"], severity="warning")
    st["updated_at"] = _iso()
    _set(_STATUS_KEY, st)
    return st


def summary_line() -> str:
    """One human-readable line for the email digest + dashboard: what phase
    we're in and (if not healthy) WHY we're stuck."""
    st = status()
    h = st.get("health", HEALTHY)
    label = _PHASE_LABELS.get(st.get("phase", ""), st.get("phase") or "Idle")
    br = st.get("blocker_reason") or ""
    if h == RECOVERING:
        return f"RECOVERING — {br}"
    if h == HARD_STALLED:
        return f"HARD STALLED — {br}"
    age = _age_str(st.get("phase_started_at"))
    tail = f" ({age})" if age else ""
    return label + (f" — {br}" if br else "") + tail


# ── worker leases ────────────────────────────────────────────────────────────
def lease_acquire(name: str) -> None:
    leases = _get(_LEASE_KEY, {})
    leases[name] = {"started_at": _iso(), "heartbeat_at": _iso(),
                    "pid": os.getpid()}
    _set(_LEASE_KEY, leases)


def lease_heartbeat(name: str) -> None:
    leases = _get(_LEASE_KEY, {})
    if name in leases:
        leases[name]["heartbeat_at"] = _iso()
        _set(_LEASE_KEY, leases)


def lease_release(name: str) -> None:
    leases = _get(_LEASE_KEY, {})
    if name in leases:
        del leases[name]
        _set(_LEASE_KEY, leases)


def lease_get(name: str):
    return (_get(_LEASE_KEY, {}) or {}).get(name)


def lease_alive(name: str, max_age_sec: float) -> bool:
    """True iff a worker holds the lease, its process still exists, AND its
    heartbeat (or start) is within max_age_sec. After a backend restart the
    old pid is gone → not alive → the supervisor knows the worker was orphaned
    and can re-trigger it."""
    le = lease_get(name)
    if not le:
        return False
    pid = le.get("pid")
    if pid and not _pid_alive(int(pid)):
        return False
    ts = le.get("heartbeat_at") or le.get("started_at")
    return _age(ts) < max_age_sec


# ── small utils ──────────────────────────────────────────────────────────────
def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _age(iso: str) -> float:
    try:
        t = dt.datetime.fromisoformat(iso)
        if t.tzinfo is None:
            t = t.replace(tzinfo=dt.timezone.utc)
        return (_now() - t).total_seconds()
    except Exception:
        return 1e9


def _age_str(iso: str) -> str:
    s = _age(iso)
    if s >= 1e8:
        return ""
    m = int(s // 60)
    return f"{m}m ago" if m else f"{int(s)}s ago"


def emit_event(ev_type: str, message: str, severity: str = "info",
               actor: str = "supervisor") -> None:
    """Best-effort Event insert for the activity feed. Never raises."""
    try:
        db = SessionLocal()
        try:
            db.add(Event(id="ev-" + os.urandom(4).hex(), type=ev_type,
                         severity=severity, actor=actor,
                         message=(message or "")[:500], created_at=_iso()))
            db.commit()
        finally:
            db.close()
        try:
            from .bus import bus
            bus.publish("events", "event", {})
        except Exception:
            pass
    except Exception:
        pass
