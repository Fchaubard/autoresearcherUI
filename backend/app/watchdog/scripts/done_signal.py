"""done_signal — a run is marked RUNNING but its training script
already called arui.finish() / emitted a "TRAINING_DONE" marker.

This is the happy-path script: the run is genuinely complete and the
agent should be PAGED so it can immediately move on to council review /
the next idea, rather than waiting for monitor.py's hourly reconcile.
"""
from __future__ import annotations

import datetime as dt
import subprocess
from typing import Optional

from ...health.schema import Issue, SEV_INFO


DEFAULT_PARAMS = {
    "markers": [
        "TRAINING_DONE",
        "arui.finish() returned",
        "[arui] run finished",
    ],
    "stable_for_sec": 30,
}
DEFAULT_ENABLED = True
KILLS_RUN = False    # we don't kill; we just page so the agent can act


def describe() -> str:
    return ("Detects a successful 'training done' marker in the run's "
            "tmux pane and PAGES the agent so it can immediately move "
            "to council review + the next idea without waiting on "
            "monitor.py's hourly reconcile.")


def _capture(session: str) -> str:
    try:
        r = subprocess.run(
            ["tmux", "capture-pane", "-t", session, "-p", "-S", "-200"],
            capture_output=True, text=True, timeout=4)
        if r.returncode == 0:
            return r.stdout or ""
    except Exception:                                       # noqa: BLE001
        pass
    return ""


def check(run, metrics_mod, params) -> Optional[Issue]:
    if not (run.tmux_session or "").strip():
        return None
    pane = _capture(run.tmux_session)
    if not pane:
        return None
    markers = [m for m in (params.get("markers") or []) if m]
    matched = next((m for m in markers if m in pane), None)
    if not matched:
        return None
    # Stability check — don't page if the marker just appeared this tick;
    # let the next tick confirm so we don't race with the training
    # script's own shutdown / finalisation.
    try:
        last_metric = metrics_mod.last_activity(run.id)
        if last_metric:
            age = (dt.datetime.now(dt.timezone.utc).timestamp()
                   - float(last_metric))
            if age < int(params.get("stable_for_sec", 30)):
                return None
    except Exception:                                       # noqa: BLE001
        pass
    return Issue(
        code="done_signal",
        severity=SEV_INFO,
        summary=f"Run {run.run_name} appears finished — agent should act",
        evidence={
            "run_id": run.id,
            "run_name": run.run_name,
            "matched_marker": matched,
        },
        since=run.started_at or "",
        actions=[
            {"label": "View run", "kind": "open_drawer",
             "run_id": run.id},
        ],
    )


def on_fire(run, issue, params) -> dict:
    page = (
        f"[WATCHDOG] done_signal — Run `{run.run_name}` ({run.id}) "
        "looks like it's finished training successfully. The watchdog "
        "is NOT killing it — the training script should self-finalize "
        "via arui.finish(). Please: (1) check the run's headline metric "
        "+ kept-status, (2) if good, queue the next idea immediately so "
        "GPUs don't idle, (3) if the run is the LAST one in a batch and "
        "the council is due, trigger /api/council/review now.")
    return {"kill_run": False, "page_agent": True, "page_message": page}
