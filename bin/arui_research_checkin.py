#!/usr/bin/env python3
"""Collect local research evidence and deliver one bounded hourly check-in.

The system cron owns timing. This program never starts/stops experiments,
changes models, sends email, or calls a model directly. The existing research
conversation receives the evidence; the configured PI continues its own review.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import urllib.request

MIN_INTERVAL_SECONDS = 55 * 60


def read_config(db: Path) -> dict:
    with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        row = conn.execute("SELECT value FROM setting WHERE key='onboarding'").fetchone()
    return json.loads(row[0]) if row else {}


def disabled_reason(cfg: dict, workspace: Path) -> str | None:
    if not cfg.get("research_oversight_enabled", False):
        return "disabled"
    if cfg.get("research_paused") or cfg.get("research_halted"):
        return "research_paused_or_halted"
    directives = workspace / "directives.jsonl"
    if directives.exists():
        for line in directives.read_text().splitlines():
            if not line.strip():
                continue
            directive = json.loads(line)
            if directive.get("type") == "HALT" and directive.get("status", "open") == "open":
                return "halt_directive"
    return None


def atomic_json(path: Path, data: dict) -> None:
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, indent=2) + "\n")
    temp.chmod(0o600)
    temp.replace(path)


def interval_summary(rows: list[dict], start: dt.datetime, end: dt.datetime) -> dict:
    spans = []
    for row in rows:
        if not row["started_at"] or (not row["ended_at"] and row["status"] != "running"):
            continue
        a = max(start, dt.datetime.fromisoformat(row["started_at"]))
        b = min(end, dt.datetime.fromisoformat(row["ended_at"])) if row["ended_at"] else end
        if b > a:
            spans.append((a, b))
    merged = []
    for a, b in sorted(spans):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    active = sum((b - a).total_seconds() for a, b in merged)
    worker = sum((b - a).total_seconds() for a, b in spans)
    return {"registered_jobs": len(spans), "registered_worker_hours": worker / 3600,
            "minutes_with_any_registered_job": active / 60,
            "minutes_without_registered_job": ((end - start).total_seconds() - active) / 60,
            "measurement": "Registered intervals include loading/setup; not sampled GPU compute utilization."}


def collect(db: Path, workspace: Path, now: dt.datetime) -> dict:
    cfg = read_config(db)
    campaign = None
    with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        start = now - dt.timedelta(hours=1)
        rows = [dict(row) for row in conn.execute(
            "SELECT id,status,started_at,ended_at FROM run WHERE started_at<? "
            "AND (ended_at>=? OR ended_at IS NULL)", (now.isoformat(), start.isoformat()))]
        recent = [dict(row) for row in conn.execute(
            "SELECT id,status,started_at,ended_at,headline_metric FROM run "
            "ORDER BY started_at DESC LIMIT 12")]
        if cfg.get("research_campaign_started_at"):
            campaign_start = dt.datetime.fromisoformat(cfg["research_campaign_started_at"])
            campaign_rows = [dict(row) for row in conn.execute(
                "SELECT id,status,started_at,ended_at,gpus_required FROM run WHERE started_at>=? "
                "AND started_at<?", (campaign_start.isoformat(), now.isoformat()))]
            hours = 0.0
            for row in campaign_rows:
                if not row["ended_at"] and row["status"] != "running":
                    continue
                end = min(now, dt.datetime.fromisoformat(row["ended_at"])) if row["ended_at"] else now
                elapsed = max(0, (end - dt.datetime.fromisoformat(row["started_at"])).total_seconds())
                hours += elapsed * max(1, int(row["gpus_required"] or 1)) / 3600
            cap = float(cfg.get("research_campaign_gpu_hour_cap", 30))
            campaign = {"started_at": campaign_start.isoformat(), "charged_gpu_hours": hours,
                        "cap_gpu_hours": cap, "budget_exhausted": hours >= cap,
                        "measurement": "Registered duration times declared GPU allocation; includes setup, not sampled compute."}
    try:
        gpu = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,utilization.gpu,memory.used",
             "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=12)
        gpu_state = {"csv": gpu.stdout[:3000], "available": gpu.returncode == 0}
    except (OSError, subprocess.TimeoutExpired):
        gpu_state = {"available": False}
    # The garbage root also holds datasets/caches with millions of entries.
    # Only traverse current research and the historical experiment namespaces.
    report_roots = [workspace / "research", *workspace.glob("garbage/resume_*")]
    reports = sorted((p for root in report_roots
                      for p in root.glob("**/final_report.md")),
                     key=lambda p: p.stat().st_mtime, reverse=True)[:3]
    return {"at": now.isoformat(), "last_hour": interval_summary(rows, start, now),
            "campaign": campaign, "recent_runs": recent, "gpus": gpu_state,
            "latest_reports": [{"path": str(p), "excerpt": p.read_text()[:3000]} for p in reports],
            "brief": str(workspace / "OPERATOR_RESEARCH_BRIEF.md"),
            "note": "A completed or kept_novel run is not evidence of a successful scientific hypothesis."}


def deliver(api_base: str, cfg: dict, message: str) -> dict:
    request = urllib.request.Request(
        api_base.rstrip("/") + "/api/agent/send",
        data=json.dumps({"text": message}).encode(),
        headers={"Authorization": "Bearer " + cfg["passcode"], "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=50) as response:
        result = json.load(response)
    return {k: result.get(k) for k in ("ok", "delivery", "error") if k in result}


def run(db: Path, workspace: Path, out: Path, api_base: str,
        *, dry_run: bool = False, now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    out.mkdir(parents=True, mode=0o700, exist_ok=True)
    with (out / "checkin.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "skipped", "reason": "already_running"}
        cfg = read_config(db)
        reason = disabled_reason(cfg, workspace)
        if reason:
            result = {"status": "skipped", "reason": reason, "at": now.isoformat()}
            if not dry_run:
                atomic_json(out / "latest_check.json", result)
            return result
        state_path = out / "delivery_state.json"
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        if not dry_run and now.timestamp() - state.get("last_attempt_epoch", 0) < MIN_INTERVAL_SECONDS:
            return {"status": "skipped", "reason": "recent_attempt"}
        snapshot = collect(db, workspace, now)
        snapshot_path = out / (now.strftime("%Y%m%dT%H%M%SZ") + ".json")
        atomic_json(snapshot_path, snapshot)
        atomic_json(out / "latest_evidence.json", snapshot)
        message = (
            "[Hourly research oversight — operator-requested automation]\n"
            f"Evidence snapshot: {snapshot_path}\n"
            "Read OPERATOR_RESEARCH_BRIEF.md and this bounded snapshot; inspect the cited final reports "
            "and actual receipts as needed. Report progress in the dashboard: current mechanism and "
            "scope relevance; actual long-context quality versus controls; retained columns and memory "
            "reduction; construction/serving cost; scaling evidence; campaign budget used; idle time "
            "and exact blockers; one falsifiable next decision. Update AGENT_STATE.md at a meaningful "
            "milestone. Correct drift immediately within the approved scope in the current brief and "
            "Settings; do not revive superseded directives. Judge progress against the operator's "
            "actual targets, not successful job exits or falling training loss alone. Honor the frozen "
            "quality rule and configured campaign ceiling; do not invent success, duplicate jobs, "
            "change models, or bypass review. Preserve active valid work and use event wait when appropriate. "
            "Give one concise evidence-based assessment, not a new status-polling loop. "
            "This is an automated check-in, not a new human instruction or permission to alter the goal."
        )
        if dry_run:
            return {"status": "dry_run", "snapshot": str(snapshot_path), "message": message}
        # Recheck pause after potentially slow filesystem/GPU inspection.
        cfg = read_config(db)
        reason = disabled_reason(cfg, workspace)
        if reason:
            result = {"status": "skipped", "reason": reason, "at": now.isoformat()}
            atomic_json(out / "latest_check.json", result)
            return result
        # Reserve before sending: a timeout may mean the native CLI accepted it.
        # Never retry an ambiguous delivery in a tight loop or duplicate the hour.
        atomic_json(state_path, {"last_attempt_epoch": now.timestamp(), "at": now.isoformat(),
                                 "status": "attempting", "snapshot": str(snapshot_path)})
        try:
            delivery = deliver(api_base, cfg, message)
            result = {"status": "delivered" if delivery.get("ok") else "delivery_failed",
                      "at": now.isoformat(), "snapshot": str(snapshot_path), "delivery": delivery}
        except Exception as exc:
            # Never log a request/header/config or an exception containing secrets.
            result = {"status": "delivery_uncertain", "at": now.isoformat(),
                      "snapshot": str(snapshot_path), "error_type": type(exc).__name__}
        atomic_json(out / "latest_check.json", result)
        atomic_json(state_path, {**result, "last_attempt_epoch": now.timestamp()})
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--api-base", default="http://127.0.0.1:8000")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = run(args.db, args.workspace, args.out, args.api_base, dry_run=args.dry_run)
    print(json.dumps(result), flush=True)
    raise SystemExit(1 if result["status"] in {"delivery_failed", "delivery_uncertain"} else 0)
