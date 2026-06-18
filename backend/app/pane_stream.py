"""Per-tmux-session raw byte stream — the engine behind the live rail xterm.

`tmux pipe-pane -o "cat >>file"` captures the EXACT bytes a program in
the pane emitted (with ANSI escapes for color, cursor moves, in-place
spinners, etc). xterm.js re-parses those bytes and reproduces the
rendering pixel-for-pixel — the same way the program would render in a
real terminal.

Compared to the old poll-``capture-pane -p`` + ``t.reset()+t.write()``
approach, this gives the user:

  • ANSI colors (Claude Code's UI is colorful)
  • cursor positioning + REPL animations (no flickering progress bars)
  • text selection survives output updates (no constant reset)
  • copy/paste of multi-line text behaves naturally

We mirror each session's pane to a stable, well-known path so the API
endpoint can byte-offset stream it without needing to know the workspace
path. The frontend remembers its offset across SSE poll ticks; reconnects
after a refresh by sending the last seen offset; resync from 0 if the
file rotates.
"""
from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from typing import Optional, Tuple

from .config import DATA_DIR

# Per-session raw-stream files live here. Gitignored (under data/).
_TERM_DIR = DATA_DIR / ".term"
_TERM_DIR.mkdir(parents=True, exist_ok=True)


def term_file(session: str) -> Path:
    """Stable path to a session's raw byte stream."""
    # tmux session names are constrained by the API layer's `_SAFE_NAME`
    # regex already, but defensive sanitize anyway.
    safe = "".join(c for c in session if c.isalnum() or c in "-_")
    return _TERM_DIR / f"{safe or 'unknown'}.raw"


def enable(session: str, *, mirror_to: Optional[str] = None,
           preserve_history: bool = True) -> Path:
    """Wire `tmux pipe-pane -o` for `session` so its byte stream lands in
    :func:`term_file`. If ``mirror_to`` is given, also append the same
    bytes to that file (a per-workspace persistent log).

    Safe to call multiple times for the same session: tmux replaces any
    prior pipe-pane mapping.

    Args:
        session: tmux session name.
        mirror_to: optional path; if set, every byte is also written to
            this file (used for per-workspace persistent agent.log).
        preserve_history: when True (default), the existing raw file is
            preserved AND we capture the pane's current visible buffer
            (``tmux capture-pane -ep``) into the file before enabling
            the live pipe — so when the UI connects, it sees both
            historical context AND new bytes. When False, the file is
            truncated (used on agent.restart so the next boot starts
            clean).
    """
    tf = term_file(session)
    if not preserve_history:
        try:
            tf.write_bytes(b"")      # truncate
        except OSError:
            pass
    else:
        # Capture the pane's current visible buffer + scrollback so the
        # UI sees "what's already there" the moment it connects. Without
        # this, opening a tab for an agent-created session that's been
        # running for an hour shows only bytes emitted AFTER we attach
        # — which usually looks like ``^L`` or nothing at all because
        # the shell isn't actively writing.
        try:
            r = subprocess.run(
                ["tmux", "capture-pane", "-t", session,
                 "-e", "-p", "-S", "-2000"],
                capture_output=True, timeout=5)
            existing = b""
            try:
                existing = tf.read_bytes() if tf.exists() else b""
            except OSError:
                existing = b""
            # If the existing raw file is empty, seed it with the
            # captured buffer. If it has content already, leave it
            # alone — we'd duplicate otherwise.
            if not existing and r.returncode == 0 and r.stdout:
                # ANSI-aware capture emits text WITHOUT trailing newline
                # on the cursor line; add CR-LF between captured rows so
                # xterm renders them as written.
                tf.write_bytes(r.stdout.replace(b"\n", b"\r\n"))
        except Exception:                                   # noqa: BLE001
            pass
    if mirror_to:
        cmd = (f"tee -a {shlex.quote(str(tf))} >> "
               f"{shlex.quote(mirror_to)}")
    else:
        cmd = f"cat >> {shlex.quote(str(tf))}"
    subprocess.run(["tmux", "pipe-pane", "-t", session, "-o", cmd],
                   capture_output=True)
    return tf


def is_piped(session: str) -> bool | None:
    """True iff `tmux pipe-pane` is currently active for the session's pane.

    Returns None if the session doesn't exist / tmux can't answer. Uses the
    ``#{pane_pipe}`` format var (1 = piped, 0 = not)."""
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-p", "-t", session, "#{pane_pipe}"],
            capture_output=True, text=True, timeout=4)
        if r.returncode != 0:
            return None
        return (r.stdout or "").strip() == "1"
    except Exception:                                       # noqa: BLE001
        return None


def ensure_piped(session: str) -> bool:
    """Self-heal: if the session is alive but its pipe-pane mirror has died
    (``pane_pipe == 0``), re-enable it so the live xterm resumes.

    This is THE fix for "the terminal froze": pipe-pane can drop (pane program
    re-exec, session reattach, etc.) and nothing was re-establishing it for the
    ``author``/``agent`` infra sessions — sweep_enable_all() skips them. Cheap
    (one display-message); only re-enables when actually unpiped, so it's safe
    to call on every raw poll. Returns True iff it re-enabled."""
    piped = is_piped(session)
    if piped is False:
        try:
            enable(session, preserve_history=True)
            return True
        except Exception:                                   # noqa: BLE001
            pass
    return False


def list_tmux_sessions() -> list[str]:
    """Return every tmux session name visible to the backend's tmux
    server. Drops infra sessions (arui, arui-cf, agent, author) that the
    Sessions tab should never show — those have dedicated views."""
    try:
        r = subprocess.run(["tmux", "list-sessions", "-F", "#{session_name}"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return []
        names = [n.strip() for n in (r.stdout or "").splitlines()
                 if n.strip()]
    except Exception:                                       # noqa: BLE001
        return []
    INFRA = {"arui", "arui-cf", "agent", "author"}
    return [n for n in names if n not in INFRA]


def sweep_enable_all() -> dict:
    """Enable pane_stream for every visible non-infra tmux session.

    The agent (and its training scripts) can create tmux sessions via
    raw ``tmux new-session`` without going through ``/api/sessions/create``
    — those sessions then have no pipe-pane wired, so opening them in
    the Sessions tab shows an empty file. This sweeper, run periodically
    by ``monitor.py``, ensures every session is piped within ~30s of
    appearing.

    Idempotent: tmux replaces any existing pipe-pane mapping when called
    on a session that's already piped. Doesn't truncate existing raw
    files.

    Returns ``{"enabled": [...], "skipped": [...]}`` for logging.
    """
    enabled, skipped = [], []
    for name in list_tmux_sessions():
        try:
            enable(name, preserve_history=True)
            enabled.append(name)
        except Exception as e:                              # noqa: BLE001
            skipped.append({"name": name, "error": str(e)[:120]})
    return {"enabled": enabled, "skipped": skipped}


def read_range(session: str, offset: int = 0,
               max_bytes: int = 1024 * 1024) -> Tuple[bytes, int, int]:
    """Return up to ``max_bytes`` from byte ``offset`` of the session's
    raw stream.

    Returns ``(chunk, new_offset, total_size)``.

    If the file shrank (rotated / truncated) since the caller's offset
    was issued, the read resumes from byte 0 — the frontend gets a
    one-time full resync and continues from there.
    """
    tf = term_file(session)
    if not tf.exists():
        return b"", 0, 0
    size = tf.stat().st_size
    if offset > size or offset < 0:
        offset = 0          # caller's offset is stale → resync
    if offset == size:
        return b"", offset, size
    with open(tf, "rb") as f:
        f.seek(offset)
        chunk = f.read(max_bytes)
    return chunk, offset + len(chunk), size


def reset(session: str) -> None:
    """Truncate the raw file. Called when an agent is about to be re-spawned
    so the next stream starts from a clean slate."""
    tf = term_file(session)
    try:
        tf.write_bytes(b"")
    except OSError:
        pass


def size(session: str) -> int:
    """Current byte size of the session's raw stream."""
    tf = term_file(session)
    try:
        return tf.stat().st_size
    except OSError:
        return 0


# Per-session last-known xterm dimensions. The frontend POSTs these
# to /api/agent/resize after FitAddon.fit(); we cache so that when
# the agent process is restarted (tmux respawned), we can re-apply
# the same size — otherwise the new tmux defaults to 120x40 and
# Claude renders too wide / too narrow for the rail until the user
# physically drags the resize handle.
_last_size: dict = {}


def remember_size(session: str, cols: int, rows: int) -> None:
    """Cache the latest xterm dimensions for a session. Called from
    /api/agent/resize."""
    _last_size[session] = (int(cols), int(rows))


def get_last_size(session: str) -> tuple[int, int] | None:
    """Return cached dimensions, or None if the frontend has never
    reported them."""
    return _last_size.get(session)


def apply_remembered_size(session: str) -> bool:
    """If we have a cached size for this session, immediately call
    tmux resize-window to match. Returns True if a resize was issued.
    Used by RealAgent.start() to restore the xterm-matched size
    after re-spawning the agent's tmux session."""
    cur = _last_size.get(session)
    if not cur:
        return False
    cols, rows = cur
    # No -A: it would size to the largest client and ignore -x/-y, leaving
    # the window at the 120x40 spawn default (garbled wrapping in the rail).
    subprocess.run(
        ["tmux", "resize-window", "-t", session, "-x", str(cols),
         "-y", str(rows)],
        capture_output=True)
    return True
