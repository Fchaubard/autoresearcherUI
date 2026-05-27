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
from .models import (ChatMessage, Event, Gpu, Idea, JournalEntry, Project,
                     Run, Setting)

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


@router.post("/track/log")
async def track_log(request: Request):
    body = await request.json()
    run_id = body["run_id"]
    points = body.get("points", [])
    metrics.append(run_id, points)
    bus.publish("metrics", "metric", {"run_id": run_id, "points": points})
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
        # council deliberation: one external LLM reviews this run, reranks
        # the queue and proposes new ideas (only if API keys are configured).
        from . import council
        council.review_async(run_id)
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


@router.get("/authkeys/pubkey")
def authkeys_pubkey():
    """This node's SSH public key (generated on first call if missing) — so
    the user can paste it into another GPU server's authorized_keys to attach
    that server as an additional GPU node."""
    return authkeys.local_pubkey()


@router.get("/authkeys")
def authkeys_list():
    return {"keys": authkeys.list_keys(),
            "ssh": "ssh root@<node-ip> -p <ssh-port>"}


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
