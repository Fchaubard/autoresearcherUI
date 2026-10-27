"""Provider protocols and every new model across coding/advisory roles."""
import json
import subprocess

import pytest


FREE_IDS = (
    "nemotron-3-ultra-free", "nemotron-3.5-lightning-free", "nemotron-3.5-super-vl-free",
    "big-pickle", "glm-5.3-flash-free", "deepseek-v4.1-flash-free", "beam-free",
    "flint-40b-free", "mimo-v2.6-flash-free",
)


@pytest.mark.parametrize("short", FREE_IDS)
def test_free_model_all_roles(short, arui_env, monkeypatch):
    from backend.app import agent_cli, council, opencode_runtime, pi, provider_config, token_check
    from backend.app.model_registry import public_registry
    model = "opencode/" + short
    monkeypatch.setattr(provider_config, "opencode_binary", lambda: "/bin/opencode")
    registry = {m["id"]: m for m in public_registry()["models"]}
    assert registry[model]["agent_cli"] and registry[model]["free"]
    seen = []
    monkeypatch.setattr(opencode_runtime, "complete", lambda *args: seen.append(args) or '{"ok":true}')
    for slot in ("gemini", "openai", "claude"):
        cfg = dict(council.DEFAULTS, **{f"council_{slot}_model": model})
        if slot == "claude":
            assert council._claude_available(cfg)
        else:
            assert slot in council._available_reviewers(cfg)
        assert council._CALLERS[slot]("system", "user", cfg) == '{"ok":true}'
    assert pi._call(model, "system", "user") == '{"ok":true}'
    cfg = dict(council.DEFAULTS, scoping_model=model)
    monkeypatch.setattr(council, "_settings", lambda: cfg)
    assert council.scope_chat([], {}, [], "purpose", "metric", "ideas") == '{"ok":true}'
    assert all(args[0] == model for args in seen)
    assert token_check.resolve_advisor({"scoping_model": model}) == {
        "provider": "opencode", "model": model, "warning": ""}
    for prompt in ("", "Read _setup_prompt.txt; don't execute `bad` $(bad)"):
        provider, command = agent_cli.command(model, "high", prompt)
        import shlex
        argv = shlex.split(command)
        assert provider == "opencode"
        assert argv[:3] == ["/bin/opencode", "mini", "--standalone"]
        assert argv[argv.index("--model") + 1] == model
        if prompt:
            assert argv[argv.index("--prompt") + 1] == prompt


@pytest.mark.parametrize("model", ["gemini-4-argon", "gemini-3.1-flash-lite"])
def test_new_gemini_all_roles(model, arui_env, monkeypatch):
    from backend.app import council, pi, agent_cli
    from backend.app.model_registry import public_registry
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic")
    monkeypatch.setattr(agent_cli, "_binary", lambda _: "gemini")
    assert any(m["id"] == model and m["agent_cli"] for m in public_registry()["models"])
    assert agent_cli.command(model)[0] == "gemini"
    requests = []
    def post(url, body, headers):
        requests.append((url, body, headers))
        return {"candidates": [{"content": {"parts": [{"text": '{"ok":true}'}]}}]}
    monkeypatch.setattr(council, "_post_json_retry", post)
    for slot in ("gemini", "openai", "claude"):
        cfg = dict(council.DEFAULTS, **{f"council_{slot}_model": model})
        assert council._CALLERS[slot]("system", "user", cfg) == '{"ok":true}'
    assert pi._call(model, "system", "user") == '{"ok":true}'
    monkeypatch.setattr(council, "_settings", lambda: dict(council.DEFAULTS, scoping_model=model))
    assert council.scope_chat([], {}, [], "purpose", "metric", "") == '{"ok":true}'
    assert all(f"models/{model}:generateContent" in req[0] for req in requests)


def test_local_flint_coding_and_advisors(arui_env, monkeypatch):
    from backend.app import council, pi, opencode_runtime, provider_config
    monkeypatch.setenv("ARUI_FLINT_BASE_URL", "http://127.0.0.1:30000/v1")
    monkeypatch.setenv("ARUI_FLINT_MODEL", "RecursiveSI/flint")
    monkeypatch.setenv("ARUI_FLINT_API_KEY", "local-secret")
    monkeypatch.setattr(provider_config, "opencode_binary", lambda: "/bin/opencode")
    config = json.loads(opencode_runtime.agent_environment("flint/local")["OPENCODE_CONFIG_CONTENT"])
    provider = config["providers"]["flint"]
    assert provider["settings"]["baseURL"] == "http://127.0.0.1:30000/v1"
    assert provider["models"]["local"]["modelID"] == "RecursiveSI/flint"
    assert provider["models"]["local"]["capabilities"]["tools"] is True
    seen = []
    def post(url, body, headers):
        seen.append((url, body, headers))
        return {"choices": [{"message": {"content": '{"ok":true}'}}]}
    monkeypatch.setattr(council, "_post_json_retry", post)
    for slot in ("gemini", "openai", "claude"):
        cfg = dict(council.DEFAULTS, **{f"council_{slot}_model": "flint/local"})
        assert council._CALLERS[slot]("system", "user", cfg) == '{"ok":true}'
    assert pi._call("flint/local", "system", "user") == '{"ok":true}'
    monkeypatch.setattr(council, "_settings", lambda: dict(council.DEFAULTS, scoping_model="flint/local"))
    assert council.scope_chat([], {}, [], "purpose", "metric", "") == '{"ok":true}'
    assert all(url == "http://127.0.0.1:30000/v1/chat/completions" and body["model"] == "RecursiveSI/flint"
               and headers == {"Authorization": "Bearer local-secret"}
               and "response_format" not in body for url, body, headers in seen)


def test_advisor_cli_is_tool_disabled_and_uses_stdin(arui_env, monkeypatch, tmp_path):
    from backend.app import opencode_runtime
    monkeypatch.setattr(opencode_runtime, "opencode_binary", lambda: "/bin/opencode")
    captured = {}
    class Proc:
        returncode = 0
        def __init__(self, argv, **kw):
            captured.update(argv=argv, **kw)
        def communicate(self, user, timeout):
            captured.update(input=user, timeout=timeout)
            return json.dumps({"type": "text", "part": {"text": "answer"}}), ""
    monkeypatch.setattr(opencode_runtime.subprocess, "Popen", Proc)
    assert opencode_runtime.complete("opencode/big-pickle", "system", "private context") == "answer"
    assert "private context" not in captured["argv"] and captured["input"] == "private context"
    assert "--standalone" in captured["argv"]
    cfg = json.loads(captured["env"]["OPENCODE_CONFIG_CONTENT"])
    assert cfg["agents"]["arui-advisor"]["permissions"] == [{"action": "*", "resource": "*", "effect": "deny"}]
    assert cfg["agents"]["arui-advisor"]["system"] == "system"


def test_cli_errors_do_not_leak_response_secrets(arui_env):
    from backend.app.opencode_runtime import parse_events
    with pytest.raises(RuntimeError) as exc:
        parse_events(json.dumps({"type": "error", "error": {"message": "Bearer secret"}}))
    assert "secret" not in str(exc.value)
    with pytest.raises(RuntimeError, match="no assistant text"):
        parse_events('{"type":"step_finish"}')


def test_quota_error_has_actionable_message_without_raw_payload(arui_env):
    from backend.app.opencode_runtime import OpenCodeRequestError, parse_events
    with pytest.raises(OpenCodeRequestError) as exc:
        parse_events(json.dumps({"type": "error", "error": {"status": 429, "message": "secret"}}))
    assert exc.value.status == 429
    assert "usage limit" in str(exc.value) and "secret" not in str(exc.value)


def test_local_missing_endpoint_does_not_report_missing_cloud_key(arui_env, monkeypatch):
    from backend.app import pi
    monkeypatch.delenv("ARUI_FLINT_BASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="server URL"):
        pi._call("flint/local", "system", "user")


def test_served_id_alone_does_not_trigger_local_validation(arui_env, monkeypatch):
    from backend.app import token_check
    monkeypatch.setattr(token_check, "check_flint", lambda cfg: pytest.fail("unused Flint should not be probed"))
    result = token_check.check_all({"flint_model": "flint", "flint_base_url": ""})
    assert "flint" not in result


def test_failed_author_restart_is_not_reported_as_success(arui_env, monkeypatch):
    from backend.app import agent_watcher, author_agent
    monkeypatch.setattr(author_agent, "start", lambda: {"status": "error", "detail": "launch failed"})
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0))
    assert agent_watcher._restart_session("author") is False


def test_opencode_mini_live_footer_prevents_duplicate_brief(arui_env, monkeypatch):
    from backend.app import author_agent, supervisor
    frame = "Ask anything, / for commands, @ for context...\nesc interrupt - arui-research - Flint - Local Flint"
    monkeypatch.setattr(author_agent, "_pane_text", lambda _: frame)
    assert author_agent._looks_busy("author")
    assert supervisor._agent_busy(frame.lower())
    assert supervisor._agent_idle_prompt("ask anything, / for commands, @ for context...")


@pytest.mark.parametrize("value", ["file:///tmp/a", "http://user:password@localhost/v1", "http://localhost/v1?key=secret", ""])
def test_invalid_local_endpoint_is_rejected(value, arui_env):
    from backend.app.provider_config import requirement_error
    assert requirement_error("flint/local", {"flint_base_url": value})


def test_flint_preflight_requires_tool_protocol(arui_env, monkeypatch):
    from backend.app import token_check
    class Response:
        def __init__(self, data): self.data = json.dumps(data).encode()
        def read(self): return self.data
        def __enter__(self): return self
        def __exit__(self, *args): return False
    requests = []
    def open_(req, timeout):
        requests.append(req)
        if req.full_url.endswith("/models"):
            return Response({"data": [{"id": "flint"}]})
        return Response({"choices": [{"message": {"content": "I cannot call tools"}}]})
    monkeypatch.setattr(token_check.urllib.request, "urlopen", open_)
    result = token_check.check_flint({"flint_base_url": "http://localhost:30000/v1"})
    assert result["ok"] is False and "tool calls failed" in result["detail"]
    assert json.loads(requests[-1].data)["tool_choice"]["function"]["name"] == "arui_ping"


def test_resume_preserves_workspace_after_display_rename(arui_env, monkeypatch, make_project, tmp_path):
    from backend.app import realrun
    workspace = tmp_path / "original-research"
    workspace.mkdir()
    make_project(name="renamed-display", repo_path=str(workspace))
    monkeypatch.setenv("ARUI_CLAUDE_BIN", "/bin/true")
    captured = {}
    class Agent:
        def __init__(self, **kw): captured.update(kw)
        def start(self): pass
    monkeypatch.setattr(realrun, "RealAgent", Agent)
    realrun.start_real({"repo_name": "new-name", "purpose": "existing purpose"}, resume=True)
    assert captured["workspace"] == str(workspace)
