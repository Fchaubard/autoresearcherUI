"""Paper Runner — the daemon that schedules paper_run rows.

Per the v3 spec, the Author Agent ONLY plans and writes; the Paper
Runner reads `Run` rows with context='paper' and status='queued', resolves
dependencies, bin-packs against the GPU table, and launches them in
tmux. It also flips integration_status when a run completes (the
figure-renderer then picks it up via the existing run-finished hooks).

v1 ships the `local` backend only. The class is structured so SLURM /
K8s / Ray plugins can drop in later.
"""
from __future__ import annotations

import datetime as dt
import os
import subprocess
import threading
import time
from pathlib import Path

from . import paper
from .bus import bus
from .config import DATA_DIR
from .db import SessionLocal
from .models import Gpu, Run, Setting

_STARTED = False
_LOCK = threading.Lock()
_POLL_SEC = 8


def _iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _settings() -> dict:
    """Reads onboarding settings + paper-runner specific overrides."""
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(Setting.key == "onboarding").first()
        out = dict(row.value) if row and isinstance(row.value, dict) else {}
        return out
    finally:
        db.close()


def start() -> None:
    """Spawn the once-per-process scheduler thread."""
    global _STARTED
    with _LOCK:
        if _STARTED:
            return
        _STARTED = True
    threading.Thread(target=_loop, daemon=True, name="paper-runner").start()
    print("[paper-runner] scheduler started", flush=True)


def _loop() -> None:
    """Poll loop. Only acts when project_mode == 'paper'."""
    while True:
        try:
            if paper.project_mode() == "paper":
                _tick()
        except Exception as e:                          # noqa: BLE001
            print(f"[paper-runner] tick error: {e}", flush=True)
        time.sleep(_POLL_SEC)


def _tick() -> None:
    """One scheduling pass.

    NOTE on the operator gate (2026-06-05 paper rebuild): the gate is
    enforced via ``Run.status`` itself — the Author Agent creates ablation
    runs with status='proposed' and ``paper_phase.approve_plan`` flips
    them to 'queued' atomically with the gate. This loop only acts on
    'queued' rows, so no explicit gate check is needed here.
    """
    db = SessionLocal()
    try:
        # 1. Find ready-to-run paper_runs (queued + all dependencies done).
        candidates = db.query(Run).filter(
            Run.context == "paper",
            Run.status == "queued").all()
        ready = []
        completed_ids = {r.id for r in db.query(Run).filter(
            Run.context == "paper",
            Run.status.in_(("done", "kept", "success"))).all()}
        for r in candidates:
            deps = r.depends_on if isinstance(r.depends_on, list) else []
            if all(d in completed_ids for d in deps):
                ready.append(r)
        if not ready:
            return

        # 2. Find free GPUs.
        gpus = db.query(Gpu).order_by(Gpu.index).all()
        busy_ids = {r.gpu_index for r in db.query(Run).filter(
            Run.status == "running").all() if r.gpu_index is not None
            and r.gpu_index >= 0}
        free_gpu_idxs = [g.index for g in gpus if g.index not in busy_ids
                         and (g.util_pct or 0) < 5
                         and (g.vram_used_mb or 0) < 600]

        # 3. Bin-pack: assign as many ready runs as fit.
        for r in ready:
            need = max(1, int(r.gpus_required or 1))
            if len(free_gpu_idxs) < need:
                break
            assigned = free_gpu_idxs[:need]
            free_gpu_idxs = free_gpu_idxs[need:]
            _launch_run(db, r, assigned[0])  # multi-GPU launching uses primary index

    finally:
        db.close()


def _launch_run(db, run: Run, gpu_idx: int) -> None:
    """Launch a paper_run in a tmux session. The run command is
    expected to live in the Author Agent's plan — config['cmd'].
    For v1 we just shell out with that command and let the Author
    Agent generate it. If no cmd is set, mark as failed with a note."""
    cfg = run.config if isinstance(run.config, dict) else {}
    cmd = cfg.get("cmd")
    if not cmd:
        run.status = "failed"
        run.ended_at = _iso()
        bus.publish("paper", "run_failed",
                    {"run_id": run.id, "reason": "no cmd"})
        db.commit()
        return
    run.gpu_index = gpu_idx
    run.status = "running"
    run.started_at = _iso()
    run.tmux_session = run.id
    db.commit()
    folder = paper.paper_folder(db) or DATA_DIR
    try:
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", run.id,
             f"cd {folder} && CUDA_VISIBLE_DEVICES={gpu_idx} {cmd} 2>&1"],
            capture_output=True, timeout=10)
        # Proactively wire pane_stream so the user can click the run's
        # tab in the Sessions rail and instantly see live bytes — no
        # /attach round-trip lag. tmux pipe-pane is cheap and idempotent.
        try:
            from . import pane_stream
            pane_stream.enable(run.id)
        except Exception as e:                              # noqa: BLE001
            print(f"[paper-runner] pane_stream.enable({run.id}) failed: {e}",
                  flush=True)
        bus.publish("paper", "run_started",
                    {"run_id": run.id, "gpu": gpu_idx})
        print(f"[paper-runner] launched {run.id} on GPU {gpu_idx}",
              flush=True)
    except Exception as e:                              # noqa: BLE001
        print(f"[paper-runner] launch failed for {run.id}: {e}", flush=True)
        run.status = "failed"; run.ended_at = _iso()
        db.commit()


# ── public helpers used by api.py ────────────────────────────────────────


def queue_run(*, claim_id: str = "", figure_id: str = "",
              role: str = "ablation", task_type: str = "compute",
              cmd: str = "", dataset: str = "", model: str = "",
              hpps: dict | None = None,
              n_seeds: int = 1, gpus_required: int = 1,
              est_time_sec: int = 0,
              depends_on: list[str] | None = None,
              compare_to_run_id: str = "",
              compare_to_baseline_id: str = "") -> str:
    """Append a row to the paper_run queue. Returns the run id."""
    rid = "pr-" + os.urandom(5).hex()
    db = SessionLocal()
    try:
        cfg = {"cmd": cmd, "dataset": dataset, "model": model,
               "hpps": hpps or {}}
        db.add(Run(
            id=rid, run_name=f"{role}-{rid[:8]}",
            context="paper", paper_claim_id=claim_id,
            paper_figure_id=figure_id, paper_role=role, task_type=task_type,
            config=cfg, status="queued",
            n_seeds=int(n_seeds), gpus_required=int(gpus_required),
            est_time_sec=int(est_time_sec),
            depends_on=depends_on or [],
            compare_to_run_id=compare_to_run_id,
            compare_to_baseline_id=compare_to_baseline_id))
        db.commit()
    finally:
        db.close()
    return rid


def update_run_status(run_id: str, status: str, headline_metric: float | None
                      = None) -> bool:
    """Called by the existing track/finish ingest when a paper-context
    run finishes. We update integration_status to 'stale' so the figure
    renderer knows to re-render."""
    db = SessionLocal()
    try:
        r = db.query(Run).filter(Run.id == run_id,
                                  Run.context == "paper").first()
        if not r:
            return False
        r.status = status
        if headline_metric is not None:
            r.headline_metric = headline_metric
        if status in ("kept", "success", "done"):
            r.integration_status = "stale"   # figures linking this run need rerender
        r.ended_at = _iso()
        db.commit()
    finally:
        db.close()
    bus.publish("paper", "run_status_changed",
                {"run_id": run_id, "status": status})
    return True
