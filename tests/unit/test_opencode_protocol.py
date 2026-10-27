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
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
