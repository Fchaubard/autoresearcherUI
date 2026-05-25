"""Live demo simulator (scaffold only).

Drives the 'running' runs forward in realtime so the dashboard streams live
charts, GPU telemetry, and timeline events. When a run finishes it promotes the
next queued idea onto the freed GPU - a working preview of the autonomous loop
the real engine (doc 05) will perform. Disabled when ARUI_DEMO=0.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import random
import time

from . import metrics
from .bus import bus
from .db import SessionLocal
from .models import Event, Gpu, Idea, Run

_rng = random.Random(13)
TICK = 1.3            # seconds between simulated steps
FINISH_STEP = 460     # a run completes here, then a queued idea takes its GPU


def _iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _ev(db, type_, severity, actor, message, run_id="", idea_id=""):
    e = Event(id=f"ev-{_rng.randrange(16**8):08x}", type=type_,
              severity=severity, actor=actor, message=message,
              run_id=run_id, idea_id=idea_id, created_at=_iso())
    db.add(e)
    bus.publish("events", "event", e.dict())


def _init_state() -> dict:
    state: dict = {}
    db = SessionLocal()
    for r in db.query(Run).filter(Run.status == "running").all():
        idea = db.query(Idea).filter(Idea.id == r.idea_id).first()
        ev = idea.ev if idea else 0.25
        state[r.id] = {
            "run_id": r.id,
            "idea_id": r.idea_id,
            "gpu": r.gpu_index,
            "step": metrics.last_step(r.id) + 1,
            "acc": metrics.latest(r.id, "at5_acc") or 0.42,
            "loss": metrics.latest(r.id, "train_loss") or 1.0,
            "target_acc": 0.30 + ev,
            "vram": r.peak_vram_mb or 22000,
        }
    db.close()
    return state


async def simulator() -> None:
    state = _init_state()
    while True:
        await asyncio.sleep(TICK)
        db = SessionLocal()
        try:
            for st in list(state.values()):
                st["step"] += 1
                # gentle drift toward target + noise (a continuing curve)
                st["acc"] += (st["target_acc"] - st["acc"]) * 0.035 \
                    + _rng.gauss(0, 0.006)
                st["acc"] = min(0.99, max(0.0, st["acc"]))
                st["loss"] += (0.55 - st["loss"]) * 0.03 + _rng.gauss(0, 0.03)
                st["loss"] = max(0.05, st["loss"])
                wt = time.time()
                pts = [
                    {"key": "at5_acc", "step": st["step"],
                     "value": st["acc"], "wall_time": wt},
                    {"key": "train_loss", "step": st["step"],
                     "value": st["loss"], "wall_time": wt},
                ]
                metrics.append(st["run_id"], pts)
                bus.publish("metrics", "metric",
                            {"run_id": st["run_id"], "points": pts})

                if st["step"] >= FINISH_STEP:
                    _finish(db, state, st)

            _tick_gpus(db, state)
            db.commit()
        finally:
            db.close()


def _finish(db, state: dict, st: dict) -> None:
    run = db.query(Run).filter(Run.id == st["run_id"]).first()
    idea = db.query(Idea).filter(Idea.id == st["idea_id"]).first()
    delta = st["acc"] - 0.412
    if run:
        run.status = "kept" if delta > 0.01 else "discarded"
        run.headline_metric = round(st["acc"], 3)
        run.baseline_delta = round(delta, 3)
        run.ended_at = _iso()
    if idea:
        idea.status = ("success" if delta > 0.02
                       else "failed" if delta < -0.01 else "unclear")
        idea.ended_at = _iso()
        idea.results_vs_baseline = (
            f"{st['acc']:.3f} at5_acc vs 0.412 baseline "
            f"({'+' if delta >= 0 else ''}{delta:.3f})")
    _ev(db, "run_finished", "info", "agent",
        f"{st['run_id']} finished: {run.status if run else '?'} "
        f"({'+' if delta >= 0 else ''}{delta:.3f} vs baseline).",
        run_id=st["run_id"])
    bus.publish("events", "runs_changed", {})

    gpu = st["gpu"]
    del state[st["run_id"]]

    # promote the highest-EV queued idea onto the freed GPU
    nxt = (db.query(Idea)
           .filter(Idea.status == "not_implemented")
           .order_by(Idea.manual_priority.desc(), Idea.ev.desc())
           .first())
    if nxt:
        nxt.status = "running"
        nxt.started_at = _iso()
        new = Run(id=nxt.idea_id, project_id=nxt.project_id,
                  idea_id=nxt.id, run_name=nxt.idea_id, status="running",
                  gpu_index=gpu, tmux_session=f"train-gpu{gpu}",
                  git_commit=f"{_rng.randrange(16**7):07x}",
                  config={"lr": 1e-4, "n_pert": 100, "batch_size": 1024,
                          "depth": 8, "solver": "spsa"},
                  peak_vram_mb=21000 + _rng.randrange(4000),
                  started_at=_iso(), created_at=_iso())
    db.add(new) if nxt else None
    if nxt:
        state[new.id] = {
            "run_id": new.id, "idea_id": nxt.id, "gpu": gpu, "step": 0,
            "acc": 0.41, "loss": 2.4, "target_acc": 0.30 + nxt.ev,
            "vram": new.peak_vram_mb,
        }
        _ev(db, "run_started", "info", "system",
            f"GPU {gpu} freed - launched {new.id} (next highest-EV idea).",
            run_id=new.id)


def _tick_gpus(db, state: dict) -> None:
    by_gpu = {st["gpu"]: st for st in state.values()}
    payload = []
    for g in db.query(Gpu).order_by(Gpu.index).all():
        st = by_gpu.get(g.index)
        if st:
            g.util_pct = round(min(99.9, 90 + _rng.gauss(0, 4)), 1)
            g.vram_used_mb = round(st["vram"] + _rng.gauss(0, 200), 1)
            g.temp_c = round(66 + _rng.gauss(0, 3), 1)
            g.current_run_id = st["run_id"]
        else:
            g.util_pct = round(max(0, _rng.gauss(3, 2)), 1)
            g.vram_used_mb = round(max(0, _rng.gauss(300, 120)), 1)
            g.temp_c = round(41 + _rng.gauss(0, 2), 1)
            g.current_run_id = ""
        g.sampled_at = _iso()
        payload.append(g.dict())
    bus.publish("gpus", "gpus", {"gpus": payload})
