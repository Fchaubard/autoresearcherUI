"""Paper-mode anti-pattern watcher.

Wakes up every hour (configurable via Settings: paper_antipattern_cadence_minutes,
default 60), inspects the project state, and files low-priority decision-queue
nudges for the 5 anti-patterns defined in the v3 spec.

Only active when project_mode == 'paper'. Idempotent: if a nudge with the same
title is already pending, it's not re-filed (see `_file_nudge` in api.py).
"""
from __future__ import annotations

import threading
import time

_LOCK = threading.Lock()
_STARTED = False


def start() -> None:
    """Spawn the watcher thread. Once-per-process."""
    global _STARTED
    with _LOCK:
        if _STARTED:
            return
        _STARTED = True
    threading.Thread(target=_loop, daemon=True, name="paper-watcher").start()
    print("[paper-watcher] scheduler started", flush=True)


def _loop() -> None:
    # First cycle 5 minutes after startup so the rest of the system can settle.
    time.sleep(5 * 60)
    while True:
        try:
            _tick()
        except Exception as e:                       # noqa: BLE001
            print(f"[paper-watcher] tick error: {e}", flush=True)
        time.sleep(60 * 60)


def _tick() -> None:
    from . import paper as _paper
    # Only fire when in paper mode.
    if _paper.project_mode() != "paper":
        return
    # Reuse the endpoint logic (so manual `POST /paper/anti_patterns/run`
    # and the cron both share the same code path).
    from .api import paper_antipatterns_run
    result = paper_antipatterns_run()
    if isinstance(result, dict) and result.get("filed"):
        print(f"[paper-watcher] filed {result['filed']} nudge(s)", flush=True)
