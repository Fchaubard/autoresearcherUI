"""Secret-safe exception descriptions for infrastructure boundaries.

``subprocess.CalledProcessError.__str__`` embeds the complete argv. Agent
launch argv intentionally contains tmux ``-e KEY=value`` entries, so logging or
returning that string leaks provider credentials. Infrastructure recovery paths
must use this module instead of interpolating arbitrary exceptions.
"""
from __future__ import annotations

import subprocess


def describe(exc: BaseException) -> str:
    """Return useful exception metadata without argv, input, output or text."""
    name = type(exc).__name__
    if isinstance(exc, subprocess.CalledProcessError):
        return f"{name}(returncode={exc.returncode})"
    if isinstance(exc, subprocess.TimeoutExpired):
        return f"{name}(timeout={exc.timeout})"
    return name
