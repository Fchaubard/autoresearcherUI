"""Paper-mode phase machine + status overview (2026-06-05 rebuild).

This is the bridge between Francois's mental model of the paper-writing
workflow and the UI. The Author Agent calls ``set_phase()`` at every
transition, the frontend polls ``get_status()`` every few seconds, and
both render the SAME shape the research-mode pill + modal already use
(phase, summary, issues, progress).

Phases in canonical order:

    paper.whittle_claims     reduce kept runs → tight set of claims
    paper.lit_review         find related work, file citation decisions
    paper.draft_v0           scaffold main.tex with TODO tables/figures
    paper.plan_ablations     derive the full ablation matrix
    paper.build_gantt        schedule the matrix against available GPUs
    paper.operator_review    WAIT for human approval — GPU gate
    paper.run_ablations      execute the approved matrix
    paper.reviewer_simulator internal pre-submission review pass
    paper.submission_ready   PDF + artifact bundle ready
    paper.error              author crashed; needs human attention

The phase value is reused from ``arui.phase()`` with a ``paper.`` prefix
so the research-mode health/service can co-exist (per GPT-5 review).
The "operator approval before burning GPUs" gate is persisted as
``paper.gate.plan`` and checked by ``paper_runner`` before transitioning
proposed → queued.
"""
from __future__ import annotations

import datetime as dt
import os
from typing import Any

from .db import SessionLocal
from .models import Event, PaperClaim, PaperCitation, PaperDecision, \
    PaperMeta, Run, Setting


# AUTOPILOT flow — no human gate. operator_review is removed: the author goes
# straight from build_gantt to run_ablations (runs auto-queue). reviewer_simulator
# is an ADVISORY pass, not a gate. The PI + council review each revision.
PAPER_PHASES = (
    "paper.whittle_claims",
    "paper.lit_review",
    "paper.draft_v0",
    "paper.plan_ablations",
    "paper.build_gantt",
    "paper.run_ablations",
    "paper.reviewer_simulator",
    "paper.submission_ready",
    "paper.error",
)

# Pill labels — what the operator sees on the header pill.
PHASE_LABELS = {
    "paper.whittle_claims":     "Whittling claims",
    "paper.lit_review":         "Literature review",
    "paper.draft_v0":           "Drafting v0",
    "paper.plan_ablations":     "Planning ablations",
    "paper.build_gantt":        "Building Gantt",
    "paper.run_ablations":      "Running ablations",
    "paper.reviewer_simulator": "Reviewer simulation",
    "paper.submission_ready":   "Submission ready",
    "paper.error":              "Error — needs attention",
}


def _iso(seconds_ago: float = 0) -> str:
    return (dt.datetime.now(dt.timezone.utc)
            - dt.timedelta(seconds=seconds_ago)).isoformat()


# ─────────────────────────── phase API ────────────────────────────────


def set_phase(phase: str, *, actor: str = "author",
              progress: dict | None = None,
              detail: dict | None = None) -> dict:
    """Mark the Author Agent's current phase. Persists the value to
    ``paper.phase`` and emits a ``phase_changed`` Event only on a real
    transition. The progress payload is shallow-merged into
    ``paper.progress`` for the UI status strip.
    """
    db = SessionLocal()
    try:
        row = (db.query(Setting)
               .filter(Setting.key == "paper.phase").first())
        prev_phase = ""
        if row and isinstance(row.value, dict):
            prev_phase = row.value.get("phase") or ""
        now = _iso()
        new_value = {"phase": phase, "at": now, "actor": actor,
                      "detail": detail or {}}
        if row is None:
            db.add(Setting(key="paper.phase", value=new_value))
        else:
            row.value = new_value
        # Merge progress into paper.progress (separate row so the
        # frontend can subscribe to either independently).
        if progress is not None and isinstance(progress, dict):
            prog_row = (db.query(Setting)
                        .filter(Setting.key == "paper.progress").first())
            if prog_row is None:
                db.add(Setting(key="paper.progress",
                                value=dict(progress)))
            elif isinstance(prog_row.value, dict):
                merged = dict(prog_row.value)
                merged.update(progress)
                prog_row.value = merged
            else:
                prog_row.value = dict(progress)
        if phase != prev_phase:
            ev_msg = (f"author: {prev_phase or '(none)'} → {phase}")[:280]
            db.add(Event(id="ev-" + os.urandom(4).hex(),
                         type="phase_changed", severity="info",
                         actor=actor, message=ev_msg, created_at=now))
        db.commit()
        return {"ok": True, "phase": phase, "at": now,
                "transitioned": phase != prev_phase}
    finally:
        db.close()


def get_phase(db=None) -> dict:
    """Read the persisted phase. Returns a dict with phase, at, actor,
    detail. If no Author Agent has reported yet, returns
    paper.whittle_claims as the default starting phase (UI shows
    "Whittling claims" instead of an empty pill)."""
    own_db = db is None
    if own_db:
        db = SessionLocal()
    try:
        row = (db.query(Setting)
               .filter(Setting.key == "paper.phase").first())
        if row and isinstance(row.value, dict) and row.value.get("phase"):
            return {
                "phase": row.value["phase"],
                "at": row.value.get("at") or "",
                "actor": row.value.get("actor") or "author",
                "detail": row.value.get("detail") or {},
                "fallback_used": False,
            }
        return {
            "phase": "paper.whittle_claims",
            "at": "",
            "actor": "system",
            "detail": {},
            "fallback_used": True,
        }
    finally:
        if own_db:
            db.close()


def get_progress(db=None) -> dict:
    """Read the paper.progress Setting row. Returns the default zero
    payload if missing so the UI can render counts without guarding."""
    own_db = db is None
    if own_db:
        db = SessionLocal()
    try:
        row = (db.query(Setting)
               .filter(Setting.key == "paper.progress").first())
        if row and isinstance(row.value, dict):
            return dict(row.value)
        return {
            "claims":    {"active": 0, "ready": 0, "killed": 0},
            "lit":       {"citations": 0, "approved": 0, "pending": 0},
            "draft":     {"sections": 0, "pdf_compiled": False},
            "ablations": {"proposed": 0, "queued": 0, "running": 0,
                          "done": 0, "kept": 0,
                          "est_hours": 0.0},
            "gantt":     {"runs": 0, "eta_hours": 0.0,
                          "deadline_days": None},
        }
    finally:
        if own_db:
            db.close()


# ─────────────────────────── gate API ─────────────────────────────────


def get_gate(db=None) -> dict:
    """Read the operator approval gate. Default: pending."""
    own_db = db is None
    if own_db:
        db = SessionLocal()
    try:
        row = (db.query(Setting)
               .filter(Setting.key == "paper.gate").first())
        if row and isinstance(row.value, dict):
            return dict(row.value)
        return {"plan": {"status": "pending", "requested_at": "",
                          "approved_at": None,
                          "note": ""}}
    finally:
        if own_db:
            db.close()


def request_plan_approval(note: str = "") -> dict:
    """Author Agent calls this when its ablation plan is ready."""
    db = SessionLocal()
    try:
        now = _iso()
        row = (db.query(Setting)
               .filter(Setting.key == "paper.gate").first())
        cur = dict(row.value) if row and isinstance(row.value, dict) \
            else {"plan": {}}
        cur["plan"] = {
            "status": "pending",
            "requested_at": now,
            "approved_at": None,
            "note": (note or "")[:600],
        }
        if row is None:
            db.add(Setting(key="paper.gate", value=cur))
        else:
            row.value = dict(cur)
        db.commit()
        return cur
    finally:
        db.close()


def approve_plan(by: str = "operator", note: str = "") -> dict:
    """Operator approves the ablation plan. Transitions any
    PaperRun.status from "proposed" to "queued" so paper_runner can
    pick them up."""
    db = SessionLocal()
    try:
        now = _iso()
        row = (db.query(Setting)
               .filter(Setting.key == "paper.gate").first())
        cur = dict(row.value) if row and isinstance(row.value, dict) \
            else {"plan": {}}
        plan = dict(cur.get("plan") or {})
        plan["status"] = "approved"
        plan["approved_at"] = now
        plan["by"] = by
        if note:
            plan["note"] = (note or "")[:600]
        cur["plan"] = plan
        if row is None:
            db.add(Setting(key="paper.gate", value=cur))
        else:
            # Reassign a NEW dict so SQLAlchemy detects the change —
            # in-place mutation of row.value isn't tracked by the JSON
            # type's default change-detection.
            row.value = dict(cur)
        # Transition proposed → queued so paper_runner picks up.
        n = 0
        for r in (db.query(Run)
                  .filter(Run.status == "proposed").all()):
            r.status = "queued"
            n += 1
        db.add(Event(id="ev-" + os.urandom(4).hex(),
                     type="paper_plan_approved", severity="info",
                     actor=by,
                     message=(f"Operator {by} approved ablation plan; "
                              f"{n} runs queued.")[:280],
                     created_at=now))
        db.commit()
        return {"ok": True, "queued_count": n, "gate": cur}
    finally:
        db.close()


def request_changes(by: str = "operator", note: str = "") -> dict:
    """Operator rejects the plan; author returns to plan_ablations."""
    db = SessionLocal()
    try:
        now = _iso()
        row = (db.query(Setting)
               .filter(Setting.key == "paper.gate").first())
        cur = dict(row.value) if row and isinstance(row.value, dict) \
            else {"plan": {}}
        cur["plan"] = {
            "status": "changes_requested",
            "requested_at": (cur.get("plan") or {}).get("requested_at")
                            or now,
            "approved_at": None,
            "by": by,
            "note": (note or "")[:600],
        }
        if row is None:
            db.add(Setting(key="paper.gate", value=cur))
        else:
            row.value = dict(cur)
        db.add(Event(id="ev-" + os.urandom(4).hex(),
                     type="paper_plan_changes_requested",
                     severity="warning", actor=by,
                     message=(f"Operator {by} requested plan changes: "
                              f"{note[:200]}")[:280],
                     created_at=now))
        db.commit()
        return {"ok": True, "gate": cur}
    finally:
        db.close()


def plan_approved(db=None) -> bool:
    """AUTOPILOT (operator: no human gating — let the paper rip). The ablation
    plan is always auto-approved so the author never waits on a human GPU gate;
    proposed runs flow straight to queued. The PI + council review each
    revision for quality/novelty instead of a human approving GPU spend."""
    return True


# ─────────────────────────── status overview ──────────────────────────


def _derive_progress_from_db(db) -> dict:
    """Compute progress counters from live DB state. Cheaper + more
    accurate than relying on the agent to push every change."""
    claims = db.query(PaperClaim).all()
    n_active = sum(1 for c in claims
                    if (c.status or "active") == "active")
    n_ready = sum(1 for c in claims
                  if (c.status or "") == "ready")
    n_killed = sum(1 for c in claims
                   if (c.status or "") == "killed")
    citations = db.query(PaperCitation).all()
    n_cit = len(citations)
    decisions = db.query(PaperDecision).all()
    n_cit_pending = sum(1 for d in decisions
                        if (d.kind or "") == "cite_paper"
                        and (d.status or "pending") == "pending")
    n_cit_approved = sum(1 for d in decisions
                         if (d.kind or "") == "cite_paper"
                         and (d.status or "") == "accepted")
    # Paper runs status breakdown.
    runs_by_status: dict[str, int] = {}
    for r in (db.query(Run)
              .filter((Run.context == "paper")
                       | (Run.paper_claim_id != None)).all()):  # noqa: E711
        runs_by_status[r.status] = runs_by_status.get(r.status, 0) + 1
    return {
        "claims": {"active": n_active, "ready": n_ready,
                    "killed": n_killed},
        "lit": {"citations": n_cit, "approved": n_cit_approved,
                "pending": n_cit_pending},
        "ablations": {
            "proposed": runs_by_status.get("proposed", 0),
            "queued":   runs_by_status.get("queued", 0),
            "running":  runs_by_status.get("running", 0),
            "done":     (runs_by_status.get("kept_novel", 0)
                          + runs_by_status.get("kept", 0)
                          + runs_by_status.get("discarded", 0)),
            "kept":     (runs_by_status.get("kept_novel", 0)
                          + runs_by_status.get("kept", 0)),
        },
    }


def get_status_overview() -> dict:
    """The single status payload the Write-the-paper view consumes on
    mount. Combines: phase + progress + gate + issues."""
    db = SessionLocal()
    try:
        phase = get_phase(db)
        progress = get_progress(db)
        # DB-derived counters override the agent-pushed values where
        # they disagree — counters are cheap to compute and always
        # fresh, while the agent's push payload can be stale.
        live = _derive_progress_from_db(db)
        for k, v in live.items():
            if k in progress and isinstance(progress[k], dict):
                merged = dict(progress[k])
                merged.update(v)
                progress[k] = merged
            else:
                progress[k] = v
        gate = get_gate(db)
        issues = _compute_issues(db, phase, progress, gate)
        meta = db.query(PaperMeta).first()
        label = PHASE_LABELS.get(phase["phase"], phase["phase"])
        # Pill summary is "Phase label — top issue OR generic OK".
        if issues:
            summary = f"{label} — {issues[0]['summary']}"
        else:
            summary = label
        novelty_row = (db.query(Setting)
                       .filter(Setting.key == "paper.novelty_v1").first())
        novelty_available = bool(novelty_row
                                  and isinstance(novelty_row.value, dict))
        return {
            "phase": phase,
            "phase_label": label,
            "summary": summary,
            "progress": progress,
            "gate": gate,
            "issues": issues,
            "novelty_available": novelty_available,
            "meta": {
                "venue": (meta.venue if meta else ""),
                "deadline_iso": (meta.deadline_iso if meta else ""),
            } if meta else None,
        }
    finally:
        db.close()


def _compute_issues(db, phase: dict, progress: dict, gate: dict) -> list[dict]:
    """Build the issues list (same shape as health/service)."""
    out: list[dict] = []
    cur_phase = phase.get("phase", "")
    # (Autopilot: operator_approval gate removed — the paper never waits on a
    # human to approve GPU spend. The PI/council review each revision instead.)
    # Author error — also critical.
    if cur_phase == "paper.error":
        out.append({
            "code": "author_error",
            "severity": 2,
            "summary": (phase.get("detail") or {}).get(
                "reason", "Author agent failed unexpectedly"),
            "evidence": phase.get("detail") or {},
            "since": phase.get("at") or "",
            "actions": [
                {"label": "Restart author",
                 "method": "POST",
                 "href": "/api/agent/restart"},
            ],
        })
    # Author hasn't reported any phase yet — warn so we know.
    if phase.get("fallback_used"):
        out.append({
            "code": "phase_not_reported",
            "severity": 1,
            "summary": ("Author hasn't reported a phase yet — it may "
                        "still be booting"),
            "evidence": {},
            "since": "",
            "actions": [
                {"label": "Restart author",
                 "method": "POST",
                 "href": "/api/agent/restart"},
            ],
        })
    return out
