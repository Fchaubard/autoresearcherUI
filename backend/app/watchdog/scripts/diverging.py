"""diverging — loss is monotonically rising over the last N points.

A weaker NaN signal: the loss isn't explicitly NaN, but it's climbing
fast enough that the run is almost certainly diverging. Default
behaviour pages the agent without killing — the agent decides whether
to kill (some research deliberately tests instability).
"""
from __future__ import annotations

from typing import Optional

from ...health.schema import Issue, SEV_WARNING


DEFAULT_PARAMS = {
    "check_keys": ["train_loss", "val_loss", "loss"],
    "window_steps": 200,
    # Ratio of (last value / window start value) above which we fire.
    # 1.5 = loss got 50% worse over the window.
    "threshold_ratio": 1.5,
}
DEFAULT_ENABLED = True
KILLS_RUN = False


def describe() -> str:
    return ("Loss has gotten N% worse over the last K steps without "
            "recovering. Indicates training is going off the rails. "
            "Tune `threshold_ratio` (1.5 = 50% worse) or `window_steps`.")


def check(run, metrics_mod, params) -> Optional[Issue]:
    keys = list(params.get("check_keys") or [])
    window = int(params.get("window_steps", 200))
    ratio = float(params.get("threshold_ratio", 1.5))
    try:
        all_pts = metrics_mod.query(run.id, keys)
    except Exception:                                       # noqa: BLE001
        return None
    for k in keys:
        pts = (all_pts or {}).get(k) or []
        if len(pts) < window:
            continue
        recent = [v for s, v in pts[-window:]
                  if v is not None and isinstance(v, (int, float))]
        if len(recent) < window // 2:
            continue
        first, last = recent[0], recent[-1]
        if first <= 0:
            continue
        if last / first >= ratio:
            return Issue(
                code="diverging",
                severity=SEV_WARNING,
                summary=(f"Run {run.run_name}: {k} rose from "
                         f"{first:.4g} → {last:.4g} over {window} "
                         f"steps ({(last/first):.2f}x)"),
                evidence={
                    "run_id": run.id,
                    "run_name": run.run_name,
                    "key": k,
                    "window_steps": window,
                    "ratio": round(last / first, 3),
                    "first_value": first,
                    "last_value": last,
                },
                since=run.started_at or "",
                actions=[
                    {"label": "View run", "kind": "open_drawer",
                     "run_id": run.id},
                    {"label": "Kill run", "method": "POST",
                     "href": f"/api/runs/{run.id}/kill"},
                ],
            )
    return None


def on_fire(run, issue, params) -> dict:
    page = (
        f"[WATCHDOG] diverging — Run `{run.run_name}` ({run.id}) saw "
        f"{issue.evidence.get('key')} rise "
        f"{issue.evidence.get('ratio')}x over the last "
        f"{issue.evidence.get('window_steps')} steps. NOT killed "
        "automatically — some research deliberately tests instability. "
        "Decide: (1) lower lr / clip grads and relaunch, (2) wait it out "
        "if a warmup is in play, (3) kill the run.")
    return {"kill_run": False, "page_agent": True, "page_message": page}
