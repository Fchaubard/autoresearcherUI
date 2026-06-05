"""Unit tests for pane_stream.enable's new preserve_history behavior
(PR 3 of state-control rewrite, 2026-06-05).

The "^L showing literal in Sessions tab" bug was a UX symptom of the
backend connecting to an already-running session AFTER it had emitted
all its interesting output. We now capture the current visible buffer
(``tmux capture-pane -ep -S -2000``) on the first enable() so the UI
shows historical context immediately.

These tests pin the contract by monkeypatching subprocess.run so they
don't need a real tmux binary.
"""
from __future__ import annotations

import subprocess
import types
import pytest


@pytest.fixture
def fake_tmux(monkeypatch):
    """Fake subprocess.run so we can capture which tmux commands fire."""
    calls = []
    sentinel = object()

    def fake_run(cmd, *args, **kwargs):
        calls.append({"cmd": cmd, "args": args, "kwargs": kwargs})
        # capture-pane returns canned scrollback bytes; pipe-pane / others
        # return 0 with empty output.
        if isinstance(cmd, list) and cmd[:2] == ["tmux", "capture-pane"]:
            return types.SimpleNamespace(
                returncode=0,
                stdout=b"hello from existing session\nstill running...",
                stderr=b"")
        if isinstance(cmd, list) and cmd[:2] == ["tmux", "list-sessions"]:
            return types.SimpleNamespace(
                returncode=0,
                stdout="agent\narui\narui-cf\nfwsweep_s0\nfwsweep_s1\n",
                stderr="")
        return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_enable_preserves_history_by_default(arui_env, fake_tmux):
    """enable() with default args should capture-pane FIRST and seed the
    raw file with the existing buffer, then pipe-pane for live bytes."""
    from backend.app import pane_stream
    tf = pane_stream.enable("fwsweep_s1")
    # capture-pane was called before pipe-pane
    cap_idx = next(i for i, c in enumerate(fake_tmux)
                   if c["cmd"][:2] == ["tmux", "capture-pane"])
    pp_idx = next(i for i, c in enumerate(fake_tmux)
                  if c["cmd"][:2] == ["tmux", "pipe-pane"])
    assert cap_idx < pp_idx
    # The raw file was seeded with the captured bytes (CR-LF normalised)
    assert tf.exists()
    body = tf.read_bytes()
    assert b"hello from existing session" in body
    assert b"\r\n" in body, "CR-LF normalisation should have happened"


def test_enable_truncates_when_preserve_history_false(
        arui_env, fake_tmux, monkeypatch):
    """The agent-boot path passes preserve_history=False to wipe the raw
    file so the next frontend connection sees a clean Claude REPL
    instead of last session's bytes."""
    from backend.app import pane_stream
    tf = pane_stream.term_file("agent")
    tf.write_bytes(b"OLD BYTES FROM PRIOR BOOT")
    pane_stream.enable("agent", preserve_history=False)
    assert tf.read_bytes() == b""


def test_enable_does_not_duplicate_seed_on_repeat_call(
        arui_env, fake_tmux):
    """Calling enable() twice in a row must NOT duplicate the captured
    buffer — once the raw file has content, we leave it alone."""
    from backend.app import pane_stream
    tf = pane_stream.enable("fwsweep_s1")
    first_len = len(tf.read_bytes())
    pane_stream.enable("fwsweep_s1")
    second_len = len(tf.read_bytes())
    assert first_len == second_len


def test_list_tmux_sessions_filters_infra(arui_env, fake_tmux):
    """list_tmux_sessions() must hide the infra sessions
    (arui / arui-cf / agent / author) — those have dedicated views and
    shouldn't appear in the Sessions tab."""
    from backend.app import pane_stream
    sessions = pane_stream.list_tmux_sessions()
    assert "arui" not in sessions
    assert "arui-cf" not in sessions
    assert "agent" not in sessions
    assert "fwsweep_s0" in sessions
    assert "fwsweep_s1" in sessions


def test_sweep_enable_all_wires_every_visible_session(
        arui_env, fake_tmux):
    """sweep_enable_all() is the periodic safety net (called from
    monitor.py) that catches sessions created via raw `tmux new-session`
    that bypass /api/sessions/create."""
    from backend.app import pane_stream
    out = pane_stream.sweep_enable_all()
    assert set(out["enabled"]) == {"fwsweep_s0", "fwsweep_s1"}
    assert out["skipped"] == []
