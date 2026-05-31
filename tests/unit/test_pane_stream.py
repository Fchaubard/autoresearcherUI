"""Unit tests for pane_stream — the per-tmux-session raw byte streamer
that drives the live xterm.js rail."""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def ps(tmp_path, monkeypatch):
    """Reload pane_stream with _TERM_DIR pointing at a per-test tmp dir.

    pane_stream caches the term dir at import time, so we monkeypatch the
    module attribute and ensure the dir exists. This keeps tests
    hermetic and doesn't leave files in the real data/.term."""
    from backend.app import pane_stream as _ps
    test_dir = tmp_path / ".term"
    test_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(_ps, "_TERM_DIR", test_dir)
    return _ps


def test_term_file_path_is_deterministic_and_sanitized(ps):
    p1 = ps.term_file("agent")
    p2 = ps.term_file("agent")
    assert p1 == p2
    assert p1.name == "agent.raw"
    # Sanitization: '/' is dropped, alnum/-/_/. kept
    p3 = ps.term_file("a/g/e/n/t")
    assert "/" not in p3.name
    assert p3.name == "agent.raw"
    # Falls back to 'unknown.raw' if everything got stripped
    p4 = ps.term_file("/////")
    assert p4.name == "unknown.raw"


def test_read_range_empty_session_returns_zero(ps):
    chunk, off, size = ps.read_range("nope_never_existed", 0)
    assert chunk == b""
    assert off == 0
    assert size == 0


def test_read_range_full_then_incremental(ps):
    sess = "inc"
    tf = ps.term_file(sess)
    tf.write_bytes(b"hello world")
    # Read everything from start
    chunk, off, size = ps.read_range(sess, 0)
    assert chunk == b"hello world"
    assert off == 11
    assert size == 11
    # Tail more data, read incrementally from the prior offset
    with open(tf, "ab") as f:
        f.write(b" and goodbye")
    chunk2, off2, size2 = ps.read_range(sess, off)
    assert chunk2 == b" and goodbye"
    assert off2 == 23
    assert size2 == 23
    # At EOF — empty chunk, same offset
    chunk3, off3, size3 = ps.read_range(sess, off2)
    assert chunk3 == b""
    assert off3 == off2
    assert size3 == size2


def test_read_range_handles_rotation(ps):
    """If the file shrank since the caller's offset, we resync from 0."""
    sess = "rot"
    tf = ps.term_file(sess)
    tf.write_bytes(b"abcdefghij")          # 10 bytes
    # File gets rotated/truncated to fewer bytes:
    tf.write_bytes(b"NEW")                 # 3 bytes
    chunk, off, size = ps.read_range(sess, 10)
    # We should detect the shrink and resync from 0
    assert chunk == b"NEW"
    assert off == 3
    assert size == 3


def test_read_range_respects_max_bytes(ps):
    sess = "big"
    tf = ps.term_file(sess)
    tf.write_bytes(b"x" * 1024)
    chunk, off, size = ps.read_range(sess, 0, max_bytes=100)
    assert len(chunk) == 100
    assert off == 100
    assert size == 1024
    chunk2, off2, size2 = ps.read_range(sess, off, max_bytes=100)
    assert len(chunk2) == 100
    assert off2 == 200


def test_reset_truncates_file(ps):
    sess = "reset"
    tf = ps.term_file(sess)
    tf.write_bytes(b"some content")
    assert tf.stat().st_size > 0
    ps.reset(sess)
    assert tf.stat().st_size == 0


def test_size_reports_current_bytes(ps):
    sess = "size"
    assert ps.size(sess) == 0
    tf = ps.term_file(sess)
    tf.write_bytes(b"abc")
    assert ps.size(sess) == 3


def test_enable_truncates_existing_file_and_invokes_tmux(ps, monkeypatch):
    """`enable()` always truncates so a fresh stream starts clean."""
    sess = "enabletest"
    tf = ps.term_file(sess)
    tf.write_bytes(b"old content")
    import subprocess as _sp
    calls = []
    def fake_run(*a, **kw):
        calls.append(list(a[0]) if a else None)
        class R: returncode = 0
        return R()
    monkeypatch.setattr(_sp, "run", fake_run)
    ps.enable(sess)
    assert tf.stat().st_size == 0
    # And we DID try to set up pipe-pane on the right session
    assert any("pipe-pane" in (cmd or []) and sess in (cmd or [])
               for cmd in calls)


def test_enable_with_mirror_to_uses_tee(ps, monkeypatch, tmp_path):
    """When mirror_to is given, pipe-pane should `tee` to both files."""
    sess = "mirrortest"
    log = tmp_path / "the.log"
    import subprocess as _sp
    captured = {}
    def fake_run(*a, **kw):
        cmd = list(a[0]) if a else []
        if "pipe-pane" in cmd:
            captured["cmd"] = cmd
        class R: returncode = 0
        return R()
    monkeypatch.setattr(_sp, "run", fake_run)
    ps.enable(sess, mirror_to=str(log))
    assert "cmd" in captured
    # The shell command (last arg of `tmux pipe-pane -t SESS -o "..."`)
    # should use tee + redirection to both files.
    shell_cmd = captured["cmd"][-1]
    assert "tee -a" in shell_cmd
    assert str(ps.term_file(sess)) in shell_cmd
    assert str(log) in shell_cmd


def test_read_range_handles_negative_offset(ps):
    sess = "neg"
    tf = ps.term_file(sess)
    tf.write_bytes(b"hello")
    # Negative offset should be clamped to 0, not crash.
    chunk, off, size = ps.read_range(sess, -5)
    assert chunk == b"hello"
    assert off == 5
