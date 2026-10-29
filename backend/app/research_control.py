"""Durable research gates independent of SQLite availability.

The local file supplements the DB, so a failed DB write cannot silently undo
an operator's halt. Clearing a gate happens only after a successful DB commit.
No credentials or onboarding content are stored here.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading

_lock = threading.RLock()


def _path() -> Path:
    from .config import DATA_DIR
    return DATA_DIR / "research-control.json"


def read() -> dict:
    try:
        state = json.loads(_path().read_text())
        if not isinstance(state, dict):
            raise ValueError("invalid research control state")
        return state
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        # An unreadable safety gate must not resume an experiment.
        return {"paused": True, "halted": True,
                "reason": "Research control state is unreadable; repair it before resuming."}


def write(**updates) -> dict:
    with _lock:
        state = read()
        state.update(updates)
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".research-control-", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(state, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return state


def workspace(cfg: dict | None = None) -> Path | None:
    """Resolve only the configured project, never the whole workspace parent."""
    from .config import WORKSPACE_DIR
    name = (cfg or {}).get("repo_name") or read().get("repo_name")
    if not name:
        return None
    base = WORKSPACE_DIR.resolve()
    candidate = (base / str(name)).resolve()
    if candidate == base or not candidate.is_relative_to(base):
        raise ValueError("Project workspace must be inside ARUI_WORKSPACE_DIR")
    return candidate


def remember_workspace(cfg: dict) -> None:
    name = cfg.get("repo_name")
    if name and read().get("repo_name") != name:
        workspace(cfg)  # validate before persisting
        write(repo_name=name)
