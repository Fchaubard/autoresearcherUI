"""no_metric_flow — run is RUNNING but hasn't logged a metric point in
a while.

Either the training code isn't calling arui.log() (a logging bug — see
the flat-line plot Francois observed 2026-06-05) or the run is hung.
Default threshold is 10 minutes; the agent may legitimately need to
raise this for slow evals (LLM judge calls, full-validation passes,
etc.) — that's why the onboarding flow asks.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from ...health.schema import Issue, SEV_WARNING, SEV_CRITICAL


DEFAULT_PARAMS = {
    "timeout_sec": 600,
    "min_run_age_sec": 120,   # don't flag a brand-new run with no points
}
DEFAULT_ENABLED = True
KILLS_RUN = False             # surface the issue; let the agent decide


def describe() -> str:
    return ("No new metric point in N seconds while the run is still "
            "marked RUNNING. Catches frozen training loops and forgotten "
            "arui.log() calls. Raise `timeout_sec` if your eval phase "
            "legitimately takes hours.")


def _age_sec(iso: str) -> Optional[float]:
    if not iso:
        return None
    try:
        return (dt.datetime.now(dt.timezone.utc)
                - dt.datetime.fromisoformat(iso)).total_seconds()
    except Exception:                                       # noqa: BLE001
        return None


def check(run, metrics_mod, params) -> Optional[Issue]:
    timeout_sec = int(params.get("timeout_sec", 600))
    min_age = int(params.get("min_run_age_sec", 120))
    run_age = _age_sec(run.started_at or run.created_at or "") or 0
    if run_age < min_age:
        return None
    try:
        last = metrics_mod.last_activity(run.id)
    except Exception:                                       # noqa: BLE001
        last = None
    if last is None:
        last_age = run_age
        last_human = "(never logged)"
    else:
        last_age = (dt.datetime.now(dt.timezone.utc).timestamp()
                    - float(last))
        last_human = f"{int(last_age // 60)}m ago"
    if last_age < timeout_sec:
        return None
    sev = SEV_CRITICAL if last_age >= 2 * timeout_sec else SEV_WARNING
    return Issue(
        code="no_metric_flow",
        severity=sev,
        summary=(f"Run {run.run_name} has logged no metric in "
                 f"{int(last_age // 60)} min (threshold "
                 f"{timeout_sec // 60} min)"),
        evidence={
            "run_id": run.id,
            "run_name": run.run_name,
            "last_metric_age_sec": int(last_age),
            "last_metric_age_human": last_human,
            "threshold_sec": timeout_sec,
        },
        since=run.started_at or "",
        actions=[
            {"label": "View run", "kind": "open_drawer",
             "run_id": run.id},
            {"label": "Kill run", "method": "POST",
             "href": f"/api/runs/{run.id}/kill"},
        ],
    )


def on_fire(run, issue, params) -> dict:
    mins = int((issue.evidence.get("last_metric_age_sec") or 0) // 60)
    page = (
        f"[WATCHDOG] no_metric_flow — Run `{run.run_name}` "
        f"({run.id}) hasn't logged a metric in {mins} min, but it's "
        "still marked RUNNING. Likely causes:\n"
        "  (a) training script forgot to call arui.log() during a long "
        "eval phase;\n"
        "  (b) the run hung / crashed silently;\n"
        "  (c) the timeout is too aggressive for this experiment — "
        "raise `watchdog.config.no_metric_flow.params.timeout_sec` if so.\n"
        "Please diagnose: tmux capture-pane the run's session to see if "
        "stdout is alive, or kill the run and relaunch with debug "
        "logging. Don't ignore — every minute past this counts as wasted "
        "GPU.")
    return {"kill_run": False, "page_agent": True, "page_message": page}
