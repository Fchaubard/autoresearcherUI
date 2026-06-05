"""Watchdog runner — fires every script against every RUNNING run.

Called from ``monitor.py``'s main loop every ~30s. Per-script firing is
de-duplicated via a small in-memory ledger keyed by ``(run_id, code)``
so the agent isn't paged again and again for the same issue.

When a script fires:
  1. Emit an ``Event`` so the issue shows up in the Activity feed.
  2. If the script's ``on_fire`` says kill_run=True, mark the Run row
     crashed + ``tmux kill-session`` it.
  3. If ``page_agent=True``, ``tmux send-keys`` the structured page
     message into the ``agent`` tmux session.
"""
from __future__ import annotations

import datetime as dt
import importlib
import os
import subprocess
import threading
from typing import Optional

from ..db import SessionLocal
from ..models import Event, Run
from . import config as wd_config


# In-memory ledger of fired (run_id, code) pairs so the watchdog
# doesn't keep paging the agent every tick for the same issue. Reset
# only on backend restart — that's fine; "did you see this?" pings on
# restart are acceptable.
_FIRED: dict[tuple[str, str], dict] = {}
_LOCK = threading.Lock()


def _iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _load_script(name: str):
    return importlib.import_module(
        f"backend.app.watchdog.scripts.{name}")


def _page_agent(message: str) -> bool:
    """Send a structured page message into the 'agent' tmux session.
    The agent's prompt tells it to read [WATCHDOG] pages and act."""
    try:
        # Type the literal multi-line text into the agent's REPL, then
        # press Enter. ``tmux send-keys -l`` sends each char as itself
        # (no special-key interpretation) which is what we want for
        # arbitrary prose.
        subprocess.run(["tmux", "send-keys", "-t", "agent", "-l", message],
                       capture_output=True, timeout=5)
        subprocess.run(["tmux", "send-keys", "-t", "agent", "Enter"],
                       capture_output=True, timeout=5)
        return True
    except Exception as e:                                  # noqa: BLE001
        print(f"[watchdog] page_agent failed: {e}", flush=True)
        return False


def _kill_run(run_id: str, reason: str) -> None:
    """Mark the run crashed + kill its tmux session. Same code path
    /api/runs/{id}/kill uses, just invoked from the watchdog."""
    db = SessionLocal()
    try:
        r = db.query(Run).filter(Run.id == run_id).first()
        if not r:
            return
        if r.status == "running":
            r.status = "crashed"
            r.ended_at = _iso()
            cfg = dict(r.config) if isinstance(r.config, dict) else {}
            cfg["watchdog_kill_reason"] = reason
            r.config = cfg
            db.commit()
        sess = (r.tmux_session or "").strip()
        if sess:
            subprocess.run(["tmux", "kill-session", "-t", sess],
                           capture_output=True)
    except Exception as e:                                  # noqa: BLE001
        print(f"[watchdog] kill_run({run_id}) failed: {e}", flush=True)
    finally:
        db.close()


def _emit_event(run, issue, kind: str = "watchdog_issue") -> None:
    db = SessionLocal()
    try:
        sev = {0: "info", 1: "warning", 2: "critical"}.get(
            int(getattr(issue, "severity", 0)), "info")
        db.add(Event(
            id="ev-" + os.urandom(4).hex(),
            type=kind,
            severity=sev,
            actor=f"watchdog:{issue.code}",
            message=(f"{issue.code} — {issue.summary}")[:280],
            created_at=_iso(),
        ))
        db.commit()
    except Exception as e:                                  # noqa: BLE001
        print(f"[watchdog] emit_event failed: {e}", flush=True)
    finally:
        db.close()


def run_once(*, dry_run: bool = False) -> list[dict]:
    """Scan every RUNNING run against every enabled script. Returns a
    list of dicts describing what fired (mostly for tests + the
    `/api/watchdog/run` endpoint).

    Args:
        dry_run: when True, no Event, no page, no kill — just compute.
            Used by unit tests + a "preview" endpoint.
    """
    from .. import metrics
    fired: list[dict] = []
    cfg = wd_config.get_config()
    db = SessionLocal()
    try:
        runs = db.query(Run).filter(Run.status == "running").all()
    finally:
        db.close()
    for run in runs:
        for name, entry in cfg.items():
            if not entry.get("enabled"):
                continue
            try:
                mod = _load_script(name)
            except Exception as e:                          # noqa: BLE001
                print(f"[watchdog] cannot load script {name}: {e}",
                      flush=True)
                continue
            params = dict(entry.get("params") or {})
            try:
                issue = mod.check(run, metrics, params)
            except Exception as e:                          # noqa: BLE001
                print(f"[watchdog] {name}.check({run.id}) crashed: {e}",
                      flush=True)
                continue
            if issue is None:
                continue
            key = (run.id, issue.code)
            with _LOCK:
                if key in _FIRED:
                    continue          # de-duped
                if not dry_run:
                    _FIRED[key] = {
                        "at": _iso(),
                        "summary": issue.summary,
                    }
            on_fire = getattr(mod, "on_fire", None)
            policy = (on_fire(run, issue, params) if on_fire else
                      {"kill_run": getattr(mod, "KILLS_RUN", False),
                       "page_agent": True,
                       "page_message": (
                           f"[WATCHDOG] {issue.code} on run "
                           f"{run.run_name}: {issue.summary}")})
            if not dry_run:
                _emit_event(run, issue)
                if policy.get("page_agent"):
                    _page_agent(policy.get("page_message")
                                or f"[WATCHDOG] {issue.code} fired.")
                if policy.get("kill_run"):
                    _kill_run(run.id,
                              policy.get("page_message")
                              or issue.summary)
            fired.append({
                "script": name,
                "run_id": run.id,
                "issue": issue.as_dict(),
                "policy": policy,
            })
    return fired


def tick() -> dict:
    """Convenience wrapper used by monitor.py. Returns a one-line
    summary for the [monitor] log line."""
    fired = run_once()
    return {"n_fired": len(fired),
            "fired": [f["script"] + "/" + f["run_id"][:8]
                      for f in fired]}


def reset_ledger() -> None:
    """Clear the de-dup ledger. Useful in tests."""
    with _LOCK:
        _FIRED.clear()
