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


def enable(session: str, *, mirror_to: Optional[str] = None) -> Path:
    """Wire `tmux pipe-pane -o` for `session` so its byte stream lands in
    :func:`term_file`. If ``mirror_to`` is given, also append the same
    bytes to that file (a per-workspace persistent log).

    The raw file is truncated first so the next frontend connection
    starts from a clean slate — otherwise the agent's previous boot
    output would persist across restarts and confuse the user.

    Safe to call multiple times for the same session: tmux replaces any
    prior pipe-pane mapping.
    """
    tf = term_file(session)
    try:
        tf.write_bytes(b"")          # truncate
    except OSError:
        pass
    if mirror_to:
        cmd = (f"tee -a {shlex.quote(str(tf))} >> "
               f"{shlex.quote(mirror_to)}")
    else:
        cmd = f"cat >> {shlex.quote(str(tf))}"
    subprocess.run(["tmux", "pipe-pane", "-t", session, "-o", cmd],
                   capture_output=True)
    return tf


def read_range(session: str, offset: int = 0,
               max_bytes: int = 256 * 1024) -> Tuple[bytes, int, int]:
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
