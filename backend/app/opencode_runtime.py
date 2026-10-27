"""OpenCode 2 adapters for free hosted models and local Flint coding.

Free Console chat models require the OpenCode client. Advisory calls use a
tool-disabled primary agent in a temporary directory, never a coding agent
in the live research tree. Standalone servers pick up current settings and
do not mutate or restart the user's shared OpenCode service.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile

from .model_registry import provider_for
from .provider_config import flint_config, opencode_binary


class OpenCodeRequestError(RuntimeError):
    """A provider failure with safe, actionable classification."""

    def __init__(self, status: int | None = None):
        self.status = status
        if status == 429:
            message = "OpenCode free usage limit reached or rate limited; retry when quota resets or select another provider"
        elif status in {401, 403}:
            message = "OpenCode authentication or model access was rejected; check the configured connection"
        elif status == 400:
            message = "OpenCode rejected the model request; the selected model may be temporarily unavailable"
        else:
            message = "OpenCode model request failed; check model access, quota and server availability"
        super().__init__(message)


def _config(model: str) -> dict:
    cfg = {"model": model}
    if provider_for(model) == "flint":
        url, served_model, token = flint_config()
        cfg["providers"] = {"flint": {
            "name": "Local Flint",
            "package": "@opencode/ai/providers/openai-compatible",
            "settings": {"baseURL": url, "apiKey": token or "unused"},
            "models": {"local": {"name": "Flint", "modelID": served_model,
                                   "capabilities": {"tools": True, "input": ["text"],
                                                    "output": ["text"],
                                                    "reasoning": {"supported": True}},
                                   "compatibility": {"reasoningField": "reasoning_content",
                                                     "maxTokensField": "max_tokens"},
                                   "limit": {"context": 262144, "output": 32768}}},
        }}
    return cfg


def agent_environment(model: str) -> dict[str, str]:
    """Environment-only config; no credentials are put in shell commands."""
    if provider_for(model) not in {"opencode", "flint"}:
        return {}
    cfg = _config(model)
    cfg["agents"] = {"arui-research": {
        "mode": "primary", "description": "Autoresearcher coding agent",
        "permissions": [{"action": "*", "resource": "*", "effect": "allow"}],
    }}
    return {"OPENCODE_CONFIG_CONTENT": json.dumps(cfg),
            "OPENCODE_CLI_CONFIG_CONTENT": json.dumps({"attention": {"notifications": False, "sound": False}})}


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
            raise OpenCodeRequestError(status if isinstance(status, int) else None)
        if event.get("type") == "text":
            part = event.get("part") or {}
            value = part.get("text") or event.get("text")
            if isinstance(value, str):
                texts.append(value)
    if not texts:
        raise RuntimeError("OpenCode returned no assistant text")
    return "\n".join(texts)


def complete(model: str, system: str, user: str, *, timeout: float = 180) -> str:
    binary = opencode_binary()
    if not binary:
        raise RuntimeError("OpenCode 2 CLI is required for free OpenCode models")
    cfg = _config(model)
    cfg["agents"] = {"arui-advisor": {
        "mode": "primary", "system": system,
        "permissions": [{"action": "*", "resource": "*", "effect": "deny"}],
    }}
    env = {**os.environ, "OPENCODE_CONFIG_CONTENT": json.dumps(cfg),
           "OPENCODE_DISABLE_PROJECT_CONFIG": "1"}
    # stdin avoids command-line size limits and keeps research context out
    # of the process list. OpenCode consumes piped stdin as the user message.
    with tempfile.TemporaryDirectory(prefix="arui-advisor-") as folder:
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
        return parse_events(stdout)
