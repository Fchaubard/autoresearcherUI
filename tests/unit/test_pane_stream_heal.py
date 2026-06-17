"""pane_stream.ensure_piped() self-heals a dropped tmux pipe-pane.

Regression for the "author terminal frozen" bug: pipe-pane drops, the raw
mirror stops growing, the rail xterm shows nothing new, and nothing re-enabled
it (sweep_enable_all skips the author/agent infra sessions).
"""
from __future__ import annotations


def test_ensure_piped_reenables_when_dropped(arui_env, monkeypatch):
    from backend.app import pane_stream
    monkeypatch.setattr(pane_stream, "is_piped", lambda s: False)
    called = []
    monkeypatch.setattr(pane_stream, "enable",
                        lambda s, **k: called.append(s) or None)
    assert pane_stream.ensure_piped("author") is True
    assert called == ["author"]


def test_ensure_piped_noop_when_healthy(arui_env, monkeypatch):
    from backend.app import pane_stream
    monkeypatch.setattr(pane_stream, "is_piped", lambda s: True)
    called = []
    monkeypatch.setattr(pane_stream, "enable",
                        lambda s, **k: called.append(s) or None)
    assert pane_stream.ensure_piped("author") is False
    assert called == []


def test_ensure_piped_noop_when_session_absent(arui_env, monkeypatch):
    from backend.app import pane_stream
    # is_piped returns None when the session doesn't exist -> don't re-enable
    monkeypatch.setattr(pane_stream, "is_piped", lambda s: None)
    called = []
    monkeypatch.setattr(pane_stream, "enable",
                        lambda s, **k: called.append(s) or None)
    assert pane_stream.ensure_piped("author") is False
    assert called == []
