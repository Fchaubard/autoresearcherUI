"""Disk-space maintenance for autoresearcherUI.

The pod fills up fast. The Author Agent and Research Agent both spool
verbose tmux scrollback into per-run .log files under ``data/run_logs/`` —
and for a long-running researcher these can grow to multiple GB while
contributing nothing scientifically once a run has been judged.

``purge_old_run_logs`` is the user-facing janitor:

  • find runs that finished MORE than ``min_age_days`` days ago, and
  • whose ``headline_metric`` puts them in the BOTTOM half (per the
    project's metric direction), and
  • that are NOT the baseline and NOT currently 'kept'.

For each such run, it deletes ONLY the stdout/stderr log file. The
``Run`` row, its ``headline_metric``, the ``review`` JSON, and any
downstream summary stats stay intact, so the Analysis tab / Lessons tab
keep working. The user gets back GBs of disk in seconds.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import shutil

from .config import DATA_DIR
from .db import SessionLocal
from .models import Project, Run, Setting

_RUN_LOGS = DATA_DIR / "run_logs"


def _repo_name(db) -> str:
    """The on-disk workspace folder name, used to locate per-run artifact
    directories under data/workspace/<repo>/runs/<run_name>."""
    row = db.query(Setting).filter(Setting.key == "onboarding").first()
    if row and isinstance(row.value, dict):
        nm = (row.value.get("repo_name") or "").strip()
        if nm:
            return nm
    proj = db.query(Project).first()
    return (proj.name if proj else "").strip()


def _candidate_paths(run: Run, repo: str) -> list[Path]:
    """All on-disk locations that hold THIS run's bulky artifacts.

    Multiple layouts exist in the wild, depending on how the training
    script names its outputs:

      • data/run_logs/<run_id>.log                  — tmux stdout capture
      • data/workspace/<repo>/runs/<run_name>/      — per-run output dir
      • data/workspace/<repo>/runs/<run_name>.log   — sibling log file
      • data/workspace/<repo>/ckpts/<run_name>.pt   — checkpoint file
      • data/workspace/<repo>/ckpts/<run_name>_*.pt — seed/EMA variants
      • data/workspace/<repo>/logs/<run_name>.log   — alternate log layout
      • data/workspace/<repo>/logs/<run_name>/      — alternate log dir

    We delete only what exists; the Run row + metric_keys + DuckDB stay.
    """
    out: list[Path] = [_RUN_LOGS / f"{run.id}.log"]
    name = (run.run_name or "").strip()
    if not name or not repo:
        return out
    ws = DATA_DIR / "workspace" / repo
    # per-run dirs (some projects use this layout)
    out.append(ws / "runs" / name)
    out.append(ws / "runs" / f"{name}.log")
    out.append(ws / "logs" / name)
    out.append(ws / "logs" / f"{name}.log")
    # checkpoints (the BIG ones — .pt files commonly named after the run)
    ckpts = ws / "ckpts"
    if ckpts.exists():
        # exact match
        out.append(ckpts / f"{name}.pt")
        # seed / EMA / variant suffixes: <name>_seed1.pt, <name>_ema.pt, …
        for p in ckpts.glob(f"{name}_*.pt"):
            out.append(p)
        # alternative extensions
        for ext in (".bin", ".safetensors"):
            cand = ckpts / f"{name}{ext}"
            if cand.exists():
                out.append(cand)
    return out


def _path_size(p: Path) -> int:
    """Recursive size in bytes for a file OR directory; 0 on error."""
    if not p.exists():
        return 0
    if p.is_file():
        try:
            return p.stat().st_size
        except OSError:
            return 0
    total = 0
    for q in p.rglob("*"):
        try:
            if q.is_file():
                total += q.stat().st_size
        except OSError:
            continue
    return total


def _parse_iso(s: str | None):
    if not s:
        return None
    try:
        d = dt.datetime.fromisoformat(s)
    except Exception:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d


def _eligible_runs(db, min_age_days: float, bottom_pct: float) -> list[Run]:
    """Pick runs that have FINISHED at least ``min_age_days`` days ago and
    whose metric puts them in the bottom ``bottom_pct`` percent (worst).
    Skips baselines, the current best, and anything still running."""
    proj = db.query(Project).first()
    maximize = bool(proj and proj.metric_direction == "maximize")
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
        days=float(min_age_days))
    pool: list[tuple[Run, dt.datetime, float | None]] = []
    for r in db.query(Run).all():
        if r.is_baseline:
            continue
        if r.status in ("running", "queued"):
            continue
        ended = _parse_iso(r.ended_at)
        if not ended or ended >= cutoff:
            continue
        pool.append((r, ended, r.headline_metric))
    # Of THIS pool, find the median metric — runs on the worse side are
    # the bottom-50% candidates. Crashed/None-metric runs are always
    # eligible (no point keeping stdout from a diverger).
    metrics = sorted(m for _, _, m in pool if m is not None)
    if not metrics:
        # No completed runs with metrics — everything in the age window is
        # fair game (they're crashes / discards).
        return [r for r, _, _ in pool]
    k = max(1, int(len(metrics) * float(bottom_pct)))
    if maximize:                              # worst = smallest
        threshold = metrics[k - 1] if k <= len(metrics) else metrics[-1]
        is_bottom = lambda m: m is None or m <= threshold
    else:                                     # worst = largest
        threshold = metrics[-k] if k <= len(metrics) else metrics[0]
        is_bottom = lambda m: m is None or m >= threshold
    # Identify the GLOBAL project best (across ALL runs, not just the
    # in-window pool) — never delete the all-time-best run's artifacts
    # even if it happens to be old.
    all_metrics = [r.headline_metric for r in db.query(Run).all()
                    if r.headline_metric is not None
                    and r.status not in ("crashed", "failed", "queued")]
    if all_metrics:
        global_best = (max if maximize else min)(all_metrics)
    else:
        global_best = None
    out: list[Run] = []
    for r, _, m in pool:
        if global_best is not None and m is not None and m == global_best:
            continue
        if is_bottom(m):
            out.append(r)
    return out


def preview(min_age_days: float = 2.0, bottom_pct: float = 0.5) -> dict:
    """Same selection as :func:`purge_old_run_logs` but doesn't delete.
    Used by the Settings UI for a confirmation modal."""
    db = SessionLocal()
    try:
        runs = _eligible_runs(db, min_age_days, bottom_pct)
        repo = _repo_name(db)
    finally:
        db.close()
    rows = []
    total_bytes = 0
    for r in runs:
        paths = _candidate_paths(r, repo)
        size = sum(_path_size(p) for p in paths)
        total_bytes += size
        rows.append({"id": r.id, "name": r.run_name or r.id,
                     "status": r.status,
                     "headline_metric": r.headline_metric,
                     "ended_at": r.ended_at,
                     "log_bytes": size,
                     "log_exists": any(p.exists() for p in paths)})
    return {"eligible": len(rows),
            "bytes_freeable": total_bytes,
            "runs": rows}


def purge_old_run_logs(min_age_days: float = 2.0,
                       bottom_pct: float = 0.5) -> dict:
    """Delete on-disk artifacts (stdout logs + checkpoint directories)
    for runs that finished &gt; ``min_age_days`` ago AND scored in the
    bottom ``bottom_pct`` per the project's metric direction.

    The Run row, headline_metric, council review, and DuckDB metric
    time-series are KEPT. Only the bulky on-disk artifacts disappear,
    so the Analysis/Lessons UI keeps working but disk pressure drops."""
    db = SessionLocal()
    try:
        runs = _eligible_runs(db, min_age_days, bottom_pct)
        repo = _repo_name(db)
    finally:
        db.close()
    deleted = 0
    bytes_freed = 0
    missing = 0
    sample_names: list[str] = []
    for r in runs:
        any_hit = False
        for path in _candidate_paths(r, repo):
            if not path.exists():
                continue
            size = _path_size(path)
            try:
                if path.is_file():
                    path.unlink()
                else:
                    shutil.rmtree(path, ignore_errors=False)
                bytes_freed += size
                any_hit = True
            except OSError as e:
                print(f"[maintenance] remove {path} failed: {e}", flush=True)
        if any_hit:
            deleted += 1
            if len(sample_names) < 6:
                sample_names.append(r.run_name or r.id)
        else:
            missing += 1
    return {"deleted": deleted, "bytes_freed": bytes_freed,
            "kept": missing,                # nothing on disk to delete
            "eligible": len(runs),
            "sample": sample_names}


# ── keep-SOTA-only: aggressive checkpoint purge ───────────────────────────


def _sota_run_ids(db) -> set[str]:
    """The run id(s) we MUST keep — the best-metric run (per the project's
    direction) and any baseline. Returns a set so a tie keeps every tied
    run. Always conservative: if no metric exists, keeps everything."""
    proj = db.query(Project).first()
    maximize = bool(proj and proj.metric_direction == "maximize")
    candidates = []
    for r in db.query(Run).all():
        if r.headline_metric is None:
            continue
        if r.status in ("crashed", "failed", "queued", "running"):
            continue
        candidates.append(r)
    if not candidates:
        return set()
    best = (max if maximize else min)(
        candidates, key=lambda r: r.headline_metric).headline_metric
    keep: set[str] = set()
    for r in db.query(Run).all():
        if r.is_baseline:
            keep.add(r.id)
        if (r.headline_metric is not None
                and r.headline_metric == best):
            keep.add(r.id)
        # Don't ever wipe in-flight or queued runs' artifacts
        if r.status in ("running", "queued"):
            keep.add(r.id)
    return keep


def preview_keep_sota_only() -> dict:
    """What would be deleted by :func:`purge_keep_sota_only`."""
    db = SessionLocal()
    try:
        keep_ids = _sota_run_ids(db)
        all_runs = db.query(Run).all()
        repo = _repo_name(db)
    finally:
        db.close()
    rows = []
    total = 0
    eligible: list[Run] = []
    for r in all_runs:
        if r.id in keep_ids:
            continue
        if r.is_baseline:
            continue
        eligible.append(r)
        sz = sum(_path_size(p) for p in _candidate_paths(r, repo))
        total += sz
        rows.append({"id": r.id, "name": r.run_name or r.id,
                     "status": r.status,
                     "headline_metric": r.headline_metric,
                     "log_bytes": sz, "log_exists": sz > 0})
    rows.sort(key=lambda r: -r["log_bytes"])
    return {"eligible": len(eligible), "bytes_freeable": total,
            "kept_run_ids": sorted(keep_ids),
            "runs": rows}


def purge_keep_sota_only() -> dict:
    """Delete on-disk artifacts (logs, checkpoints, output dirs) for EVERY
    completed run except the project-best (SOTA). Run rows + metrics +
    reviews are KEPT — so Analysis / Lessons keep working — but the
    bulky on-disk state goes away."""
    db = SessionLocal()
    try:
        keep_ids = _sota_run_ids(db)
        all_runs = db.query(Run).all()
        repo = _repo_name(db)
    finally:
        db.close()
    deleted = 0
    bytes_freed = 0
    missing = 0
    for r in all_runs:
        if r.id in keep_ids or r.is_baseline:
            continue
        any_hit = False
        for path in _candidate_paths(r, repo):
            if not path.exists():
                continue
            sz = _path_size(path)
            try:
                if path.is_file():
                    path.unlink()
                else:
                    shutil.rmtree(path, ignore_errors=False)
                bytes_freed += sz
                any_hit = True
            except OSError as e:
                print(f"[maintenance] remove {path} failed: {e}",
                      flush=True)
        if any_hit:
            deleted += 1
        else:
            missing += 1
    return {"deleted": deleted, "bytes_freed": bytes_freed,
            "kept": missing, "kept_run_ids": sorted(keep_ids)}


# ── system warnings (used by both the System Stats tab and the email) ─────


def system_warnings() -> list[dict]:
    """Surface issues the user should know about right now: disk getting
    full, GPUs cooked, runaway processes. Returns a list of
    ``{severity, msg}`` ordered worst-first."""
    from . import monitor
    s = monitor.system_stats()
    out: list[dict] = []
    disk = s.get("disk") or {}
    free = disk.get("free_gb")
    pct = disk.get("percent")
    if free is not None:
        if free < 2:
            out.append({"severity": "critical",
                        "msg": f"Disk almost full — only {free} GB free. "
                               "New runs will crash. Purge old run logs in "
                               "Settings to free space."})
        elif free < 10:
            out.append({"severity": "warning",
                        "msg": f"Disk low — {free} GB free. "
                               "Consider purging old run logs in Settings."})
    if pct is not None and pct >= 90:
        out.append({"severity": "warning" if pct < 95 else "critical",
                    "msg": f"Disk at {pct}% utilization."})
    ram = s.get("ram") or {}
    if ram.get("percent", 0) >= 95:
        out.append({"severity": "warning",
                    "msg": f"RAM at {ram['percent']}% — runs may OOM."})
    for g in s.get("gpus") or []:
        temp = g.get("temp_c")
        if temp and temp >= 88:
            out.append({"severity": "warning",
                        "msg": f"GPU {g.get('index')} hot ({int(temp)}°C)."})
    return out
