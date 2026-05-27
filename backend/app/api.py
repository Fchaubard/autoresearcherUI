"""All HTTP routes: REST + SSE streams + the arui ingest endpoints (doc 08)."""
from __future__ import annotations

import asyncio
import datetime as dt
import glob
import math
import os
import random
import re
import subprocess
import threading

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from . import (archive, authkeys, metrics, monitor, notify, orchestrator,
               realrun)
from .bus import bus
from .config import DATA_DIR, ROOT
from .db import Base, SessionLocal, engine, get_session
from .models import (ChatMessage, Event, Gpu, Idea, JournalEntry,
                     ModeHistory, PaperBaseline, PaperBudgetEvent,
                     PaperCitation, PaperClaim, PaperDecision, PaperFigure,
                     PaperMeta, PaperProposal, PaperReviewSim, PaperSection,
                     PaperVersion, Project, Run, Setting)

router = APIRouter(prefix="/api")
_rng = random.Random()


def _iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


# ─────────────────────────── headline metric ───────────────────────────────
# Keys that are configuration/bookkeeping, never a run's headline result.
_NON_METRIC = {
    "params", "n_params", "nparams", "num_params", "param_count",
    "seed", "step", "steps", "epoch", "epochs", "iter", "iters", "max_iters",
    "batch_size", "global_batch_size", "ensemble_size", "n_eval", "n",
    "size", "hidden_size", "h_cycles", "gpu", "gpu_index", "lr",
}
# Metric names worth using as a headline when the validation metric is absent.
_COMMON_METRICS = ["gsm8k_test_acc", "test_acc", "val_acc", "accuracy",
                   "score", "f1", "val_loss", "val_mse", "loss"]


def _looks_baseline(name: str) -> bool:
    return "baseline" in (name or "").lower()


def _as_number(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def _resolve_headline(run_id: str, summary: dict, metric_key: str):
    """A run's headline number: the project's validation metric if the agent
    reported it, else a logged value of it, else a sane numeric fallback.
    Never picks an obvious non-metric like a parameter count."""
    keys = ([metric_key] if metric_key else []) + \
           [k for k in _COMMON_METRICS if k != metric_key]
    for k in keys:                          # 1. summary value for a metric key
        n = _as_number((summary or {}).get(k))
        if n is not None:
            return n
    for k in keys:                          # 2. last logged point of a metric
        v = metrics.latest(run_id, k)
        if v is not None:
            return float(v)
    for k, v in (summary or {}).items():    # 3. any non-config numeric value
        if str(k).lower() not in _NON_METRIC:
            n = _as_number(v)
            if n is not None:
                return n
    return None


def _is_crashed(headline, direction: str) -> bool:
    """A run crashed if it has no metric or training diverged. For loss-style
    (minimize) metrics a huge value signals divergence; an accuracy-style
    (maximize) metric simply being low is a poor result, not a crash."""
    if headline is None or not math.isfinite(headline):
        return True
    if direction != "maximize" and headline >= 5e4:
        return True
    return False


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
        if (r.status == "crashed" or r.headline_metric is None
                or not math.isfinite(r.headline_metric)):
            continue
        if best is None or (r.headline_metric > best
                            if p.metric_direction == "maximize"
                            else r.headline_metric < best):
            best = r.headline_metric
    base_run = next((r for r in runs if r.is_baseline), None)
    return {
        **p.dict(),
        "experiments_done": len(done),
        "experiments_running": len([r for r in runs if r.status == "running"]),
        "experiments_total": len(ideas),
        "success_rate": round(len(kept) / len(done), 2) if done else 0,
        "best_metric": best,
        "baseline_metric": base_run.headline_metric if base_run else None,
    }


@router.get("/ideas")
def list_ideas(db: Session = Depends(get_session)):
    ideas = db.query(Idea).all()
    # upcoming sorted by manual priority then EV desc (doc 05 5.7)
    return sorted([i.dict() for i in ideas],
                  key=lambda i: (-i["manual_priority"], -i["ev"]))


@router.post("/ideas/reorder")
async def ideas_reorder(request: Request):
    """Re-rank queued ideas (top of the list runs first) and tell the agent."""
    body = await request.json()
    order = body.get("order", [])
    db = SessionLocal()
    names = []
    for i, iid in enumerate(order):
        idea = db.query(Idea).filter(Idea.id == iid).first()
        if idea:
            idea.manual_priority = len(order) - i
            names.append(idea.idea_id)
    db.commit()
    db.close()
    if names:
        try:
            monitor.message_agent(
                "The researcher reprioritised the idea queue — run the "
                "queued ideas in THIS order next: " + ", ".join(names)
                + ". Update ideas.md to match this ordering.")
        except Exception:                            # noqa: BLE001
            pass
    bus.publish("events", "runs_changed", {})
    return {"status": "ok", "order": names}


@router.post("/ideas/delete")
async def ideas_delete(request: Request):
    """Remove a queued idea; feed the reason back to the agent."""
    body = await request.json()
    iid = body.get("idea_id", "")
    reason = (body.get("reason") or "").strip()
    db = SessionLocal()
    idea = db.query(Idea).filter(Idea.id == iid).first()
    name = idea.idea_id if idea else iid
    if idea:
        db.delete(idea)
    row = db.query(Setting).filter(Setting.key == "dismissed_ideas").first()
    dismissed = list(row.value) if row and isinstance(row.value, list) else []
    if name and name not in dismissed:
        dismissed.append(name)
    if row:
        row.value = dismissed
    else:
        db.add(Setting(key="dismissed_ideas", value=dismissed))
    db.add(Event(id=f"ev-{_rng.randrange(16**8):08x}", type="idea_added",
                 severity="info", actor="human",
                 message=f"Researcher removed queued idea '{name}'"
                         + (f" — {reason}" if reason else ""),
                 created_at=_iso()))
    db.commit()
    db.close()
    try:
        monitor.message_agent(
            f"The researcher REMOVED the queued idea '{name}' from the plan."
            + (f" Their reason: {reason}." if reason else "")
            + " Do not run it — remove it from ideas.md and take this "
              "preference into account when choosing future ideas.")
    except Exception:                                # noqa: BLE001
        pass
    bus.publish("events", "runs_changed", {})
    return {"status": "ok"}


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
def list_events(limit: int = 60, before: str = "",
                db: Session = Depends(get_session)):
    """Recent events. Pass `before=<iso>` for the page older than that — used
    by the Summary feed's lazy scroll-up loader."""
    q = db.query(Event)
    if before:
        q = q.filter(Event.created_at < before)
    rows = q.order_by(Event.created_at.desc()).limit(limit).all()
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
                    description="", status="running",
                    source="agent", created_at=_iso(), started_at=_iso())
        db.add(idea)
        db.add(Run(id=name, project_id=pid, idea_id=idea.id, run_name=name,
                   status="running", is_baseline=_looks_baseline(name),
                   config=body.get("config", {}),
                   started_at=_iso(), created_at=_iso()))
        db.commit()
        bus.publish("events", "runs_changed", {})
    db.close()
    return {"run_id": name}


_METRICS_CHANGED_DEBOUNCE: dict[str, float] = {}
_METRICS_CHANGED_DEBOUNCE_LOCK = threading.Lock()


def _maybe_emit_metrics_changed(run_id: str) -> None:
    """Coalesce metrics_changed(run_id) events to at most one per 2 s per
    run. The Analysis tab uses this to refetch bucketed data; firing more
    often than that just makes lines flash as buckets shift. Combined
    with the frontend's 1.2 s refresh debounce, this gives a smooth
    once-per-2 s tick on running runs."""
    import time as _time
    now = _time.time()
    with _METRICS_CHANGED_DEBOUNCE_LOCK:
        last = _METRICS_CHANGED_DEBOUNCE.get(run_id, 0.0)
        if now - last < 2.0:
            return
        _METRICS_CHANGED_DEBOUNCE[run_id] = now
    bus.publish("metrics", "metrics_changed", {"run_id": run_id})


@router.post("/track/log")
async def track_log(request: Request):
    body = await request.json()
    run_id = body["run_id"]
    points = body.get("points", [])
    metrics.append(run_id, points)
    bus.publish("metrics", "metric", {"run_id": run_id, "points": points})
    _maybe_emit_metrics_changed(run_id)
    return {"ok": True}


@router.post("/track/logs")
async def track_logs(request: Request):
    """Append a run's captured console output (sent by the arui SDK). This is
    how run logs are collected — independent of tmux."""
    body = await request.json()
    run_id = body.get("run_id", "")
    text = body.get("text", "")
    if run_id and re.match(r"^[A-Za-z0-9_.\-=]+$", run_id) and text:
        try:
            d = DATA_DIR / "run_logs"
            d.mkdir(parents=True, exist_ok=True)
            with open(d / f"{run_id}.log", "a", errors="ignore") as f:
                f.write(text)
        except OSError:
            pass
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
        proj = db.query(Project).first()
        metric_key = (proj.validation_metric if proj else "") or ""
        direction = (proj.metric_direction if proj else "minimize") \
            or "minimize"
        headline = _resolve_headline(run_id, summary, metric_key)
        run.headline_metric = headline
        run.ended_at = _iso()
        crashed = _is_crashed(headline, direction)
        run.status = "crashed" if crashed else "kept"
        idea = db.query(Idea).filter(Idea.id == run.idea_id).first()
        if idea:
            idea.status = "failed" if crashed else "success"
            idea.ended_at = _iso()
        ev = Event(id=f"ev-{_rng.randrange(16**8):08x}", type="run_finished",
                   severity="warning" if crashed else "info", actor="agent",
                   message=(f"{run_id} diverged" if crashed
                            else f"{run_id} finished — {headline:.4f}"),
                   run_id=run_id, created_at=_iso())
        db.add(ev)
        db.commit()
        payload = ev.dict()
        db.close()
        bus.publish("events", "event", payload)
        # immediate email if this run set a new best (notify decides)
        threading.Thread(target=notify.on_run_finished, args=(run_id,),
                         daemon=True).start()
        # Council reviews. Two paths:
        #  1) Per-run review (default OFF — was noisy and pushed the agent
        #     off working tracks). Settings.council_per_run_enabled enables.
        #  2) Strategic batch review every N=GPU-count finished runs — one
        #     reflection per parallel-run wave. This is the default.
        from . import council
        council.review_async(run_id)             # gated inside on settings
        council.note_run_finished(run_id)        # batch trigger
    else:
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


# ──────────── agent terminal: live view + chat into the session ─────────────

def _tmux_alive(session: str = "agent") -> bool:
    return subprocess.run(["tmux", "has-session", "-t", session],
                          capture_output=True).returncode == 0


@router.get("/agent/terminal")
def agent_terminal():
    """Live contents of the agent's tmux session — drives the rail Live tab."""
    r = subprocess.run(
        ["tmux", "capture-pane", "-t", "agent", "-p", "-S", "-3000"],
        capture_output=True, text=True)
    text = r.stdout if r.returncode == 0 else ""
    if not text.strip():                       # no live pane — fall back to log
        logs = [p for p in glob.glob(
                str(DATA_DIR / "workspace" / "*" / "agent.log"))
                if os.path.exists(p) and os.path.getsize(p) > 0]
        logs.sort(key=os.path.getmtime)
        if logs:
            try:
                text = open(logs[-1], errors="ignore").read()[-16000:]
            except OSError:
                pass
    return {"text": text or "(no agent session yet)",
            "alive": _tmux_alive("agent")}


@router.post("/pi/run")
async def pi_run_now(request: Request):
    """Trigger an immediate PI cycle. Returns the decision dict so the UI /
    a test script can verify the PI is wired up correctly."""
    from . import pi as _pi
    out = _pi.cycle(force=True)
    return out or {"status": "skipped"}


@router.post("/council/strategic")
async def council_strategic_now(request: Request):
    """Trigger an immediate strategic review on the N most-recent finished
    runs (or a caller-supplied list). Test endpoint."""
    from . import council as _c
    body = await request.json()
    ids = body.get("run_ids") or []
    if not ids:
        n = int(body.get("n") or _c._strategic_threshold(_c._settings()))
        db = SessionLocal()
        try:
            runs = (db.query(Run)
                    .filter(Run.status.in_(["kept", "discarded", "crashed",
                                            "failed", "success"]))
                    .order_by(Run.ended_at.desc())
                    .limit(n).all())
            ids = [r.id for r in runs]
        finally:
            db.close()
    out = _c.strategic_review(ids)
    if out:
        _c._persist_strategic(ids, out)
        _c._apply_to_ideas_md(out)
    return out or {"status": "no_review"}


@router.post("/council/review")
async def council_review_now(request: Request):
    """Trigger a council review on a specific run_id (synchronous)."""
    from . import council as _c
    body = await request.json()
    rid = (body.get("run_id") or "").strip()
    if not rid:
        return {"status": "error", "detail": "run_id required"}
    out = _c.deliberate(rid)
    if out:
        _c._persist(rid, out)
        _c._apply_to_ideas_md(out)
    return out or {"status": "no_review"}


@router.post("/agent/send")
async def agent_send(request: Request):
    """Type a message into the agent's Claude Code tmux session."""
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "empty"}
    if not _tmux_alive("agent"):
        return {"ok": False, "error": "no agent session is running"}
    subprocess.run(["tmux", "send-keys", "-t", "agent", "-l", text],
                   capture_output=True)
    subprocess.run(["tmux", "send-keys", "-t", "agent", "Enter"],
                   capture_output=True)
    db = SessionLocal()
    msg = ChatMessage(id=f"cm-{_rng.randrange(16**8):08x}", role="researcher",
                      content=text, created_at=_iso())
    db.add(msg)
    db.commit()
    payload = msg.dict()
    db.close()
    bus.publish("chat", "chat", payload)
    return {"ok": True}


# ──────────── reset + onboarding ─────────────────────────────────────────────

@router.post("/reset")
async def reset_all():
    """Wipe everything and return the instance to the onboarding state."""
    orchestrator.stop()
    realrun.stop()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    metrics.reset()
    return {"status": "reset"}


@router.get("/onboarding")
def get_onboarding(db: Session = Depends(get_session)):
    row = db.query(Setting).filter(Setting.key == "onboarding").first()
    return row.value if row else {}


@router.get("/onboarding/defaults")
def onboarding_defaults():
    """Defaults for editable onboarding fields. The frontend pre-fills these
    so the user sees how the agent is configured by default and can override."""
    # Lazy imports so circular-import safety: council and pi import db/models.
    from . import council as _c
    from . import pi as _p
    out = {"agent_instructions": realrun.DEFAULT_AGENT_INSTRUCTIONS}
    out.update(_c.DEFAULTS)
    out.update(_p.DEFAULTS)
    # research agent (Claude) default model
    out.setdefault("research_agent_model", "claude-opus-4-6")
    return out


# Secret / token fields are blanked in /api/settings responses so the UI
# doesn't display the saved value. PUTting blank means "leave unchanged".
SECRET_FIELDS = {"claude_token", "gemini_token", "openai_token",
                 "github_token", "gmail_app_pw", "passcode"}


@router.get("/settings")
def get_settings(db: Session = Depends(get_session)):
    """Return the saved onboarding config (= live settings), with secret
    fields masked. The Settings tab uses this to populate its form."""
    row = db.query(Setting).filter(Setting.key == "onboarding").first()
    cfg = dict(row.value) if row and isinstance(row.value, dict) else {}
    masked = {k: ("••••••••" if (k in SECRET_FIELDS and v) else v)
              for k, v in cfg.items()}
    return masked


@router.put("/settings")
async def put_settings(request: Request):
    """Merge updates into the onboarding config. Secret fields are only
    updated when the user provides a non-empty, non-mask value (blanks
    mean 'leave alone'). Returns the new (masked) settings."""
    updates = await request.json()
    if not isinstance(updates, dict):
        return {"status": "error", "detail": "expected an object"}
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(Setting.key == "onboarding").first()
        cur = dict(row.value) if row and isinstance(row.value, dict) else {}
        for k, v in updates.items():
            # don't clobber a saved secret with a blank or mask
            if k in SECRET_FIELDS:
                if not v or str(v).strip() in ("", "••••••••"):
                    continue
            cur[k] = v
        if row:
            row.value = cur
        else:
            db.add(Setting(key="onboarding", value=cur))
        db.commit()
    finally:
        db.close()
    return {"status": "ok"}


@router.post("/onboarding")
async def post_onboarding(request: Request):
    """Save the onboarding config and register the project.

    This does NOT run anything and shows NO demo data. The engine that
    actually researches the configured project — a real Claude Code agent on
    the GPUs (RealAgent) — is not built yet, so the dashboard stays honestly
    empty until a real agent produces real experiments.
    """
    cfg = await request.json()
    db = SessionLocal()
    row = db.query(Setting).filter(Setting.key == "onboarding").first()
    if row:
        row.value = cfg
    else:
        db.add(Setting(key="onboarding", value=cfg))
    db.commit()
    db.close()

    # a Claude token (or the test hook) -> launch the real autonomous agent
    token = (cfg.get("claude_token") or "").strip()
    if token or os.environ.get("ARUI_CLAUDE_BIN"):
        realrun.start_real(cfg)
        return {"status": "started"}

    # otherwise just register the project; dashboard stays honestly empty
    db = SessionLocal()
    if not db.query(Project).first():
        metric = (cfg.get("metric") or "metric").strip()
        direction = "maximize" if metric in (
            "accuracy", "f1", "arc_score", "reward") else "minimize"
        db.add(Project(
            id="proj-" + (cfg.get("repo_name") or "project"),
            name=cfg.get("repo_name") or "project",
            purpose=cfg.get("purpose", ""),
            validation_metric=metric, metric_direction=direction,
            status="awaiting agent", gpu_count=0, created_at=_iso()))
        db.commit()
    db.close()
    return {"status": "configured"}


# ──────────── tmux run sessions (the Sessions tab) ───────────────────────────

_INFRA_SESSIONS = {"arui", "cf", "agent"}
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.\-=]+$")   # run ids contain '='


@router.get("/sessions")
def list_sessions():
    """Every tmux session that is a research run (infra sessions excluded)."""
    out = subprocess.run(["tmux", "list-sessions", "-F", "#{session_name}"],
                         capture_output=True, text=True)
    names = [n.strip() for n in out.stdout.splitlines() if n.strip()]
    return {"sessions": [n for n in names if n not in _INFRA_SESSIONS]}


@router.get("/sessions/{name}")
def session_output(name: str):
    """The captured stdout/stderr of one run's tmux session."""
    if not _SAFE_NAME.match(name or ""):
        return {"text": "", "alive": False}
    alive = subprocess.run(["tmux", "has-session", "-t", name],
                           capture_output=True).returncode == 0
    if not alive:
        return {"text": "", "alive": False}
    out = subprocess.run(
        ["tmux", "capture-pane", "-t", name, "-p", "-S", "-4000"],
        capture_output=True, text=True)
    return {"text": out.stdout, "alive": True}


# ──────────── run logs, kill, system stats, metric names ─────────────────────

@router.get("/runs/{run_id}/logs")
def run_logs(run_id: str, tail: int = 800):
    """A run's captured stdout/stderr — persists after the run finishes."""
    return monitor.run_log(run_id, tail)


@router.post("/runs/{run_id}/kill")
def run_kill(run_id: str):
    """Kill a run's tmux session (the monitor then marks it crashed)."""
    if not _SAFE_NAME.match(run_id or ""):
        return {"ok": False, "error": "bad run id"}
    subprocess.run(["tmux", "kill-session", "-t", run_id],
                   capture_output=True)
    return {"ok": True}


@router.get("/system")
def system():
    """Cached host telemetry — GPUs, CPU, RAM, disk, uptime."""
    return monitor.system_stats()


@router.post("/clientlog")
async def clientlog(request: Request):
    """Receive a browser-side JS error so frontend crashes are debuggable."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    line = (f"{_iso()} {body.get('msg', '')} | {body.get('src', '')}:"
            f"{body.get('line', '')} | {str(body.get('stack', ''))[:700]}")
    print("[clientlog]", line, flush=True)
    try:
        with open(DATA_DIR / "clientlog.txt", "a") as f:
            f.write(line + "\n")
    except OSError:
        pass
    return {"ok": True}


@router.get("/metrics/names")
def metric_names():
    return {"metrics": metrics.all_keys()}


# ─────────── Analysis v2: batched bucketed metrics + keys + panels ────────

@router.get("/metrics/keys")
def metrics_keys():
    """All metric keys ever seen on this project (Analysis 'Add panel' uses
    this). Backed by the metric_keys table, not a DISTINCT scan."""
    return {"keys": metrics.all_keys()}


@router.get("/runs/{run_id}/metric_keys")
def run_metric_keys(run_id: str):
    """Keys that THIS run has logged. Drives the drawer 'View all plots'."""
    return {"keys": metrics.run_keys(run_id)}


@router.get("/metrics/key_coverage")
def metrics_key_coverage(key: str = "", limit: int = 50):
    """Which runs in this project logged a given metric key.
    Drives the Analysis empty-state's 'X other runs logged this — click to
    swap' helper. Returns up to `limit` run_ids ordered newest first."""
    if not key:
        return {"key": "", "run_ids": []}
    db = SessionLocal()
    try:
        rows = (db.query(Run.id, Run.run_name, Run.created_at)
                .filter(Run.id.in_(
                    [r[0] for r in metrics._con.execute(
                        "SELECT DISTINCT run_id FROM metrics WHERE key = ?",
                        [key]).fetchall()]))
                .order_by(Run.created_at.desc())
                .limit(limit).all())
        return {"key": key,
                "run_ids": [{"id": r[0], "run_name": r[1]} for r in rows]}
    finally:
        db.close()


@router.post("/metrics/batch")
async def metrics_batch(request: Request):
    """Server-bucketed multi-run, multi-key time-series. The single endpoint
    powering the Analysis tab's panel grid. Schema in
    docs/12-analysis-v2-spec-final.md."""
    body = await request.json()
    run_ids = body.get("run_ids") or []
    keys_wanted = body.get("keys") or []
    x_key = body.get("x_key") or "step"
    x_min = body.get("x_min")
    x_max = body.get("x_max")
    bucket_count = int(body.get("bucket_count") or 500)
    if not isinstance(run_ids, list) or not isinstance(keys_wanted, list):
        return {"status": "error", "detail": "run_ids and keys must be lists"}
    # Which runs are still running? Affects cache freshness.
    db = SessionLocal()
    try:
        running = {r.id for r in db.query(Run)
                   .filter(Run.id.in_(run_ids), Run.status == "running")
                   .all()}
    finally:
        db.close()
    return metrics.batch_bucketed(
        run_ids, keys_wanted,
        x_key=x_key, x_min=x_min, x_max=x_max,
        bucket_count=bucket_count, running_set=running)


@router.get("/analysis/panels")
def analysis_panels(db: Session = Depends(get_session)):
    """Persisted panel set for the Analysis tab. Falls back to a default
    set that adapts to which metric keys this project actually logs."""
    row = db.query(Setting).filter(Setting.key == "analysis_panels").first()
    if row and isinstance(row.value, dict) and row.value.get("panels"):
        return row.value

    # Project-aware default. Always emit the spec's standard 6 slots, so the
    # UI shows them as labelled spots — but title each with what the project
    # actually logs when possible. The project metric (proj.validation_metric)
    # is the most important one and gets pinned first.
    proj = db.query(Project).first()
    project_metric = (proj.validation_metric if proj else "") or ""
    keys_in_project = set(metrics.all_keys() or [])

    def _panel(pid: str, title: str, key: str, log: bool = False,
               baseline: bool = True, width: str = "half") -> dict:
        return {"id": pid, "title": title, "y_keys": [key],
                "x_key": "step", "y_log": log, "smoothing": 0.0,
                "include_baseline": baseline, "show_band": False,
                "width": width}

    panels = []
    # Pin the project's own validation metric first if it's set and exists.
    if project_metric and project_metric in keys_in_project:
        panels.append(_panel("p_main", f"Project metric · {project_metric}",
                              project_metric, log=False, baseline=True,
                              width="full"))
    # Standard spec defaults — keep their slots even if not logged yet so
    # the user sees the empty-state message and knows what's expected.
    panels.append(_panel("p_train_loss", "Training loss", "train_loss",
                         log=True))
    panels.append(_panel("p_val_loss", "Validation loss", "val_loss",
                         log=True))
    panels.append(_panel("p_train_acc", "Training accuracy", "train_acc"))
    panels.append(_panel("p_val_acc", "Validation accuracy", "val_acc"))
    panels.append(_panel("p_lr", "Learning rate", "lr", baseline=False))
    panels.append(_panel("p_step_time", "Time per step", "time_per_step",
                         baseline=False))
    return {"panels": panels}


@router.put("/analysis/panels")
async def analysis_panels_put(request: Request):
    """Save the panel set."""
    body = await request.json()
    panels = body.get("panels") or []
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(Setting.key == "analysis_panels").first()
        if row:
            row.value = {"panels": panels}
        else:
            db.add(Setting(key="analysis_panels", value={"panels": panels}))
        db.commit()
    finally:
        db.close()
    return {"status": "ok"}


# ──────────── authorized_keys management ─────────────────────────────────────

@router.post("/extra_nodes/check")
async def extra_nodes_check(request: Request):
    """Try to ssh to each of the user's extra GPU nodes and run nvidia-smi.
    Returns a list of {target, ok, gpus, error}. Best-effort — never raises."""
    body = await request.json()
    targets = body.get("targets") or []
    if isinstance(targets, str):
        targets = [t.strip() for t in targets.splitlines() if t.strip()]
    out = []
    for t in targets[:16]:
        host = t.strip()
        if not host:
            continue
        port = "22"
        user_host = host
        if ":" in host and "@" in host.split(":")[-1] is False:  # user@h:p
            try:
                user_host, port = host.rsplit(":", 1)
                int(port)
            except Exception:
                user_host, port = host, "22"
        cmd = ["ssh", "-i", "/root/.ssh/id_ed25519", "-p", port,
               "-o", "StrictHostKeyChecking=accept-new",
               "-o", "ConnectTimeout=8",
               "-o", "BatchMode=yes",
               user_host,
               "nvidia-smi --query-gpu=name --format=csv,noheader || hostname"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if r.returncode == 0:
                gpus = [g.strip() for g in (r.stdout or "").splitlines()
                        if g.strip()]
                out.append({"target": host, "ok": True, "gpus": gpus})
            else:
                out.append({"target": host, "ok": False,
                            "error": (r.stderr or r.stdout)[:200]})
        except Exception as e:                          # noqa: BLE001
            out.append({"target": host, "ok": False, "error": str(e)[:200]})
    return {"results": out}


# ════════════════════════════════════════════════════════════════════════
# Paper Mode endpoints (doc 13). Mode flip + proposal + state + decisions
# + sections + versions + reviewer-sim + submit. Research mode is unaware
# of any of these.
# ════════════════════════════════════════════════════════════════════════


@router.get("/mode")
def get_mode(db: Session = Depends(get_session)):
    """Current project mode plus minimal paper meta for the header pill."""
    from . import paper as _paper
    mode = _paper.project_mode()
    meta_row = db.query(PaperMeta).first() if mode == "paper" else None
    return {
        "mode": mode,
        "meta": meta_row.dict() if meta_row else None,
        "days_till_deadline": _paper.days_till_deadline(),
        "budget": _paper.budget_summary() if mode == "paper" else None,
    }


@router.post("/paper/proposal/start")
async def paper_proposal_start():
    """Create a paper_proposal row, kick off council assessments
    asynchronously. Returns immediately with the proposal id."""
    from . import paper as _paper, council as _c
    pid = "pp-" + os.urandom(5).hex()
    db = SessionLocal()
    try:
        db.add(PaperProposal(id=pid, status="in_progress",
                              council_responses={}))
        db.commit()
    finally:
        db.close()
    # Spawn a background thread that runs ONE reviewer per available
    # provider in parallel and aggregates into the proposal row.
    threading.Thread(
        target=_run_paper_proposal_council,
        args=(pid,), daemon=True,
        name=f"paper-proposal-{pid}").start()
    return {"proposal_id": pid, "status": "in_progress"}


def _run_paper_proposal_council(proposal_id: str) -> None:
    from . import council as _c, paper as _paper
    cfg = _c._settings()
    available = _c._available_reviewers(cfg)
    if _c._claude_available(cfg) and "claude" not in available:
        available.append("claude")
    if not available:
        db = SessionLocal()
        try:
            p = db.query(PaperProposal).filter(
                PaperProposal.id == proposal_id).first()
            if p:
                p.status = "ready"
                p.council_responses = {"_no_reviewers":
                    "No council reviewers configured. Add API keys in Settings."}
                db.commit()
        finally:
            db.close()
        return
    # Build the context once
    db = SessionLocal()
    try:
        proj = db.query(Project).first()
        # lessons
        try:
            lessons_path = _paper.paper_folder(db)
            lessons_path = (lessons_path.parent / "lessons.md"
                            if lessons_path else None)
            lessons = lessons_path.read_text(errors="ignore") \
                if lessons_path and lessons_path.exists() else ""
        except Exception:
            lessons = ""
        # frontier
        every = db.query(Run).all()
        frontier_ids = _c._frontier_ids(every)
        kept = [r for r in every if r.id in frontier_ids]
        # aggregate
        agg = _c._aggregate_stats(every, proj) if proj else {}
    finally:
        db.close()
    context = {
        "project": {"name": proj.name if proj else "",
                    "purpose": proj.purpose if proj else "",
                    "metric": proj.validation_metric if proj else "",
                    "direction": proj.metric_direction if proj else ""},
        "lessons_md": (lessons or "")[-8000:],
        "frontier_runs": [{"id": r.id, "name": r.run_name,
                            "metric": r.headline_metric,
                            "config": r.config if isinstance(r.config, dict) else {}}
                           for r in kept[-30:]],
        "aggregate": agg,
    }
    user = ("You are being asked the central question of paper-writing:\n"
            "**Is this research ready to write up?**\n\n"
            "Read everything below and return JSON ONLY matching:\n"
            "{ \"claims\": [{\"title\":..., \"summary\":..., "
            "\"evidence_strength\":\"strong|suggestive|anecdotal\"}], "
            "\"novelty\":\"high|medium|low|unclear\","
            "\"novelty_rationale\":\"...\","
            "\"red_flags\": [\"...\"],"
            "\"recommendation\":\"proceed_to_paper|keep_researching|pivot\","
            "\"rationale_md\":\"...\" }\n\nBe honest. Reviewers will see "
            "through hype. If novelty is unclear, say so.\n\n"
            "=== PROJECT CONTEXT ===\n"
            + json.dumps(context, indent=2, default=str))
    results: dict[str, dict] = {}
    threads = []
    def _one(rev):
        out = _c._call_reviewer(
            rev,
            "You are a senior reviewer for an ML conference (NeurIPS-tier). "
            "Assess whether the project below is ready to write up as a paper.",
            user, cfg)
        if out is not None:
            results[rev] = out
    for rev in available:
        t = threading.Thread(target=_one, args=(rev,), daemon=True)
        t.start(); threads.append(t)
    for t in threads:
        t.join(timeout=300)
    db = SessionLocal()
    try:
        p = db.query(PaperProposal).filter(
            PaperProposal.id == proposal_id).first()
        if p:
            p.status = "ready"
            p.council_responses = results
            db.commit()
    finally:
        db.close()
    try:
        bus.publish("paper", "proposal_ready", {"id": proposal_id})
    except Exception:
        pass


@router.get("/paper/proposal/{pid}")
def paper_proposal_get(pid: str, db: Session = Depends(get_session)):
    p = db.query(PaperProposal).filter(PaperProposal.id == pid).first()
    return p.dict() if p else {}


@router.get("/paper/proposal/latest")
def paper_proposal_latest(db: Session = Depends(get_session)):
    p = (db.query(PaperProposal).order_by(
         PaperProposal.created_at.desc()).first())
    return p.dict() if p else {}


@router.post("/paper/enter")
async def paper_enter(request: Request):
    """Flip to paper mode. Body: {meta: {venue, deadline_iso, authors, ...},
    proposal_id}. Spawns Author Agent + Paper Runner + writes mode_history."""
    from . import paper as _paper
    from . import author_agent
    from . import paper_runner
    body = await request.json()
    meta = body.get("meta") or {}
    proposal_id = body.get("proposal_id") or ""
    if _paper.project_mode() == "paper":
        return {"status": "already_in_paper_mode"}
    db = SessionLocal()
    try:
        # write PaperMeta
        m = db.query(PaperMeta).first()
        if not m:
            m = PaperMeta(id="pm-" + os.urandom(4).hex(),
                          venue=meta.get("venue") or "NeurIPS 2026",
                          style_id=meta.get("style_id") or "neurips_2025",
                          deadline_iso=meta.get("deadline_iso") or "",
                          anonymize=bool(meta.get("anonymize", True)),
                          authors_json=meta.get("authors") or [],
                          gpu_budget_hours=float(meta.get("gpu_budget_hours")
                                                  or 800),
                          llm_budget_daily_usd=float(
                              meta.get("llm_budget_daily_usd") or 20),
                          title_preference=meta.get("title_preference")
                                          or "auto",
                          phase="scaffold")
            db.add(m)
        else:
            for k, v in meta.items():
                if v is None:
                    continue
                if hasattr(m, k):
                    setattr(m, k, v)
            m.phase = "scaffold"
        # mode_history snapshot of current state
        db.add(ModeHistory(
            id="mh-" + os.urandom(4).hex(),
            from_mode="research", to_mode="paper",
            reason_md="user accepted paper proposal",
            snapshot_json={"proposal_id": proposal_id}))
        db.commit()
    finally:
        db.close()
    _paper.set_project_mode("paper")
    # Spawn agent + runner. Author Agent reads the proposal.
    ar = author_agent.start(proposal_id=proposal_id)
    paper_runner.start()
    return {"status": "entered_paper_mode", "author_agent": ar}


@router.post("/paper/revert")
async def paper_revert(request: Request):
    """Flip back to research mode. Body: {reason}. Kills Author Agent,
    pauses paper_runs, captures Paper Snapshot."""
    from . import paper as _paper
    from . import author_agent
    body = await request.json()
    reason = (body.get("reason") or "").strip()
    if not reason or len(reason) < 5:
        return {"status": "error",
                "detail": "reason required (1+ sentence)"}
    snap = _paper.take_snapshot()
    db = SessionLocal()
    try:
        db.add(ModeHistory(
            id="mh-" + os.urandom(4).hex(),
            from_mode="paper", to_mode="research",
            reason_md=reason, snapshot_json=snap))
        # Pause running paper_runs
        for r in db.query(Run).filter(
                Run.context == "paper",
                Run.status.in_(("running", "queued"))).all():
            if r.status == "running":
                r.status = "paused"
        meta = db.query(PaperMeta).first()
        if meta:
            meta.phase = "archived"
        db.commit()
    finally:
        db.close()
    author_agent.stop()
    _paper.set_project_mode("research")
    bus.publish("paper", "mode_reverted", {"reason": reason})
    return {"status": "reverted"}


@router.get("/paper/state")
def paper_state(db: Session = Depends(get_session)):
    """Single payload: meta, claims, figures, paper_runs, sections,
    decisions(pending), versions, citations, budget, build_status."""
    from . import paper as _paper, paper_compile
    meta = db.query(PaperMeta).first()
    claims = [c.dict() for c in db.query(PaperClaim).order_by(
        PaperClaim.idx).all()]
    figures = [f.dict() for f in db.query(PaperFigure).all()]
    paper_runs = [r.dict() for r in db.query(Run).filter(
        Run.context == "paper").all()]
    sections = [s.dict() for s in db.query(PaperSection).all()]
    decisions = [d.dict() for d in db.query(PaperDecision).filter(
        PaperDecision.status == "pending").order_by(
        PaperDecision.priority.desc(),
        PaperDecision.created_at.asc()).all()]
    versions = [v.dict() for v in db.query(PaperVersion).order_by(
        PaperVersion.created_at.desc()).all()]
    citations = [c.dict() for c in db.query(PaperCitation).all()]
    return {
        "mode": _paper.project_mode(),
        "meta": meta.dict() if meta else None,
        "claims": claims, "figures": figures, "paper_runs": paper_runs,
        "sections": sections, "decisions": decisions,
        "versions": versions, "citations": citations,
        "budget": _paper.budget_summary(),
        "build_status": paper_compile.status(),
        "days_till_deadline": _paper.days_till_deadline(),
    }


@router.get("/paper/today")
def paper_today(db: Session = Depends(get_session)):
    """The Today view's content (more compact than /paper/state)."""
    from . import paper as _paper, paper_compile
    decisions = [d.dict() for d in db.query(PaperDecision).filter(
        PaperDecision.status == "pending").order_by(
        PaperDecision.priority.desc(),
        PaperDecision.created_at.asc()).limit(20).all()]
    running = [r.dict() for r in db.query(Run).filter(
        Run.context == "paper",
        Run.status == "running").all()]
    sections = [s.dict() for s in db.query(PaperSection).all()]
    folder = _paper.paper_folder(db)
    commits = _paper.list_commits(folder, limit=8) if folder else []
    # last-N completed paper_runs for the "overnight" line
    recent = (db.query(Run).filter(
        Run.context == "paper",
        Run.status.in_(("kept", "success", "done", "crashed", "failed")))
        .order_by(Run.ended_at.desc()).limit(15).all())
    return {
        "mode": _paper.project_mode(),
        "decisions": decisions,
        "running_runs": running,
        "recent_runs": [r.dict() for r in recent],
        "sections": sections,
        "commits": commits,
        "budget": _paper.budget_summary(),
        "days_till_deadline": _paper.days_till_deadline(),
        "build_status": paper_compile.status(),
    }


@router.get("/paper/decisions")
def paper_decisions(status: str = "pending",
                    db: Session = Depends(get_session)):
    q = db.query(PaperDecision)
    if status and status != "all":
        q = q.filter(PaperDecision.status == status)
    rows = q.order_by(PaperDecision.priority.desc(),
                      PaperDecision.created_at.asc()).all()
    return {"decisions": [d.dict() for d in rows]}


@router.post("/paper/decisions/{did}/resolve")
async def paper_decision_resolve(did: str, request: Request):
    from . import paper as _paper
    body = await request.json()
    action = body.get("action") or "approve"
    note = body.get("note") or ""
    ok = _paper.resolve_decision(did, action=action, note=note)
    return {"ok": bool(ok)}


@router.post("/paper/recompile")
async def paper_recompile(request: Request):
    from . import paper_compile
    body = await request.json() if request.headers.get("content-length") else {}
    force = bool(body.get("force"))
    status = paper_compile.build(force=force)
    return status


@router.get("/paper/pdf")
def paper_pdf():
    from . import paper_compile
    data = paper_compile.pdf_bytes()
    if not data:
        return {"ok": False, "detail": "no pdf yet"}
    from fastapi.responses import Response
    return Response(content=data, media_type="application/pdf")


@router.get("/paper/build_log")
def paper_build_log():
    from . import paper_compile
    return paper_compile.status()


@router.get("/paper/tex")
def paper_tex(db: Session = Depends(get_session)):
    """Concatenated main.tex + sections/*.tex for the LaTeX viewer.
    Marks per-file boundaries so the frontend can split them."""
    from . import paper as _paper
    folder = _paper.paper_folder(db)
    if not folder or not (folder / "main.tex").exists():
        return {"files": []}
    files = []
    for p in [folder / "main.tex"] + sorted(folder.glob("sections/*.tex")):
        try:
            text = p.read_text(errors="ignore")
        except OSError:
            text = ""
        files.append({"path": str(p.relative_to(folder)), "content": text,
                       "user_owned": p.name.endswith(".user.tex")})
    return {"files": files}


@router.post("/paper/section/save")
async def paper_section_save(request: Request):
    """Persist a *.user.tex override file (the only kind of LaTeX edit
    we accept from the user in v1)."""
    from . import paper as _paper
    body = await request.json()
    path = body.get("path") or ""
    content = body.get("content") or ""
    if not path.endswith(".user.tex"):
        return {"ok": False, "detail":
                "only *.user.tex files are user-editable in v1"}
    folder = _paper.paper_folder()
    if not folder:
        return {"ok": False, "detail": "no paper folder"}
    target = (folder / path).resolve()
    if not str(target).startswith(str(folder.resolve())):
        return {"ok": False, "detail": "path traversal"}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    _paper.commit_paper_changes(folder, f"user edit: {path}",
                                author="Researcher")
    return {"ok": True}


@router.post("/paper/lit/search")
async def paper_lit_search(request: Request):
    from . import lit_agent
    body = await request.json()
    q = (body.get("query") or "").strip()
    if not q:
        return {"results": []}
    return {"results": lit_agent.search(q, limit=int(body.get("limit", 15)))}


@router.post("/paper/lit/auto_discover")
async def paper_lit_auto():
    from . import lit_agent
    n = lit_agent.auto_discover_for_claims(max_per_claim=5)
    return {"filed": n}


@router.get("/paper/versions")
def paper_versions(db: Session = Depends(get_session)):
    rows = db.query(PaperVersion).order_by(
        PaperVersion.created_at.desc()).all()
    return {"versions": [v.dict() for v in rows]}


@router.post("/paper/versions/pin")
async def paper_versions_pin(request: Request):
    from . import paper as _paper
    body = await request.json()
    label = (body.get("label") or "").strip() or \
        ("v-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M"))
    snap = _paper.take_snapshot()
    folder = _paper.paper_folder()
    sha = (_paper._run_git(folder, "rev-parse", "HEAD") if folder
            and (folder / ".git").exists() else "")
    vid = "pv-" + os.urandom(4).hex()
    db = SessionLocal()
    try:
        db.add(PaperVersion(
            id=vid, label=label, latex_commit_sha=sha,
            snapshot_json=snap, claims_summary_md=""))
        db.commit()
    finally:
        db.close()
    return {"id": vid, "label": label, "sha": sha}


@router.post("/paper/reviewer_sim/run")
async def paper_reviewer_sim_run():
    """Council role-plays venue reviewers on the current paper. Returns
    immediately; results land in paper_review_sim rows asynchronously."""
    threading.Thread(target=_run_reviewer_sim, daemon=True,
                     name="reviewer-sim").start()
    return {"status": "started"}


def _run_reviewer_sim():
    """Each council reviewer reads the current paper and emits a fake
    review with suggested defensive ablations as decisions."""
    from . import council as _c, paper as _paper, paper_compile
    cfg = _c._settings()
    available = _c._available_reviewers(cfg)
    if not available:
        return
    folder = _paper.paper_folder()
    if not folder:
        return
    # Read the current paper.
    tex = ""
    for p in [folder / "main.tex"] + sorted(folder.glob("sections/*.tex")):
        try:
            tex += f"\n\n% === {p.name} ===\n" + p.read_text(errors="ignore")
        except OSError:
            pass
    if not tex.strip():
        return
    prompt = (
        "You are a strict, skeptical NeurIPS-tier reviewer. Read the paper "
        "below and write your honest review. Find weaknesses, missing "
        "experiments, and unconvincing claims. Output JSON ONLY:\n"
        "{\"strengths\":[\"...\"], \"weaknesses\":[\"...\"], "
        "\"questions\":[\"...\"], \"score\":1-10, "
        "\"suggested_ablations\":[{\"title\":\"\",\"why\":\"\","
        "\"est_gpu_hours\":0,\"target_claim\":\"\"}]}\n\n=== PAPER ===\n"
        + tex[:60000])
    for rev in available:
        out = _c._call_reviewer(
            rev,
            "You are a strict NeurIPS reviewer. Be honest, not polite.",
            prompt, cfg)
        if not out:
            continue
        sid = "rs-" + os.urandom(4).hex()
        db = SessionLocal()
        try:
            db.add(PaperReviewSim(
                id=sid, model=rev,
                content_md=json.dumps(out, indent=2),
                suggested_decisions_json=out.get("suggested_ablations", [])))
            db.commit()
        finally:
            db.close()
        # File a decision per suggested ablation
        for sa in (out.get("suggested_ablations") or [])[:5]:
            _paper.file_decision(
                source="reviewer_sim", kind="add_ablation",
                title=f"[{rev}] Add ablation: {sa.get('title','')[:80]}",
                body_md=(f"**Why (reviewer):** {sa.get('why','')}\n\n"
                         f"**Targets claim:** {sa.get('target_claim','')}\n\n"
                         f"**Est GPU-hours:** {sa.get('est_gpu_hours','?')}"),
                default_action="approve",
                priority=8)
    bus.publish("paper", "reviewer_sim_finished", {})


@router.get("/lessons")
def lessons():
    """Parse workspace/<repo>/lessons.md (auto-written by the council) into a
    structured list: each entry has ts, reviewer, supporting_run, text, and
    any other run-ids the council mentioned in the body (evidence)."""
    from . import council as _c
    p = _c._lessons_path()
    if not p or not p.exists():
        return {"lessons": [], "path": str(p) if p else ""}
    try:
        text = p.read_text(errors="ignore")
    except OSError:
        return {"lessons": [], "path": str(p)}
    # Line format we write:
    #   - [YYYY-MM-DD HH:MM · <reviewer> on <run_name>] <text>
    pat = re.compile(
        r"^-\s*\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s*·\s*"
        r"(?P<rev>[^·\]]+?)\s*on\s+(?P<run>[^\]]+?)\]\s*(?P<text>.*)$")
    # Build the set of known run_ids so we can pull "evidence" mentions out
    # of the lesson body.
    db = SessionLocal()
    try:
        known = {r.id for r in db.query(Run).all()}
        run_names = {r.run_name for r in db.query(Run).all() if r.run_name}
    finally:
        db.close()
    known_all = known | run_names
    out = []
    for ln in text.splitlines():
        m = pat.match(ln.strip())
        if not m:
            continue
        body = m.group("text").strip()
        # find any other run ids mentioned in the body (evidence beyond the
        # primary supporting run)
        evidence = []
        for name in sorted(known_all, key=lambda s: -len(s)):
            if name and name in body and name != m.group("run"):
                evidence.append(name)
                if len(evidence) >= 6:
                    break
        out.append({
            "ts": m.group("ts"),
            "reviewer": m.group("rev").strip(),
            "supporting_run": m.group("run").strip(),
            "text": body,
            "evidence": evidence,
        })
    return {"lessons": out, "path": str(p), "count": len(out)}


@router.get("/authkeys/pubkey")
def authkeys_pubkey():
    """This node's SSH public key (generated on first call if missing) — so
    the user can paste it into another GPU server's authorized_keys to attach
    that server as an additional GPU node."""
    return authkeys.local_pubkey()


@router.get("/authkeys")
def authkeys_list(db: Session = Depends(get_session)):
    """Authorized keys + a real SSH connect command for this node.
    Auto-detects user / public IP / sshd port; Settings overrides win
    (node_ssh_user, node_ssh_host, node_ssh_port)."""
    info = authkeys.detect_ssh_info()
    row = db.query(Setting).filter(Setting.key == "onboarding").first()
    cfg = dict(row.value) if row and isinstance(row.value, dict) else {}
    user = (cfg.get("node_ssh_user") or info.get("user") or "root").strip()
    host = (cfg.get("node_ssh_host") or info.get("host") or "").strip()
    port = str(cfg.get("node_ssh_port") or info.get("port") or "22").strip()
    overridden = bool(cfg.get("node_ssh_port"))
    hint = ""
    if host and port == "22" and not overridden:
        hint = (" — note: this is the INTERNAL sshd port. On RunPod / "
                "vast.ai the public port is usually a high port (e.g. "
                "22149); set node_ssh_port in Settings to override.")
    cmd = (f"ssh {user}@{host} -p {port}" if host
           else f"ssh {user}@<node-ip> -p {port}  (set node_ssh_host in "
                "Settings — public IP auto-detect failed)")
    return {"keys": authkeys.list_keys(), "ssh": cmd, "ssh_hint": hint,
            "ssh_user": user, "ssh_host": host, "ssh_port": port,
            "ssh_detected": info}


@router.post("/authkeys")
async def authkeys_add(request: Request):
    body = await request.json()
    return authkeys.add_key(body.get("key", ""))


@router.post("/authkeys/delete")
async def authkeys_delete(request: Request):
    body = await request.json()
    return authkeys.delete_key(body.get("fingerprint", ""))


# ──────────── notifications: config + test ───────────────────────────────────

_NOTIFY_KEYS = ("email", "cadence", "email_recipients", "gmail_app_pw",
                "dashboard_url", "resend_api_key", "notify_from",
                "smtp_host", "smtp_port", "smtp_user", "smtp_pass")


@router.post("/notify/config")
async def notify_config(request: Request):
    """Merge email / cadence / transport settings into the onboarding config
    without re-running onboarding (so live runs can enable email)."""
    body = await request.json()
    db = SessionLocal()
    row = db.query(Setting).filter(Setting.key == "onboarding").first()
    cfg = dict(row.value) if row and isinstance(row.value, dict) else {}
    for k in _NOTIFY_KEYS:
        if k in body:
            cfg[k] = body[k]
    if row:
        row.value = cfg
    else:
        db.add(Setting(key="onboarding", value=cfg))
    db.commit()
    db.close()
    return {"status": "ok", "cadence": cfg.get("cadence", "off")}


@router.post("/notify/test")
async def notify_test(request: Request):
    """Send a one-off email (or a full digest) to confirm delivery works."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if body.get("digest"):
        ok = notify.send_digest_now()
    else:
        ok = notify.send(
            "autoresearcherUI - test email",
            "This is a test from autoresearcherUI. If you can read this, "
            "email notifications are working.\n\n- autoresearcherUI")
    return {"sent": ok}


# ──────────── archive & restore ──────────────────────────────────────────────

@router.get("/archive/info")
def archive_info():
    """Sizes of the research state — drives the Archive modal."""
    return archive.info()


@router.get("/archive")
def archive_download(profile: str = "full"):
    """Stream the whole research state as a .tar.gz (full or slim)."""
    fname = archive.archive_filename(profile)
    return StreamingResponse(
        archive.stream(profile), media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@router.post("/archive/email")
async def archive_email():
    """Email the user how to grab their archive (the tarball itself is far too
    big to attach — email carries the instructions, not the gigabytes)."""
    info = archive.info()
    body = (
        "Your autoresearcherUI research archive is ready to pull off this "
        "server.\n\n"
        f"Full state:  {info['full_bytes'] / 1e6:.0f} MB\n"
        f"Slim state:  {info['slim_bytes'] / 1e6:.0f} MB "
        f"(no checkpoints / datasets)\n\n"
        "Two ways to get it:\n\n"
        "1. Dashboard — click Archive, then Download (full or slim).\n\n"
        "2. Server-to-server (best for large state) — run this on the NEW "
        "server:\n"
        f"   {info['rsync']}\n\n"
        "On the new server: install autoresearcherUI and, on the onboarding "
        "screen, choose 'Resume from archive' — upload the tarball (or point "
        "it at the rsync'd data/ folder). The agent picks the research back "
        "up where it left off.\n\n- autoresearcherUI")
    ok = notify.send("autoresearcherUI — your research archive is ready", body)
    return {"sent": ok}


@router.post("/restore")
async def restore(file: UploadFile = File(...)):
    """Onboarding: upload a research archive, restore it, resume the agent."""
    tmp = DATA_DIR / "_upload.tar.gz"
    with open(tmp, "wb") as fh:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            fh.write(chunk)
    try:
        result = archive.restore(str(tmp))
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    db = SessionLocal()
    row = db.query(Setting).filter(Setting.key == "onboarding").first()
    cfg = dict(row.value) if row and isinstance(row.value, dict) else {}
    db.close()
    if cfg.get("claude_token") or os.environ.get("ARUI_CLAUDE_BIN"):
        try:
            realrun.start_real(cfg, resume=True)
            result["agent"] = "resumed"
        except Exception as e:                       # noqa: BLE001
            result["agent"] = f"not resumed: {e}"
    return result
