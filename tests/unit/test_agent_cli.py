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


def test_binary_prefers_standalone_codex_over_path_npm_wrapper(tmp_path,
                                                               monkeypatch):
    from backend.app import agent_cli
    standalone = (tmp_path / ".codex" / "packages" / "standalone" /
                  "current" / "bin" / "codex")
    standalone.parent.mkdir(parents=True)
    standalone.touch()
    npm = tmp_path / "usr" / "local" / "bin" / "codex"
    npm.parent.mkdir(parents=True)
    npm.touch()
    monkeypatch.delenv("ARUI_CODEX_BIN", raising=False)
    monkeypatch.setattr(agent_cli.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(agent_cli.shutil, "which",
                        lambda name: str(npm) if name == "codex" else None)

    chosen = agent_cli._binary("openai")

    assert str(standalone) in chosen
    assert str(npm) not in chosen


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


def test_gemini_unattended_settings_are_local_and_contain_no_key(tmp_path, monkeypatch):
    import json
    from backend.app.agent_cli import prepare_environment

    monkeypatch.setenv("GEMINI_API_KEY", "secret-must-stay-in-env")
    env = prepare_environment("gemini-3.8-flash", tmp_path)
    assert env["GEMINI_CLI_HOME"] == str(tmp_path)
    for path in (tmp_path / "settings.json", tmp_path / ".gemini" / "settings.json"):
        raw = path.read_text()
        config = json.loads(raw)
        assert config["security"]["auth"]["selectedType"] == "gemini-api-key"
        assert config["general"]["enableAutoUpdate"] is False
        assert config["experimental"]["dynamicModelConfiguration"] is True
        assert config["modelConfigs"]["modelIdResolutions"]["gemini-3.8-flash"] == {
            "default": "gemini-3.8-flash", "contexts": []}
        for chain in config["modelConfigs"]["modelChains"].values():
            assert [item["model"] for item in chain] == ["gemini-3.8-flash"]
            assert "silent" not in chain[0]["actions"].values()
        assert "secret-must-stay-in-env" not in raw


def test_codex_does_not_run_self_update_or_select_fallback(monkeypatch):
    from backend.app import agent_cli
    monkeypatch.setattr(agent_cli, "_binary", lambda provider: "codex")
    _, cmd = agent_cli.command("gpt-5.6-sol")
    assert "check_for_update_on_startup=false" in cmd
    assert "--model gpt-5.6-sol" in cmd
    assert 'model_provider="arui_openai"' in cmd
    assert 'base_url="https://api.openai.com/v1"' in cmd
    assert 'env_key="OPENAI_API_KEY"' in cmd
    assert 'requires_openai_auth=false' in cmd
    assert "fallback" not in cmd


def test_model_failure_guard_does_not_match_research_fallback_prose():
    from backend.app.agent_cli import model_selection_failed
    assert not model_selection_failed("If the experiment fails, fall back to the baseline.\n❯ Ask Codex")
    assert model_selection_failed("Model not found. Switch to gemini-2.5-flash?")


def test_claude_overrides_inherited_fallback_chain_with_selected_model(monkeypatch):
    import json
    import shlex
    from backend.app import agent_cli
    monkeypatch.setattr(agent_cli, "_binary", lambda provider: "claude")
    _, cmd = agent_cli.command("claude-fable-5-1")
    argv = shlex.split(cmd)
    assert argv[argv.index("--model") + 1] == "claude-fable-5-1"
    assert argv[argv.index("--fallback-model") + 1] == "claude-fable-5-1"
    settings = json.loads(argv[argv.index("--settings") + 1])
    assert settings["availableModels"] == ["claude-fable-5-1"]
    assert settings["switchModelsOnFlag"] is False


def test_codex_trust_is_scoped_to_selected_folder_and_enclosing_repo(tmp_path, monkeypatch):
    import json
    import shlex
    from backend.app import agent_cli
    repo = tmp_path / "selected project"
    (repo / ".git").mkdir(parents=True)
    paper = repo / "latex"
    paper.mkdir()
    monkeypatch.setattr(agent_cli, "_binary", lambda provider: "codex")
    _, cmd = agent_cli.command("gpt-5.6-sol", workspace=paper)
    argv = shlex.split(cmd)
    settings = [argv[i + 1] for i, value in enumerate(argv[:-1]) if value == "-c"]
    assert "projects." + json.dumps(str(repo)) + '.trust_level="trusted"' in settings
    assert "projects." + json.dumps(str(paper)) + '.trust_level="trusted"' in settings
    assert not any(json.dumps(str(tmp_path)) in value for value in settings)


def test_codex_first_project_trust_accepts_only_exact_dialog(monkeypatch):
    from types import SimpleNamespace
    from backend.app import agent_cli
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="Do you trust the contents of this directory?\n1. Yes, continue")
    monkeypatch.setattr(agent_cli.subprocess, "run", run)
    monkeypatch.setattr(agent_cli.time, "sleep", lambda seconds: None)
    assert agent_cli.accept_codex_project_trust("probe") is True
    assert calls[-1] == ["tmux", "send-keys", "-t", "=probe:", "Enter"]


def test_codex_trust_helper_never_confirms_model_replacement(monkeypatch):
    from types import SimpleNamespace
    from backend.app import agent_cli
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="Unknown model. Select a different model?\n1. Yes, continue")
    monkeypatch.setattr(agent_cli.subprocess, "run", run)
    assert agent_cli.accept_codex_project_trust("probe") is False
    assert len(calls) == 1


def test_codex_trust_helper_reaches_exact_private_tmux_pane(arui_env, tmp_path):
    import shlex
    import shutil
    import subprocess
    import sys
    import time
    from backend.app.agent_cli import accept_codex_project_trust
    if not shutil.which("tmux"):
        pytest.skip("tmux not installed")
    accepted = tmp_path / "accepted"
    code = ("from pathlib import Path; import time; "
            "print('Do you trust the contents of this directory?\\n1. Yes, continue', flush=True); "
            f"input(); Path({str(accepted)!r}).touch(); time.sleep(2)")
    subprocess.run(["tmux", "new-session", "-d", "-s", "probe", "-c", str(tmp_path),
                    shlex.join([sys.executable, "-c", code])], check=True, capture_output=True)
    assert accept_codex_project_trust("probe", timeout=5) is True
    deadline = time.monotonic() + 2
    while not accepted.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert accepted.exists()


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
    assert len(login) == 0
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
