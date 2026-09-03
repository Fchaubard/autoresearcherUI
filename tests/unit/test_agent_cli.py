from __future__ import annotations

import pytest


def test_agent_cli_builds_each_provider_command(monkeypatch):
    from backend.app import agent_cli
    monkeypatch.setattr(agent_cli, "_binary", lambda provider: {
        "claude": "claude", "openai": "codex", "gemini": "gemini"}[provider])

    provider, claude = agent_cli.command("claude-fable-5-1", "high", "brief")
    assert provider == "claude"
    assert "claude --dangerously-skip-permissions" in claude
    assert "brief" not in claude

    provider, codex = agent_cli.command("gpt-5.6-sol", "xhigh", "read brief")
    assert provider == "openai"
    assert "codex --dangerously-bypass-approvals-and-sandbox" in codex
    assert "model_reasoning_effort=xhigh" in codex
    assert "read brief" in codex

    provider, gemini = agent_cli.command("gemini-3.8-flash", "high", "read brief")
    assert provider == "gemini"
    assert "gemini --approval-mode=yolo" in gemini
    assert "--prompt-interactive 'read brief'" in gemini


def test_agent_cli_reports_missing_provider_binary(monkeypatch):
    from backend.app import agent_cli
    monkeypatch.setattr(agent_cli, "_binary", lambda provider: (_ for _ in ()).throw(
        agent_cli.AgentCLIUnavailable("codex CLI is required")))
    with pytest.raises(agent_cli.AgentCLIUnavailable, match="codex CLI"):
        agent_cli.command("gpt-5.6-sol")


def test_real_agent_never_types_credentials_into_pane(tmp_path, monkeypatch):
    from backend.app.agent import RealAgent
    import subprocess

    calls = []

    class Result:
        returncode = 0
        stdout = b""
        stderr = b""

    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, *args, **kwargs:
                        calls.append(cmd) or Result())
    from backend.app import pane_stream
    monkeypatch.setattr(pane_stream, "enable", lambda *args, **kwargs: None)
    monkeypatch.setattr(pane_stream, "apply_remembered_size",
                        lambda *args, **kwargs: None)
    secret = "test-secret-never-echo"
    agent = RealAgent(str(tmp_path), "project", "http://local", str(tmp_path),
                      agent_cmd=["true"], openai_key=secret)
    agent.start()
    typed = [cmd for cmd in calls
             if isinstance(cmd, list) and cmd[:2] == ["tmux", "send-keys"]]
    assert all(secret not in " ".join(cmd) for cmd in typed)
    respawns = [cmd for cmd in calls
                if isinstance(cmd, list) and cmd[:2] == ["tmux", "respawn-pane"]]
    assert len(respawns) == 1
    assert f"OPENAI_API_KEY={secret}" in respawns[0]
    assert any(cmd[:4] == ["tmux", "set-window-option", "-t", "agent"]
               and cmd[-2:] == ["remain-on-exit", "on"]
               for cmd in calls if isinstance(cmd, list))
