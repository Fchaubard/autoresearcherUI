from __future__ import annotations


def test_codex_welcome_screen_is_recognized_and_brief_is_submitted(monkeypatch):
    from backend.app import author_agent as author

    sent = []
    monkeypatch.setattr(author, "_tmux_alive", lambda session: True)
    monkeypatch.setattr(author, "_pane_text",
                        lambda session: "› Ask Codex to do anything")
    monkeypatch.setattr(author, "_send_keys",
                        lambda session, *args, literal=None:
                        sent.append(literal if literal is not None else args))
    monkeypatch.setattr(author, "_looks_busy", lambda session: True)
    monkeypatch.setattr(author.time, "sleep", lambda seconds: None)

    assert author._feed_brief_inner("author", "generic brief", 1) is True
    assert sent[:2] == ["generic brief", ("Enter",)]


def test_busy_detection_ignores_stale_token_scrollback(monkeypatch):
    from backend.app import author_agent as author

    monkeypatch.setattr(
        author, "_pane_text",
        lambda session: "↑ 10k tokens\nFinished earlier\n› Ask Codex to do anything")
    assert author._looks_busy("author") is False


def test_busy_detection_recognizes_live_codex_turn(monkeypatch):
    from backend.app import author_agent as author

    monkeypatch.setattr(
        author, "_pane_text",
        lambda session: "Working (12s) · esc to interrupt")
    assert author._looks_busy("author") is True
