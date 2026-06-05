"""crashed_silently — the run's tmux session has exited but the DB still
says RUNNING.

monitor.py already reconciles this every tick, but the watchdog ALSO
checks so the agent gets paged even if monitor's reconcile is busy or
the Run row is in the middle of an update.
"""
from __future__ import annotations

import subprocess
from typing import Optional

from ...health.schema import Issue, SEV_WARNING


DEFAULT_PARAMS = {
    "min_run_age_sec": 60,
}
DEFAULT_ENABLED = True
KILLS_RUN = False


def describe() -> str:
    return ("Run is marked RUNNING but its tmux session no longer "
            "exists. Catches silent crashes that escape monitor.py's "
            "reconcile. Default behaviour: tag the run as 'crashed' + "
            "page the agent.")


def _tmux_alive(session: str) -> bool:
    try:
        r = subprocess.run(
            ["tmux", "has-session", "-t", session],
            capture_output=True, timeout=4)
        return r.returncode == 0
    except Exception:                                       # noqa: BLE001
        return True   # be safe — never page on a tmux probe failure


def check(run, metrics_mod, params) -> Optional[Issue]:
    sess = (run.tmux_session or "").strip()
    if not sess:
        return None
    import datetime as dt
    try:
        age = (dt.datetime.now(dt.timezone.utc)
               - dt.datetime.fromisoformat(
                   run.started_at or run.created_at or "")).total_seconds()
    except Exception:                                       # noqa: BLE001
        age = 999
    if age < int(params.get("min_run_age_sec", 60)):
        return None
    if _tmux_alive(sess):
        return None
    return Issue(
        code="crashed_silently",
        severity=SEV_WARNING,
        summary=(f"Run {run.run_name} tmux session vanished but the DB "
                 "row still says RUNNING"),
        evidence={
            "run_id": run.id,
            "run_name": run.run_name,
            "tmux_session": sess,
        },
        since=run.started_at or "",
        actions=[
            {"label": "View run", "kind": "open_drawer",
             "run_id": run.id},
            {"label": "Mark crashed", "method": "POST",
             "href": f"/api/runs/{run.id}/kill"},
        ],
    )


def on_fire(run, issue, params) -> dict:
    page = (
        f"[WATCHDOG] crashed_silently — Run `{run.run_name}` ({run.id}) "
        f"tmux session `{issue.evidence.get('tmux_session')}` is gone "
        "but the DB still has it as RUNNING. The watchdog is marking it "
        "crashed. Look at the run's logs to understand why it died, log "
        "a lesson, and decide whether to relaunch.")
    return {"kill_run": True, "page_agent": True, "page_message": page}
