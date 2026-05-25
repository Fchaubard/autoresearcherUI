"""All HTTP routes: REST + SSE streams + the arui ingest endpoints (doc 08)."""
from __future__ import annotations

import asyncio
import datetime as dt
import random

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from . import metrics, orchestrator
from .bus import bus
from .config import ROOT
from .db import SessionLocal, get_session
from .models import ChatMessage, Event, Gpu, Idea, JournalEntry, Project, Run

router = APIRouter(prefix="/api")
_rng = random.Random()


def _iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


# ───────────────────────────── REST: read ──────────────────────────────────

@router.get("/project")
def get_project(db: Session = Depends(get_session)):
    p = db.query(Project).first()
    if not p:
        return {}
    runs = db.query(Run).filter(Run.project_id == p.id).all()
    ideas = db.query(Idea).filter(Idea.project_id == p.id).all()
    done = [r for r in runs if r.status in ("kept", "discarded", "crashed")]
    kept = [r for r in done if r.status == "kept"]
    best = None
    for r in done:
        if r.headline_metric is None:
            continue
        if best is None or (r.headline_metric > best
                            if p.metric_direction == "maximize"
                            else r.headline_metric < best):
            best = r.headline_metric
    return {
        **p.dict(),
        "experiments_done": len(done),
        "experiments_running": len([r for r in runs if r.status == "running"]),
        "experiments_total": len(ideas),
        "success_rate": round(len(kept) / len(done), 2) if done else 0,
        "best_metric": best,
        "baseline_metric": 0.412,
    }


@router.get("/ideas")
def list_ideas(db: Session = Depends(get_session)):
    ideas = db.query(Idea).all()
    # upcoming sorted by manual priority then EV desc (doc 05 5.7)
    return sorted([i.dict() for i in ideas],
                  key=lambda i: (-i["manual_priority"], -i["ev"]))


@router.get("/runs")
def list_runs(db: Session = Depends(get_session)):
    return [r.dict() for r in db.query(Run).all()]


@router.get("/runs/{run_id}")
def get_run(run_id: str, db: Session = Depends(get_session)):
    r = db.query(Run).filter(Run.id == run_id).first()
    if not r:
        return {}
    idea = db.query(Idea).filter(Idea.id == r.idea_id).first()
    return {**r.dict(), "idea": idea.dict() if idea else None,
            "metric_keys": metrics.keys(run_id)}


@router.get("/runs/{run_id}/metrics")
def run_metrics(run_id: str, keys: str = "", db: Session = Depends(get_session)):
    wanted = [k for k in keys.split(",") if k] or None
    return metrics.query(run_id, wanted)


@router.get("/gpus")
def list_gpus(db: Session = Depends(get_session)):
    return [g.dict() for g in db.query(Gpu).order_by(Gpu.index).all()]


@router.get("/events")
def list_events(limit: int = 60, db: Session = Depends(get_session)):
    rows = db.query(Event).order_by(Event.created_at.desc()).limit(limit).all()
    return [e.dict() for e in rows]


@router.get("/journal")
def list_journal(db: Session = Depends(get_session)):
    rows = db.query(JournalEntry).order_by(
        JournalEntry.created_at.desc()).all()
    return [j.dict() for j in rows]


@router.get("/chat")
def list_chat(db: Session = Depends(get_session)):
    rows = db.query(ChatMessage).order_by(ChatMessage.created_at).all()
    return [m.dict() for m in rows]


# ──────────────────────────── REST: write ──────────────────────────────────

@router.post("/chat")
async def post_chat(request: Request):
    body = await request.json()
    text = (body.get("content") or "").strip()
    db = SessionLocal()
    msg = ChatMessage(id=f"cm-{_rng.randrange(16**8):08x}", role="researcher",
                      content=text, created_at=_iso())
    db.add(msg)
    db.commit()
    bus.publish("chat", "chat", msg.dict())
    db.close()
    asyncio.create_task(_agent_reply(text))
    return {"ok": True}


async def _agent_reply(prompt: str) -> None:
    """Scaffold stand-in for the Principal Researcher (doc 05 5.9)."""
    await asyncio.sleep(1.4)
    canned = ("On it. The four running experiments are all past step 200; "
              "prototype insertion is still the front-runner. I'll fold your "
              "note into the next planning loop.")
    db = SessionLocal()
    msg = ChatMessage(id=f"cm-{_rng.randrange(16**8):08x}", role="agent",
                      content=canned, created_at=_iso())
    db.add(msg)
    db.commit()
    bus.publish("chat", "chat", msg.dict())
    db.close()


@router.post("/ideas/reorder")
async def reorder_ideas(request: Request):
    """Pin a manual priority order on the idea queue (doc 05 5.8)."""
    body = await request.json()
    ordered = body.get("ordered_ids", [])
    db = SessionLocal()
    for rank, idea_id in enumerate(reversed(ordered)):
        idea = db.query(Idea).filter(Idea.id == idea_id).first()
        if idea:
            idea.manual_priority = rank + 1
    db.commit()
    db.close()
    bus.publish("events", "runs_changed", {})
    return {"ok": True}


# ───────────────────────── arui ingest (doc 06) ────────────────────────────

@router.post("/track/run")
async def track_run(request: Request):
    body = await request.json()
    name = body.get("name", f"run-{_rng.randrange(16**6):06x}")
    db = SessionLocal()
    project = db.query(Project).first()
    pid = project.id if project else "proj-default"
    if not db.query(Run).filter(Run.id == name).first():
        idea = Idea(id=f"idea-{name}", project_id=pid, idea_id=name,
                    description="(logged via the arui SDK)", status="running",
                    source="agent", created_at=_iso(), started_at=_iso())
        db.add(idea)
        db.add(Run(id=name, project_id=pid, idea_id=idea.id, run_name=name,
                   status="running", config=body.get("config", {}),
                   started_at=_iso(), created_at=_iso()))
        db.commit()
        bus.publish("events", "runs_changed", {})
    db.close()
    return {"run_id": name}


@router.post("/track/log")
async def track_log(request: Request):
    body = await request.json()
    run_id = body["run_id"]
    points = body.get("points", [])
    metrics.append(run_id, points)
    bus.publish("metrics", "metric", {"run_id": run_id, "points": points})
    return {"ok": True}


@router.post("/track/artifact")
async def track_artifact(request: Request):
    await request.json()
    return {"ok": True}


@router.post("/track/finish")
async def track_finish(request: Request):
    body = await request.json()
    run_id = body["run_id"]
    summary = body.get("summary", {})
    db = SessionLocal()
    run = db.query(Run).filter(Run.id == run_id).first()
    if run:
        run.status = "kept"
        run.ended_at = _iso()
        for v in summary.values():
            if isinstance(v, (int, float)):
                run.headline_metric = float(v)
                break
        idea = db.query(Idea).filter(Idea.id == run.idea_id).first()
        if idea:
            idea.status = "success"
            idea.ended_at = _iso()
        db.commit()
    db.close()
    bus.publish("events", "runs_changed", {})
    return {"ok": True}


# ─────────────────────────── SSE streams (doc 11 D1) ───────────────────────

def _sse(topic: str) -> StreamingResponse:
    return StreamingResponse(
        bus.subscribe(topic), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/stream/metrics")
async def stream_metrics():
    return _sse("metrics")


@router.get("/stream/events")
async def stream_events():
    return _sse("events")


@router.get("/stream/gpus")
async def stream_gpus():
    return _sse("gpus")


@router.get("/stream/chat")
async def stream_chat():
    return _sse("chat")


# ──────── orchestrator: run the bundled example research project ────────────
# Used by the e2e integration test and for a hardware-free local run of the
# full autonomous loop (FakeAgent + the tiny-sgd project).

@router.post("/dev/run-example")
async def dev_run_example():
    ex = ROOT / "tests" / "example_project"
    if not ex.exists():
        return {"status": "error", "detail": "example project not found"}
    o = orchestrator.active()
    if o and o.running:
        return {"status": "already_running"}
    orchestrator.start(str(ex), name="tiny-sgd", n_slots=3,
                       metric_key="val_mse", direction="minimize")
    return {"status": "started", "project": "tiny-sgd"}


@router.get("/dev/status")
def dev_status():
    o = orchestrator.active()
    return {"running": bool(o and o.running),
            "project_id": o.project_id if o else None}
