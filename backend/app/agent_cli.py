"""Provider-aware command construction for autonomous coding agents."""
from __future__ import annotations

import shlex
import shutil
import os
from pathlib import Path

from .model_registry import efforts_for, provider_for


class AgentCLIUnavailable(RuntimeError):
    pass


def _binary(provider: str) -> str:
    name = {"claude": "claude", "openai": "codex", "gemini": "gemini"}[provider]
    override = os.environ.get(f"ARUI_{name.upper()}_BIN", "").strip()
    candidates = [Path(override)] if override else []
    if name in {"codex", "gemini"}:
        # Backend services often start with a minimal PATH that omits nvm even
        # though the current coding CLIs require its modern Node runtime.
        candidates += sorted(Path.home().glob(f".nvm/versions/node/*/bin/{name}"),
                             reverse=True)
    binary = next((p for p in candidates if p.is_file()), None)
    found = shutil.which(name)
    if binary is None and found:
        # shutil.which already guarantees an executable path. Keeping this
        # branch separate also makes command resolution straightforward to
        # isolate in tests.
        binary = Path(found)
    if not binary:
        raise AgentCLIUnavailable(
            f"{name} CLI is required for this autonomous agent")
    # Preserve the selected Node installation for #!/usr/bin/env node.
    if name in {"codex", "gemini"} and binary.parent.name == "bin":
        return f"PATH={shlex.quote(str(binary.parent))}:$PATH {shlex.quote(str(binary))}"
    return shlex.quote(str(binary))


def command(model: str, effort: str = "", prompt: str = "") -> tuple[str, str]:
    """Return ``(provider, shell_command)`` for an interactive coding CLI.

    Claude Code is fed after boot because of its consent screens. Codex and
    Gemini CLI accept the first prompt on their command line and remain
    interactive afterwards.
    """
    provider = provider_for(model)
    if not provider:
        raise AgentCLIUnavailable(f"unknown model provider for {model!r}")
    binary = _binary(provider)
    qmodel = shlex.quote(model)
    if provider == "claude":
        cmd = f"{binary} --dangerously-skip-permissions --model {qmodel}"
        if effort in {"low", "medium", "high", "xhigh", "max"}:
            cmd += f" --effort {shlex.quote(effort)}"
        return provider, cmd
    if provider == "openai":
        cmd = (f"{binary} --dangerously-bypass-approvals-and-sandbox "
               f"--no-alt-screen --model {qmodel}")
        if effort in efforts_for(model):
            cmd += f" -c model_reasoning_effort={shlex.quote(effort)}"
        if prompt:
            cmd += f" {shlex.quote(prompt)}"
        return provider, cmd
    cmd = (f"{binary} --approval-mode=yolo --screen-reader --model {qmodel}")
    if prompt:
        cmd += f" --prompt-interactive {shlex.quote(prompt)}"
    return provider, cmd
