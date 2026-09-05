from __future__ import annotations

import pytest


def test_binary_prefers_active_path_cli_over_stale_nvm(tmp_path, monkeypatch):
    """A Codex standalone migration must not relaunch an old NVM shim."""
    from backend.app import agent_cli

    active = tmp_path / "local" / "bin" / "codex"
    active.parent.mkdir(parents=True)
    active.touch()
    stale = tmp_path / ".nvm" / "versions" / "node" / "v22" / "bin" / "codex"
    stale.parent.mkdir(parents=True)
    stale.touch()
    monkeypatch.delenv("ARUI_CODEX_BIN", raising=False)
    monkeypatch.setattr(agent_cli.shutil, "which",
                        lambda name: str(active) if name == "codex" else None)
    monkeypatch.setattr(agent_cli.Path, "home", lambda: tmp_path)

    chosen = agent_cli._binary("openai")

    assert str(active) in chosen
    assert str(stale) not in chosen


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

    provider, astra = agent_cli.command("gpt-6-astra", "max", "read brief")
    assert provider == "openai"
    assert "--model gpt-6-astra" in astra
    assert "model_reasoning_effort=max" in astra

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
    from backend.app import agent as agent_mod
    import subprocess

    calls = []

    class Result:
        returncode = 0
        stdout = b""
        stderr = b""

    monkeypatch.setattr(subprocess, "run",
                        lambda cmd, *args, **kwargs:
                        calls.append(cmd) or Result())
    # GitHub's clean unit runner intentionally has no provider CLIs. Binary
    # discovery is not under test here; pin it so the credential transport
    # assertions exercise the same path on every machine.
    monkeypatch.setattr(agent_mod.shutil, "which",
                        lambda name: "/usr/bin/codex"
                        if name == "codex" else None)
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
    login = [cmd for cmd in calls
             if isinstance(cmd, list) and "--with-api-key" in cmd]
    assert len(login) == 1
    assert all(secret not in " ".join(cmd) for cmd in login)
    respawns = [cmd for cmd in calls
                if isinstance(cmd, list) and cmd[:2] == ["tmux", "respawn-pane"]]
    assert len(respawns) == 1
    assert f"OPENAI_API_KEY={secret}" in respawns[0]
    assert any(cmd[:4] == ["tmux", "set-window-option", "-t", "agent"]
               and cmd[-2:] == ["remain-on-exit", "on"]
               for cmd in calls if isinstance(cmd, list))


def test_initial_launch_failure_remains_recoverable(arui_env, monkeypatch):
    from backend.app import realrun
    monkeypatch.setattr(realrun, "claude_binary_present", lambda: True)
    monkeypatch.setattr(realrun.RealAgent, "start",
                        lambda self: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError, match="boom"):
        realrun.start_real({"repo_name": "launch-failure",
                            "research_agent_model": "claude-opus-5"})
    assert realrun.expected() is True


def test_stop_kills_canonical_agent_after_backend_lost_handle(
        arui_env, monkeypatch):
    from backend.app import realrun
    calls = []
    monkeypatch.setattr(realrun.subprocess, "run",
                        lambda argv, **kwargs: calls.append(argv))
    realrun._agent = None
    realrun.set_expected(True, reason="running before backend restart")
    realrun.stop()
    assert ["tmux", "kill-session", "-t", "agent"] in calls
    assert realrun.expected() is False
