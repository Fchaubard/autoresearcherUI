"""nan_loss — training loss / val loss went non-finite (NaN or +/-Inf).

Almost always indicates a divergent run that's now wasting GPU. Default
behaviour KILLS the run after one confirmation — agent can override by
listing the kill toggle in onboarding.
"""
from __future__ import annotations

import math
from typing import Optional

from ...health.schema import Issue, SEV_CRITICAL


DEFAULT_PARAMS = {
    # Metric keys to monitor for non-finite values. Aliased keys
    # (loss → train_loss etc) are handled by metrics.append() already,
    # so we just check these canonical names plus the project's own.
    "check_keys": ["train_loss", "val_loss", "loss"],
    "min_points": 3,
}
DEFAULT_ENABLED = True
KILLS_RUN = True


def describe() -> str:
    return ("Training loss / val loss went NaN or Inf. Default behaviour "
            "KILLS the run since the optimiser has lost the plot. Adjust "
            "`check_keys` to add project-specific loss aliases.")


def check(run, metrics_mod, params) -> Optional[Issue]:
    keys = list(params.get("check_keys") or [])
    if not keys:
        return None
    min_pts = int(params.get("min_points", 3))
    bad_key = None
    bad_step = None
    bad_val = None
    try:
        all_pts = metrics_mod.query(run.id, keys)
    except Exception:                                       # noqa: BLE001
        return None
    for k in keys:
        pts = (all_pts or {}).get(k) or []
        if len(pts) < min_pts:
            continue
        for step, val in pts[-min_pts:]:
            if val is None:
                continue
            try:
                f = float(val)
            except Exception:                               # noqa: BLE001
                continue
            if math.isnan(f) or math.isinf(f):
                bad_key, bad_step, bad_val = k, step, f
                break
        if bad_key:
            break
    if not bad_key:
        return None
    return Issue(
        code="nan_loss",
        severity=SEV_CRITICAL,
        summary=(f"Run {run.run_name} diverged: {bad_key} = {bad_val} at "
                 f"step {bad_step}"),
        evidence={
            "run_id": run.id,
            "run_name": run.run_name,
            "key": bad_key,
            "step": bad_step,
            "value": str(bad_val),
        },
        since=run.started_at or "",
        actions=[
            {"label": "View run", "kind": "open_drawer",
             "run_id": run.id},
            {"label": "Kill (or re-confirm)", "method": "POST",
             "href": f"/api/runs/{run.id}/kill"},
        ],
    )


def on_fire(run, issue, params) -> dict:
    page = (
        f"[WATCHDOG] nan_loss — Run `{run.run_name}` ({run.id}) just "
        f"emitted a non-finite {issue.evidence.get('key')} value at step "
        f"{issue.evidence.get('step')}. The watchdog is KILLING this run "
        "automatically. Please diagnose:\n"
        "  • likely lr/wd/grad-clip mis-set, mixed precision over/underflow, "
        "or a bad batch.\n"
        "  • check the lessons.md for prior incidents of the same kind on "
        "this project.\n"
        "Then either fix the config and relaunch, OR mark the idea "
        "discarded if this is the third NaN you've seen for it.")
    return {"kill_run": True, "page_agent": True, "page_message": page}
