"""Safe tmux session helpers.

A single, defensive place for "is this session alive", "list sessions", and
"kill a session" so that generic controls (subagent tooling, the Sessions tab,
run-kill endpoints) can NEVER take down the core infrastructure sessions - most
importantly the main research ``agent`` - by name collision or a stray request.

Every kill goes through :func:`kill_session`, which validates the session name
and refuses the protected core sessions unless a caller explicitly opts in with
``allow_protected=True`` (used only by the deliberate agent-restart paths).
"""
from __future__ import annotations

import re
import subprocess

# Session names we generate: run ids may contain '=' (axis sweeps) and '.'.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.\-=]+$")

# Core infrastructure sessions. Generic controls must never kill these:
#   agent   - the main research agent (Claude Code)
#   author  - the paper-mode author agent (Claude Code)
#   arui    - the FastAPI backend
#   arui-cf / cf - the cloudflared tunnel
#   coord   - legacy coordinator session
PROTECTED_SESSIONS = frozenset({"agent", "author", "arui", "arui-cf", "cf",
                                "coord"})


def valid_name(name: str) -> bool:
    """True iff ``name`` is a syntactically safe tmux session name (1-80 chars
    of ``[A-Za-z0-9_.-=]``). Guards against argument injection / empty names."""
    if not name or len(name) > 80 or name.startswith("-"):
        return False
    return bool(_SAFE_NAME.match(name))


def is_protected(name: str) -> bool:
    return name in PROTECTED_SESSIONS


def is_alive(name: str) -> bool:
    """True iff a tmux session named ``name`` currently exists. Never raises."""
    if not valid_name(name):
        return False
    try:
        return subprocess.run(["tmux", "has-session", "-t", name],
                              capture_output=True, timeout=5).returncode == 0
    except Exception:                                      # noqa: BLE001
        return False


def list_sessions(include_protected: bool = True) -> list[str]:
    """All tmux session names. With ``include_protected=False`` the core infra
    sessions are filtered out (what the Sessions tab wants to show)."""
    try:
        out = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True, text=True, timeout=5)
    except Exception:                                      # noqa: BLE001
        return []
    names = [n.strip() for n in (out.stdout or "").splitlines() if n.strip()]
    if include_protected:
        return names
    return [n for n in names if n not in PROTECTED_SESSIONS]


def kill_session(name: str, *, allow_protected: bool = False) -> tuple[bool, str]:
    """Kill the tmux session ``name``. Returns ``(ok, message)``.

    Refuses to kill a :data:`PROTECTED_SESSIONS` session unless the caller
    passes ``allow_protected=True`` - so a generic ``/runs/<id>/kill`` or a
    subagent control can never take down the main research ``agent`` (or the
    backend / tunnel) by passing its name. Never raises.
    """
    if not valid_name(name):
        return False, "invalid session name"
    if name in PROTECTED_SESSIONS and not allow_protected:
        return False, f"'{name}' is a protected core session - refusing to kill"
    try:
        r = subprocess.run(["tmux", "kill-session", "-t", name],
                           capture_output=True, text=True, timeout=5)
    except Exception as e:                                 # noqa: BLE001
        return False, f"kill failed: {e}"
    if r.returncode == 0:
        return True, f"killed '{name}'"
    return False, (r.stderr or "").strip() or f"session '{name}' not found"
