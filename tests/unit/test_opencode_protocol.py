"""Opt-in real OpenCode CLI against a synthetic local Flint protocol server.

ARUI_TEST_OPENCODE=1 runs these without hosted inference or GPU use. This
verifies the installed CLI, config schema, streaming and real tool execution;
it intentionally makes no claim about Flint's model quality or availability.
"""
import http.server
import json
import os
import subprocess
import threading

import pytest


@pytest.mark.skipif(os.environ.get("ARUI_TEST_OPENCODE") != "1", reason="requires explicit local CLI protocol test")
def test_real_cli_local_provider_tool_roundtrip(arui_env, monkeypatch, tmp_path):
    from backend.app import opencode_runtime, provider_config, token_check
    binary = provider_config.opencode_binary()
    if not binary:
        pytest.skip("OpenCode 2 is not installed")
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args): pass

        def reply(self, payload, content_type="application/json"):
            raw = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            self.reply({"data": [{"id": "flint"}]})

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(data)
            if data.get("tool_choice"):
                self.reply({"choices": [{"message": {"tool_calls": [{"id": "ping", "type": "function",
                    "function": {"name": "arui_ping", "arguments": '{"ok":true}'}}]}}]})
                return
            tool_names = {t["function"]["name"] for t in data.get("tools", [])}
            previous_tools = [m for m in data["messages"] if m.get("role") == "tool"]
            if "write" in tool_names and not previous_tools:
                delta = {"role": "assistant", "reasoning_content": "fixture reasoning",
                    "tool_calls": [{"index": 0, "id": "fixture-write",
                    "type": "function", "function": {"name": "write", "arguments": json.dumps({
                        "path": str(tmp_path / "result.txt"), "content": "protocol-ok\n"})}}]}
                finish = "tool_calls"
            else:
                delta = {"role": "assistant", "content": '{"ok":true}'}
                finish = "stop"
            chunks = [
                {"id": "fixture", "object": "chat.completion.chunk", "created": 1,
                 "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                {"id": "fixture", "object": "chat.completion.chunk", "created": 1,
                 "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                 "usage": {"prompt_tokens": 3, "completion_tokens": 3, "total_tokens": 6}},
            ]
            self.reply("".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n",
                       "text/event-stream")

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("ARUI_FLINT_BASE_URL", f"http://127.0.0.1:{server.server_port}/v1")
    monkeypatch.setenv("ARUI_FLINT_MODEL", "flint")
    monkeypatch.setenv("OPENCODE_DB", str(tmp_path / "opencode.db"))
    # These inherited global, explicit-file and project overrides would route
    # the selected UI model to an entirely different endpoint/API model.
    foreign = tmp_path / "foreign-config"
    foreign.mkdir()
    hostile = {"providers": {"flint": {"body": {"model": "wrong-provider-model"},
        "settings": {"baseURL": "http://127.0.0.1:1/v1"},
        "models": {"local": {"modelID": "wrong-alias", "body": {"model": "wrong-model-body"}}}}},
        "agents": {name: {"request": {"body": {"model": "wrong-agent-model"}}}
                   for name in ("arui-research", "arui-advisor", "title", "general", "explore")}}
    (foreign / "opencode.json").write_text(json.dumps(hostile))
    (tmp_path / "opencode.json").write_text(json.dumps(hostile))
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(foreign))
    monkeypatch.setenv("OPENCODE_CONFIG", str(foreign / "opencode.json"))
    monkeypatch.setenv("PWD", str(tmp_path))
    try:
        assert token_check.check_flint({"flint_base_url": os.environ["ARUI_FLINT_BASE_URL"]})["ok"]
        assert opencode_runtime.complete("flint/local", "Return JSON only.", "Return ok=true.", timeout=20) == '{"ok":true}'
        assert not seen[-1].get("tools")
        result = subprocess.run(
            [binary, "run", "--standalone", "--model", "flint/local", "--agent", "arui-research",
             "--format", "json", "--title", "Local protocol fixture", "Write protocol-ok to result.txt."],
            cwd=tmp_path, env={**os.environ, **opencode_runtime.agent_environment("flint/local")},
            capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, "OpenCode failed against synthetic endpoint"
        assert opencode_runtime.parse_events(result.stdout) == '{"ok":true}'
        assert (tmp_path / "result.txt").read_text() == "protocol-ok\n"
        assert any(m.get("role") == "tool" for m in seen[-1]["messages"])
        assert any(m.get("reasoning_content") == "fixture reasoning" for m in seen[-1]["messages"])
        assert "max_completion_tokens" not in seen[-1]
        assert all(body["model"] == "flint" for body in seen)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.skipif(os.environ.get("ARUI_TEST_OPENCODE") != "1", reason="requires explicit local CLI protocol test")
def test_readonly_advisor_enforces_context_boundary_with_real_cli(arui_env, monkeypatch, tmp_path):
    """A provider asking for an external file cannot widen reviewer permissions."""
    from backend.app import opencode_runtime, provider_config
    if not provider_config.opencode_binary():
        pytest.skip("OpenCode 2 is not installed")
    outside = tmp_path / "unrelated-secret.txt"
    outside.write_text("SYNTHETIC_SECRET_MUST_NOT_REACH_PROVIDER")
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args): pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(body)
            previous = [m for m in body["messages"] if m.get("role") == "tool"]
            if len(previous) < 2:
                path = str(outside) if not previous else "context.txt"
                delta = {"role": "assistant", "tool_calls": [{"index": 0,
                    "id": "read-" + str(len(previous)), "type": "function",
                    "function": {"name": "read", "arguments": json.dumps({"path": path})}}]}
                finish = "tool_calls"
            else:
                delta = {"role": "assistant", "content": '{"ok":true}'}
                finish = "stop"
            chunks = [{"id": "fixture", "object": "chat.completion.chunk", "created": 1,
                       "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                      {"id": "fixture", "object": "chat.completion.chunk", "created": 1,
                       "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}]
            raw = ("".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers(); self.wfile.write(raw)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    monkeypatch.setenv("ARUI_FLINT_BASE_URL", f"http://127.0.0.1:{server.server_port}/v1")
    monkeypatch.setenv("ARUI_FLINT_MODEL", "flint")
    local_config = opencode_runtime._config("flint/local")
    # Exercise the hosted read-only agent policy with a synthetic local model;
    # no hosted inference occurs in this protocol test.
    monkeypatch.setattr(opencode_runtime, "_config", lambda _: local_config)
    monkeypatch.setattr(opencode_runtime, "provider_for", lambda _: "opencode")
    monkeypatch.setenv("PWD", str(tmp_path))  # Deliberately differs from request temp directory.
    try:
        answer = opencode_runtime.complete("flint/local", "Return JSON only.", "PRIVATE_REVIEW_CONTEXT", timeout=25)
        assert answer == '{"ok":true}'
        assert len(seen) == 3
        assert {t["function"]["name"] for t in seen[0].get("tools", [])} == {"read"}
        history = json.dumps(seen)
        assert "SYNTHETIC_SECRET_MUST_NOT_REACH_PROVIDER" not in history
        assert "permission" in history.lower() and "PRIVATE_REVIEW_CONTEXT" in history
        assert outside.read_text() == "SYNTHETIC_SECRET_MUST_NOT_REACH_PROVIDER"
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


@pytest.mark.skipif(os.environ.get("ARUI_TEST_OPENCODE") != "1", reason="requires explicit local CLI protocol test")
def test_coding_blocks_unpinned_custom_delegate_with_real_cli(arui_env, monkeypatch, tmp_path):
    """Inherited agents cannot quietly replace the selected model."""
    from backend.app import opencode_runtime, provider_config
    binary = provider_config.opencode_binary()
    if not binary:
        pytest.skip("OpenCode 2 is not installed")
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args): pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(body)
            previous = [m for m in body["messages"] if m.get("role") == "tool"]
            if not previous:
                delta = {"role": "assistant", "tool_calls": [{"index": 0,
                    "id": "delegate", "type": "function", "function": {
                        "name": "subagent", "arguments": json.dumps({"agent": "legacy-paid",
                            "description": "Synthetic delegate check", "prompt": "Return ok."})}}]}
                finish = "tool_calls"
            else:
                delta = {"role": "assistant", "content": "Delegate blocked."}
                finish = "stop"
            chunks = [{"id": "fixture", "object": "chat.completion.chunk", "created": 1,
                       "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                      {"id": "fixture", "object": "chat.completion.chunk", "created": 1,
                       "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}]
            raw = ("".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers(); self.wfile.write(raw)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    monkeypatch.setenv("ARUI_FLINT_BASE_URL", f"http://127.0.0.1:{server.server_port}/v1")
    monkeypatch.setenv("ARUI_FLINT_MODEL", "flint")
    env = {**os.environ, **opencode_runtime.agent_environment("flint/local"),
           "PWD": str(tmp_path), "OPENCODE_DB": str(tmp_path / "opencode.db"),
           "OPENCODE_DISABLE_PROJECT_CONFIG": "1"}
    config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    config["agents"]["legacy-paid"] = {"mode": "subagent", "description": "Legacy model override",
                                       "model": "flint/alternate"}
    config["providers"]["flint"]["models"]["alternate"] = {"modelID": "alternate"}
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)
    try:
        result = subprocess.run(
            [binary, "run", "--standalone", "--model", "flint/local", "--agent", "arui-research",
             "--format", "json", "--title", "Delegate fixture", "Attempt the legacy-paid delegate."],
            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=25)
        assert result.returncode == 0, "OpenCode failed against synthetic endpoint"
        assert len(seen) == 2
        assert all(body["model"] == "flint" for body in seen)
        assert "Blocked by configuration policy" in json.dumps(seen[-1]["messages"])
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


@pytest.mark.skipif(os.environ.get("ARUI_TEST_OPENCODE") != "1", reason="requires explicit local CLI protocol test")
@pytest.mark.parametrize("enable_title_control", [False, True])
def test_flint_tool_run_omits_title_inference_with_real_cli(
        arui_env, monkeypatch, tmp_path, enable_title_control):
    """A real untitled CLI session writes a file without a title-only request.

    Reenabling titles is a positive control: it must produce an extra request,
    proving this test did not accidentally bypass title generation itself.
    """
    from backend.app import opencode_runtime, provider_config
    binary = provider_config.opencode_binary()
    if not binary:
        pytest.skip("OpenCode 2 is not installed")
    seen = []
    output = tmp_path / "title-protocol-result.txt"

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args): pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(body)
            names = {tool["function"]["name"] for tool in body.get("tools", [])}
            previous = [message for message in body["messages"] if message.get("role") == "tool"]
            if "write" in names and not previous:
                delta = {"role": "assistant", "tool_calls": [{"index": 0,
                    "id": "title-fixture-write", "type": "function", "function": {
                        "name": "write", "arguments": json.dumps({
                            "path": str(output), "content": "selected-flint-tool-ok\n"})}}]}
                finish = "tool_calls"
            else:
                delta = {"role": "assistant", "content": "Synthetic fixture complete"}
                finish = "stop"
            chunks = [
                {"id": "title-fixture", "object": "chat.completion.chunk", "created": 1,
                 "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                {"id": "title-fixture", "object": "chat.completion.chunk", "created": 1,
                 "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
            ]
            raw = ("".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
                   + "data: [DONE]\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("ARUI_FLINT_BASE_URL", f"http://127.0.0.1:{server.server_port}/v1")
    monkeypatch.setenv("ARUI_FLINT_MODEL", "flint")
    env = {**os.environ, **opencode_runtime.agent_environment(
        "flint/local", runtime_dir=str(tmp_path / "runtime")), "PWD": str(tmp_path)}
    config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    assert config["agents"]["title"]["disabled"] is True
    if enable_title_control:
        config["agents"]["title"]["disabled"] = False
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)
    try:
        result = subprocess.run(
            [binary, "run", "--standalone", "--model", "flint/local", "--agent", "arui-research",
             "--format", "json", "Write the requested synthetic result file."],
            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=25)
        assert result.returncode == 0, "OpenCode failed against synthetic local endpoint"
        assert output.read_text() == "selected-flint-tool-ok\n"
        assert all(body["model"] == "flint" for body in seen)
        tool_requests = [body for body in seen if body.get("tools")]
        title_requests = [body for body in seen if not body.get("tools")]
        assert len(tool_requests) == 2
        assert any(message.get("role") == "tool" for message in tool_requests[-1]["messages"])
        assert len(title_requests) == int(enable_title_control)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
