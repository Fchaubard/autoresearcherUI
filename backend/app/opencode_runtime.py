"""OpenCode 2 adapters for free hosted models and local Flint coding.

Free Console chat models require the OpenCode client. Advisory calls use a
read-only primary agent in a temporary directory, never a coding agent
in the live research tree. Standalone servers pick up current settings and
do not mutate or restart the user's shared OpenCode service.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile

from .model_registry import provider_for
from .provider_config import flint_config, flint_limits, opencode_binary

_OPENCODE_INFERENCE_URL = "https://opencode.ai/inference/openai/v1"
_OPENAI_COMPATIBLE_PACKAGE = "@opencode/ai/providers/openai-compatible"


class OpenCodeRequestError(RuntimeError):
    """A provider failure with safe, actionable classification."""

    def __init__(self, status: int | None = None, *, category: str = ""):
        self.status = status
        self.error_category = category or ("quota_exhausted" if status == 429 else
            "authentication" if status in {401, 403} else "provider_request_failed")
        if status == 429:
            message = "OpenCode free usage limit reached or rate limited; retry when quota resets or select another provider"
        elif category == "model_unavailable":
            message = "The selected OpenCode model is unavailable; retry later or explicitly select another model"
        elif status in {401, 403}:
            message = "OpenCode authentication or model access was rejected; check the configured connection"
        elif status == 400:
            message = "OpenCode rejected the model request; the selected model may be temporarily unavailable"
        else:
            message = "OpenCode model request failed; check model access, quota and server availability"
        super().__init__(message)


def _config(model: str) -> dict:
    provider = provider_for(model)
    cfg = {"model": model,
           # Pin maintenance requests too; a user's global small/default
           # model must not turn a free/local role into a paid provider call.
           "agents": {name: {"model": model} for name in
                      ("build", "plan", "general", "explore", "title", "summary", "compaction")},
           "policies": [{"action": "provider.use", "resource": "*", "effect": "deny"},
                        {"action": "provider.use", "resource": provider, "effect": "allow"},
                        # V2 has no model.use policy. Only explicitly pinned
                        # built-in delegates may run; inherited custom agents
                        # might otherwise select another model in this provider.
                        {"action": "tool.use", "resource": "subagent:*", "effect": "deny"},
                        {"action": "tool.use", "resource": "subagent:general", "effect": "allow"},
                        {"action": "tool.use", "resource": "subagent:explore", "effect": "allow"}]}
    if provider == "opencode":
        selected_id = model.split("/", 1)[1]
        # Console-managed provider settings are still available with an env
        # API key. Pin the official free route and payload after those defaults.
        cfg["providers"] = {"opencode": {
            "package": _OPENAI_COMPATIBLE_PACKAGE,
            "settings": {"baseURL": _OPENCODE_INFERENCE_URL},
            "body": {"model": selected_id},
            "models": {selected_id: {"modelID": selected_id,
                "package": _OPENAI_COMPATIBLE_PACKAGE,
                "settings": {"baseURL": _OPENCODE_INFERENCE_URL},
                "body": {"model": selected_id}}}}}
    if provider_for(model) == "flint":
        # V2's supported `disabled` flag removes the decorative title agent.
        # Local reasoning models can otherwise spend minutes naming a session
        # before the real coding request reaches the one-slot CPU server.
        cfg["agents"]["title"]["disabled"] = True
        url, served_model, token = flint_config()
        context, output = flint_limits()
        cfg["providers"] = {"flint": {
            "name": "Local Flint",
            "package": _OPENAI_COMPATIBLE_PACKAGE,
            "settings": {"baseURL": url, "apiKey": token or "unused"},
            "models": {"local": {"name": "Flint", "modelID": served_model,
                                   "capabilities": {"tools": True, "input": ["text"],
                                                    "output": ["text"],
                                                    "reasoning": {"supported": True}},
                                   "compatibility": {"reasoningField": "reasoning_content",
                                                     "maxTokensField": "max_tokens"},
                                   "limit": {"context": context, "output": output}}},
        }}
    api_model = _api_model(cfg, model)
    for agent in cfg["agents"].values():
        agent["request"] = {"body": {"model": api_model}}
    return cfg


def _api_model(cfg: dict, model: str) -> str:
    provider, name = model.split("/", 1)
    return cfg["providers"][provider]["models"][name]["modelID"]


def _isolated_environment(runtime_dir: str | None = None) -> dict[str, str]:
    """Keep user config, aliases, plugins and saved accounts out of role routing."""
    if runtime_dir is None:
        from .config import DATA_DIR
        root = DATA_DIR / "opencode-runtime"
    else:
        root = Path(runtime_dir)
    folder = root / "config"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    empty = root / "empty.json"
    if not empty.exists():
        empty.write_text("{}\n", encoding="utf-8")
        empty.chmod(0o600)
    return {"OPENCODE_CONFIG_DIR": str(folder.resolve()),
            "OPENCODE_CONFIG": str(empty.resolve()),
            "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
            "OPENCODE_DB": str((root / "sessions.db").resolve()),
            "OPENCODE_CLI_CONFIG_CONTENT": json.dumps({"attention": {"notifications": False, "sound": False}})}


def agent_environment(model: str, *, runtime_dir: str | None = None) -> dict[str, str]:
    """Environment-only config; no credentials are put in shell commands."""
    if provider_for(model) not in {"opencode", "flint"}:
        return {}
    cfg = _config(model)
    cfg["agents"]["arui-research"] = {
        "model": model,
        "request": {"body": {"model": _api_model(cfg, model)}},
        "mode": "primary", "description": "Autoresearcher coding agent",
        "permissions": [{"action": "*", "resource": "*", "effect": "allow"}],
    }
    return {**_isolated_environment(runtime_dir), "OPENCODE_CONFIG_CONTENT": json.dumps(cfg)}


def parse_events(output: str) -> str:
    texts = []
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "error":
            # CLI error payloads can contain request URLs or auth headers.
            error = event.get("error")
            status = error.get("status") if isinstance(error, dict) else None
            detail = str(error.get("message", "")).lower() if isinstance(error, dict) else ""
            category = "model_unavailable" if "model" in detail and any(
                word in detail for word in ("unavailable", "not found", "does not exist")) else ""
            raise OpenCodeRequestError(status if isinstance(status, int) else None,
                                       category=category)
        if event.get("type") == "text":
            part = event.get("part") or {}
            value = part.get("text") or event.get("text")
            if isinstance(value, str):
                texts.append(value)
    if not texts:
        raise RuntimeError("OpenCode returned no assistant text")
    return "\n".join(texts)


def _read_context_completed(output: str, folder: str) -> bool:
    expected = Path(folder) / "context.txt"
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(event, dict) or event.get("type") != "tool_use":
            continue
        part = event.get("part") or {}
        state = part.get("state") or {}
        if part.get("tool") != "read" or state.get("status") != "completed":
            continue
        name = (state.get("input") or {}).get("path")
        if isinstance(name, str) and (Path(folder) / name).resolve() == expected:
            return True
    return False


def complete(model: str, system: str, user: str, *, timeout: float = 180) -> str:
    binary = opencode_binary()
    if not binary:
        raise RuntimeError("OpenCode 2 CLI is required for free OpenCode models")
    cfg = _config(model)
    cfg["agents"]["arui-advisor"] = {
        "model": model,
        "request": {"body": {"model": _api_model(cfg, model)}},
        "mode": "primary", "system": system,
        "permissions": [{"action": "*", "resource": "*", "effect": "deny"}],
    }
    env = {**os.environ, "OPENCODE_CONFIG_CONTENT": json.dumps(cfg),
           "OPENCODE_DISABLE_PROJECT_CONFIG": "1"}
    # stdin avoids command-line size limits and keeps research context out
    # of the process list. OpenCode consumes piped stdin as the user message.
    with tempfile.TemporaryDirectory(prefix="arui-advisor-") as folder:
        env.update(_isolated_environment(os.path.join(folder, "runtime")))
        # A standalone server otherwise still writes the shared OpenCode DB.
        # Advisory prompts and sessions belong only to this bounded request.
        env["OPENCODE_DB"] = os.path.join(folder, "opencode.db")
        # OpenCode resolves its workspace from PWD, not only getcwd(). Popen's
        # cwd does not rewrite inherited PWD; keep both tied to this directory.
        env["PWD"] = folder
        uses_context_file = provider_for(model) == "opencode"
        if uses_context_file:
            # OpenCode's free inference rejects an empty tool catalog. Its
            # documented read-only reviewer agent keeps a useful read tool
            # while denying edits, shell, network, and every external path.
            # Internal permission resources are workspace-relative in V2.
            path = Path(folder) / "context.txt"
            path.write_text(user, encoding="utf-8")
            path.chmod(0o600)
            cfg["agents"]["arui-advisor"]["permissions"].append(
                {"action": "read", "resource": "context.txt", "effect": "allow"})
            env["OPENCODE_CONFIG_CONTENT"] = json.dumps(cfg)
            user = ("Read context.txt with the read tool. It contains the complete research request. "
                    "Answer that request according to your system instructions. "
                    "Only context.txt is available; do not access other files or use other tools.")
        proc = subprocess.Popen(
            [binary, "run", "--standalone", "--model", model,
             "--agent", "arui-advisor", "--format", "json",
             "--title", "Autoresearcher advisor"],
            cwd=folder, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            stdout, _ = proc.communicate(user, timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.communicate()
            raise TimeoutError("OpenCode advisor timed out") from None
        if proc.returncode:
            # Prefer the structured provider error, retaining only its HTTP
            # status; never surface raw CLI stderr or nested request bodies.
            try:
                parse_events(stdout)
            except OpenCodeRequestError:
                raise
            except RuntimeError:
                pass
            raise RuntimeError(f"OpenCode advisor exited with status {proc.returncode}; check model access and CLI version")
        answer = parse_events(stdout)
        if uses_context_file and not _read_context_completed(stdout, folder):
            raise RuntimeError("OpenCode advisor did not read the supplied research context; no review was accepted")
        return answer
