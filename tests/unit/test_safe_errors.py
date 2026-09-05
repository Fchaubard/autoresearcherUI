from __future__ import annotations

import subprocess


def test_called_process_error_never_serializes_command_or_secrets():
    from backend.app.safe_errors import describe
    secret = "provider-secret-must-not-appear"
    exc = subprocess.CalledProcessError(
        1, ["tmux", "respawn-pane", "-e", f"OPENAI_API_KEY={secret}"])
    text = describe(exc)
    assert text == "CalledProcessError(returncode=1)"
    assert secret not in text
    assert "OPENAI_API_KEY" not in text


def test_timeout_error_never_serializes_command():
    from backend.app.safe_errors import describe
    exc = subprocess.TimeoutExpired(["command", "secret-value"], 5)
    assert describe(exc) == "TimeoutExpired(timeout=5)"
