"""gpu_oom — the run's pane contains a CUDA OOM trace.

We scrape the run's tmux pane (capture-pane) for OOM patterns. This is
the ONE place we intentionally screen-scrape — it's a string look-up,
not a state inference, and the cost of a false positive is just a page
to the agent.
"""
from __future__ import annotations

import subprocess
from typing import Optional

from ...health.schema import Issue, SEV_CRITICAL


DEFAULT_PARAMS = {
    "patterns": [
        "CUDA out of memory",
        "torch.cuda.OutOfMemoryError",
        "RuntimeError: CUDA error: out of memory",
        "cuOOM",
    ],
}
DEFAULT_ENABLED = True
KILLS_RUN = True


def describe() -> str:
    return ("Detects 'CUDA out of memory' / torch.cuda.OutOfMemoryError "
            "in the run's tmux pane and KILLS the run so a smaller "
            "batch_size / gradient_accumulation can take its place.")


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
    patterns = [p for p in (params.get("patterns") or []) if p]
    if not patterns:
        return None
    pane = _capture(run.tmux_session)
    if not pane:
        return None
    matched = next((p for p in patterns if p in pane), None)
    if not matched:
        return None
    return Issue(
        code="gpu_oom",
        severity=SEV_CRITICAL,
        summary=f"Run {run.run_name} OOM'd: {matched!r}",
        evidence={
            "run_id": run.id,
            "run_name": run.run_name,
            "matched_pattern": matched,
            "tmux_session": run.tmux_session,
        },
        since=run.started_at or "",
        actions=[
            {"label": "View run", "kind": "open_drawer",
             "run_id": run.id},
        ],
    )


def on_fire(run, issue, params) -> dict:
    page = (
        f"[WATCHDOG] gpu_oom — Run `{run.run_name}` ({run.id}) hit "
        f"{issue.evidence.get('matched_pattern')!r}. The watchdog is "
        "KILLING this run so the GPU comes back online. Please relaunch "
        "with a smaller batch_size or higher gradient_accumulation, OR "
        "if this is OOMing on the first batch the model just doesn't fit "
        "— consider a smaller variant or LoRA.")
    return {"kill_run": True, "page_agent": True, "page_message": page}
