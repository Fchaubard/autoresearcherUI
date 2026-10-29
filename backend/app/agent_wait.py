"""Persisted, deterministic waits between research decisions.

The agent registers a wait and ends its turn. The existing supervisor checks
the run ledger without calling a model, then wakes the same CLI conversation
once useful state changes. Experiment processes keep running independently.
"""
from __future__ import annotations

import datetime as dt
from contextlib import contextmanager
import threading

from .db import SessionLocal
from .models import Project, Run, Setting

KEY = "research_event_wait"
HANDOFF_KEY = "research_last_handoff"
_lock = threading.RLock()


@contextmanager
def delivery_lock():
    """Serialize app-owned input and wait state so a nudge cannot erase a message."""
    with _lock:
        yield


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _project(db):
    # Match the app's active-project selection used by /project and realrun.
    return db.query(Project).first()


def _snapshot(db, project_id):
    runs = db.query(Run.id, Run.status).filter(Run.project_id == project_id).all()
    bless = db.query(Setting).filter(Setting.key == "code_bless").first()
    state = bless.value if bless and isinstance(bless.value, dict) else {}
    return {"runs": {rid: status for rid, status in runs},
            "code_bless": state.get("status", "not_requested")}


def status():
    with SessionLocal() as db:
        row = db.query(Setting).filter(Setting.key == KEY).first()
        value = dict(row.value) if row and isinstance(row.value, dict) else {}
    # Internal baseline can be large. Never replay it to the model or UI.
    return {k: v for k, v in value.items() if k != "baseline"}


def cancel():
    with _lock, SessionLocal() as db:
        row = db.query(Setting).filter(Setting.key == KEY).first()
        if row:
            db.delete(row)
            db.commit()


def arm(summary: str, seconds: int = 900):
    """Wait only for real in-flight work, with a bounded safety reassessment."""
    from . import notify, paper
    if notify.research_paused() or notify.research_halted()[0]:
        raise ValueError("Research is paused or halted")
    if paper.project_mode() == "paper":
        raise ValueError("The research agent does not own paper mode")
    summary = summary.strip()
    if not summary or len(summary) > 4000:
        raise ValueError("Provide a concise handoff summary (1–4000 characters)")
    seconds = max(300, min(int(seconds), 1800))
    with _lock, SessionLocal() as db:
        project = _project(db)
        if not project:
            raise ValueError("No active research project")
        baseline = _snapshot(db, project.id)
        running = sum(s == "running" for s in baseline["runs"].values())
        if not running and baseline["code_bless"] != "pending":
            raise ValueError("No running experiments or pending code review; continue useful work")
        now = _now()
        value = {"status": "waiting", "project_id": project.id,
                 "armed_at": now.isoformat(),
                 "deadline": (now + dt.timedelta(seconds=seconds)).isoformat(),
                 "summary": summary, "baseline": baseline}
        row = db.query(Setting).filter(Setting.key == KEY).first()
        if row:
            row.value = value
        else:
            db.add(Setting(key=KEY, value=value))
        handoff_value = {"project_id": project.id, "at": now.isoformat(),
                         "summary": summary}
        handoff = db.query(Setting).filter(Setting.key == HANDOFF_KEY).first()
        if handoff:
            handoff.value = handoff_value
        else:
            db.add(Setting(key=HANDOFF_KEY, value=handoff_value))
        db.commit()
    from . import lifecycle
    lifecycle.emit_event("agent_waiting", "Research agent is waiting for experiment or review events; training continues without model polling.", actor="research_agent")
    return {"status": "waiting", "running_experiments": running,
            "deadline": value["deadline"],
            "instruction": "Finish this turn now. Do not sleep or poll. The server will resume this conversation when work changes or the safety timer expires."}


def _changes(before, after):
    old, new = before.get("runs", {}), after.get("runs", {})
    changes = [{"id": rid, "from": old.get(rid), "to": new.get(rid)}
               for rid in sorted(old.keys() | new.keys()) if old.get(rid) != new.get(rid)]
    review = before.get("code_bless") != after.get("code_bless")
    return changes, review


def supervise(*, alive: bool, busy: bool, idle: bool, blocked: bool,
              send, now=None):
    """Return True when an intentional wait owns idle-agent supervision.

    Keep the wait on failed delivery, and never interrupt active generation or
    an editable draft. The baseline survives backend restarts. No provider API
    is invoked here; `send` submits one event message to the existing CLI.
    """
    now = now or _now()
    with _lock, SessionLocal() as db:
        row = db.query(Setting).filter(Setting.key == KEY).first()
        if not row or not isinstance(row.value, dict):
            return False
        value = dict(row.value)
        project = _project(db)
        if not project or project.id != value.get("project_id"):
            db.delete(row)
            db.commit()
            return False
        if blocked or not alive:
            return True
        current = _snapshot(db, project.id)
        changed, review = _changes(value.get("baseline", {}), current)
        due = now >= dt.datetime.fromisoformat(value["deadline"])
        if not (changed or review or due):
            return True
        if busy or not idle:
            return True
        last_attempt = value.get("last_attempt")
        if last_attempt and (now - dt.datetime.fromisoformat(last_attempt)).total_seconds() < 60:
            return True
        value["last_attempt"] = now.isoformat()
        row.value = value
        db.commit()
        details = "; ".join(f"{c['id']}: {c['from']} -> {c['to']}" for c in changed[:12])
        if len(changed) > 12:
            details += f"; and {len(changed) - 12} more run changes"
        if review:
            details += f"; code review: {current['code_bless']}"
        if not details:
            details = "Safety reassessment timer expired; inspect concise status once."
        message = ("[RESEARCH EVENT] " + details + "\nYour saved handoff: " + value["summary"]
                   + "\nContinue from this conversation. GET /api/agent/context for bounded current state. "
                     "Preserve running experiments. Make the next research decision; when only waiting remains, "
                     "POST /api/agent/wait and finish your turn again. Do not run sleep/status-poll loops.")
        if not send(message):
            return True
        db.delete(row)
        db.commit()
    from . import lifecycle
    lifecycle.emit_event("agent_woken", "Research agent resumed after an experiment/review event or bounded safety reassessment.", actor="supervisor")
    return True


def context():
    """Bounded working state; full history remains in its existing APIs/files."""
    with SessionLocal() as db:
        project = _project(db)
        if not project:
            return {"project": None, "active_runs": [], "recent_finished": []}
        query = db.query(Run).filter(Run.project_id == project.id)
        active = query.filter(Run.status.in_(["running", "queued"])).order_by(Run.created_at.desc()).limit(40).all()
        finished = query.filter(~Run.status.in_(["running", "queued"])).order_by(Run.ended_at.desc()).limit(8).all()
        handoff = db.query(Setting).filter(Setting.key == HANDOFF_KEY).first()
        last_handoff = handoff.value if handoff and isinstance(handoff.value, dict) else {}
        if last_handoff.get("project_id") != project.id:
            last_handoff = {}
        def brief(run):
            return {"id": run.id, "name": (run.run_name or "")[:160], "status": run.status,
                    "gpu": run.gpu_index, "metric": run.headline_metric,
                    "started_at": run.started_at, "ended_at": run.ended_at}
        return {"project": {"id": project.id, "name": project.name,
                            "purpose": (project.purpose or "")[:1600],
                            "metric": project.validation_metric},
                "active_runs": [brief(run) for run in active],
                "recent_finished": [brief(run) for run in finished],
                "active_count": query.filter(Run.status.in_(["running", "queued"])).count(),
                "total_runs": query.count(),
                "last_handoff": last_handoff,
                "note": "This is a bounded summary. Retrieve a specific run or file when its evidence is needed."}


POLICY = """## Model-call efficiency and durable research context
Keep the selected model and reasoning effort. Spend model calls on decisions,
implementation and analysis, not mechanical waiting. Experiment workers and
dispatchers run independently of this conversation.

When all useful preparation is complete and experiments or code review are
running, POST $ARUI_INGEST_URL/api/agent/wait with Authorization: Bearer
$ARUI_INGEST_TOKEN and JSON {"summary":"what is running, the next decision,
and paths to evidence","seconds":900}. If it returns status=waiting, FINISH
YOUR TURN. The server checks the ledger without an LLM and wakes this same
conversation on run/review changes or a bounded safety timer. Do not issue
clock.sleep, repeated status commands or repeated code-bless requests. Do not
stop experiments. If the request fails or no wait is registered, diagnose the
error; do not assume the server will wake you.

Use GET /api/agent/context (same Authorization header) for concise current
state. Maintain a short AGENT_STATE.md with active plan, decisions, blockers,
next actions and evidence paths at meaningful milestones. Read that first on
handoff; retrieve specific sections of ideas, lessons and logs as needed.
Never reread the entire transcript, run ledger or accumulated lessons on every
cycle. Preserve all full history on disk. Batch independent status reads in
one tool call. Avoid rewriting status documents when nothing changed.
An intentional registered event wait is autonomous operation, not a halt or a
request for human input. It takes precedence over older instructions to keep
polling or to keep the conversation generating while experiments run.
"""
