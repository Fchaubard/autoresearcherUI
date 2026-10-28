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


def test_busy_detection_recognizes_gemini_screen_reader_footer(monkeypatch):
    from backend.app import author_agent as author
    monkeypatch.setattr(author, "_pane_text", lambda session:
                        "gemini-3.8-flash\nresponding Thinking... (esc to cancel, 6s) ? for shortcuts\n"
                        "YOLO Ctrl+Y\nYOLO mode Type your message\n" + "\n" * 30)
    assert author._looks_busy("author") is True


def test_author_contract_authenticates_every_local_api_call():
    from backend.app import author_agent as author

    assert "X-Arui-Passcode: $ARUI_INGEST_TOKEN" in author.SYSTEM
    assert "$ARUI_INGEST_TOKEN" in author._AUTHOR_BRIEF
    assert "passcode required" in author.SYSTEM


def test_author_stops_before_launch_when_settings_unreadable(arui_env, monkeypatch):
    from backend.app import author_agent as author
    monkeypatch.setattr(author, "_tmux_alive", lambda session: False)
    monkeypatch.setattr(author, "SessionLocal", lambda: (_ for _ in ()).throw(
        RuntimeError("db unavailable with sensitive details")))
    monkeypatch.setattr(author.subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("must not touch sessions")))
    result = author.start()
    assert result["status"] == "error"
    assert "could not be read" in result["detail"]
    assert "sensitive" not in result["detail"]


def test_author_stops_before_launch_when_settings_missing(arui_env, monkeypatch):
    from backend.app import author_agent as author
    monkeypatch.setattr(author, "_tmux_alive", lambda session: False)
    monkeypatch.setattr(author.subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("must not touch sessions")))
    result = author.start()
    assert result["status"] == "error"
    assert "missing" in result["detail"]


def test_author_does_not_confirm_unavailable_model_picker(monkeypatch):
    from backend.app import author_agent as author
    monkeypatch.setattr(author, "_tmux_alive", lambda session: True)
    monkeypatch.setattr(author, "_pane_text", lambda session: "Unknown model. ❯ Select a different model")
    monkeypatch.setattr(author, "_send_keys", lambda *a, **kw: (_ for _ in ()).throw(
        AssertionError("must not accept fallback")))
    assert author._feed_brief_inner("author", "brief", 1) is False


def test_author_handles_new_trust_screen_before_brief(monkeypatch):
    from backend.app import author_agent as author
    screens = iter(["❯ No, exit\nYes, I trust this folder", "❯ Ask Claude"])
    sent = []
    monkeypatch.setattr(author, "_tmux_alive", lambda session: True)
    monkeypatch.setattr(author, "_pane_text", lambda session: next(screens, "❯ Ask Claude"))
    monkeypatch.setattr(author, "_send_keys", lambda session, *keys, literal=None:
                        sent.append(literal if literal is not None else keys))
    monkeypatch.setattr(author, "_looks_busy", lambda session: True)
    monkeypatch.setattr(author.time, "sleep", lambda seconds: None)
    assert author._feed_brief_inner("author", "brief", 1) is True
    assert sent[:3] == [("Home", "Down", "Enter"), "brief", ("Enter",)]
