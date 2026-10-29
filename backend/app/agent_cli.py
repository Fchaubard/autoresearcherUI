"""Provider-aware command construction for autonomous coding agents."""
from __future__ import annotations

import shlex
import shutil
import os
import json
import hashlib
import re
import tempfile
import subprocess
import time
from pathlib import Path

from .model_registry import efforts_for, provider_for


class AgentCLIUnavailable(RuntimeError):
    pass


def model_selection_failed(screen: str) -> bool:
    """Never submit/confirm through a CLI's unavailable-model picker."""
    low = screen.lower()
    return any(text in low for text in (
        "model_not_found", "model not found", "unknown model", "invalid model",
        "model does not exist", "not a valid model", "not supported when using",
        "choose a different model", "select a different model", "switch to another model",
        "switch to a different model", "is restricted by your organization's settings",
        "request was flagged", "message was flagged")) or bool(re.search(
            r"(?:fall\s?back|fall back|switch) (?:model:\s*|to\s+)(?:gpt-|claude-|gemini-)", low))


def accept_codex_project_trust(session: str, timeout: float = 45) -> bool:
    """Accept only the selected project's first-run Codex trust screen.

    Codex's onboarding can precede command-line project trust overrides.
    The initial research prompt is already on its command line, so do not
    inject any additional prompt or key once it starts working.
    """
    deadline = time.monotonic() + timeout
    target = "=" + session + ":"
    while time.monotonic() < deadline:
        result = subprocess.run(["tmux", "capture-pane", "-t", target, "-p"],
                                capture_output=True, text=True, timeout=5)
        screen = result.stdout.lower()
        if result.returncode or model_selection_failed(screen):
            return False
        if "do you trust the contents of this directory" in screen and "yes, continue" in screen:
            time.sleep(0.4)
            sent = subprocess.run(["tmux", "send-keys", "-t", target, "Enter"],
                                  capture_output=True, timeout=5)
            return sent.returncode == 0
        if "esc to interrupt" in screen or "ask codex to do anything" in screen:
            return True
        time.sleep(0.5)
    return False


def prepare_environment(model: str, state_dir: Path | None = None) -> dict[str, str]:
    """Prepare provider-owned non-secret settings for unattended startup."""
    if provider_for(model) == "claude":
        return {"DISABLE_AUTOUPDATER": "1"}
    if provider_for(model) != "gemini":
        return {}
    if state_dir is None:
        from .config import DATA_DIR
        # Research and author may select different models concurrently.
        # Their availability policy files must never overwrite one another.
        state_dir = DATA_DIR / "cli" / "gemini" / hashlib.sha256(model.encode()).hexdigest()[:16]
    state_dir = Path(state_dir)
    settings = {"security": {"auth": {"selectedType": "gemini-api-key"}},
                "general": {"enableAutoUpdate": False, "enableAutoUpdateNotification": False,
                            "disableAutoUpdate": True, "disableUpdateNag": True}}
    # Gemini's stock utility/auto availability chains can silently route to
    # another model after quota/server errors. Give every chain only the
    # explicit selection; a failed model stays failed instead of substituting.
    # https://geminicli.com/docs/cli/model-routing/
    policy = {"model": model, "isLastResort": True, "maxAttempts": 2,
              "actions": {kind: "prompt" for kind in ("terminal", "transient", "not_found", "unknown")},
              "stateTransitions": {kind: "terminal" for kind in ("terminal", "transient", "not_found", "unknown")}}
    settings["experimental"] = {"dynamicModelConfiguration": True}
    settings["modelConfigs"] = {
        # Explicit IDs must not inherit the CLI's rollout promotions (for
        # example 3.1 Flash Lite -> 3.5 Flash Lite). Let the API validate the
        # exact ID instead of treating it as an auto/preview alias.
        "modelIdResolutions": {model: {"default": model, "contexts": []}},
        "modelDefinitions": {model: {"isPreview": False, "tier": "custom", "family": "arui-exact"}},
        "modelChains": {name: [policy] for name in
                        ("default", "auto-default", "preview", "auto-preview", "lite")}}
    # Current Gemini bootstrap and its core resolve GEMINI_CLI_HOME
    # differently; keep both settings locations consistent across versions.
    for path in (state_dir / "settings.json", state_dir / ".gemini" / "settings.json"):
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=".settings-", delete=False) as tmp:
            json.dump(settings, tmp)
            temporary = Path(tmp.name)
        try:
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    return {"GEMINI_CLI_HOME": str(state_dir), "GEMINI_CLI_TRUST_WORKSPACE": "true"}


def _binary(provider: str) -> str:
    if provider in {"opencode", "flint"}:
        from .provider_config import opencode_binary
        binary = opencode_binary()
        if not binary:
            raise AgentCLIUnavailable("OpenCode 2 CLI is required for OpenCode and local Flint agents")
        return shlex.quote(binary)
    name = {"claude": "claude", "openai": "codex", "gemini": "gemini"}[provider]
    override = os.environ.get(f"ARUI_{name.upper()}_BIN", "").strip()
    candidates = [Path(override)] if override else []
    # Prefer the command currently selected by PATH. Modern Codex upgrades
    # migrate from the npm/NVM layout to an immutable standalone release and
    # remove the old npm package. Choosing a merely-existing NVM shim first
    # can launch the old native process just as its sibling
    # `codex-code-mode-host` is removed, leaving a live but unusable REPL.
    # The standalone `current` symlink resolves into a versioned release, so
    # its helper binaries remain beside the running executable across updates.
    # Codex's standalone installer is deliberately independent of Node.  A
    # system service often has a smaller PATH than an interactive shell and
    # can otherwise find an old npm wrapper first.  That wrapper may be
    # syntactically incompatible with the service's Node and die before its
    # REPL appears.  Prefer the installer's stable `current` link and then its
    # newest immutable release.
    if name == "codex" and not override:
        root = Path.home() / ".codex" / "packages" / "standalone"
        candidates.append(root / "current" / "bin" / name)
        candidates += sorted(root.glob(f"releases/*/bin/{name}"), reverse=True)
    found = shutil.which(name)
    if found:
        candidates.append(Path(found))
    if name in {"codex", "gemini"}:
        # Backend services often start with a minimal PATH that omits nvm even
        # though the current coding CLIs require its modern Node runtime.
        candidates += sorted(Path.home().glob(f".nvm/versions/node/*/bin/{name}"),
                             reverse=True)
    binary = next((p for p in candidates if p.is_file()), None)
    if not binary:
        raise AgentCLIUnavailable(
            f"{name} CLI is required for this autonomous agent")
    # Preserve the selected Node installation for #!/usr/bin/env node.
    if name in {"codex", "gemini"} and binary.parent.name == "bin":
        return f"PATH={shlex.quote(str(binary.parent))}:$PATH {shlex.quote(str(binary))}"
    return shlex.quote(str(binary))


def command(model: str, effort: str = "", prompt: str = "", *, workspace: str | Path | None = None,
            resume_session_id: str | None = None) -> tuple[str, str]:
    """Return ``(provider, shell_command)`` for an interactive coding CLI.

    Claude Code is fed after boot because of its consent screens. Codex and
    Gemini CLI accept the first prompt on their command line and remain
    interactive afterwards.
    """
    provider = provider_for(model)
    if not provider:
        raise AgentCLIUnavailable(f"unknown model provider for {model!r}")
    if provider == "openai" and effort and effort not in efforts_for(model):
        raise AgentCLIUnavailable(f"Reasoning effort {effort!r} is not supported by the selected model {model!r}")
    if resume_session_id:
        import uuid
        if provider != "openai" or workspace is None:
            raise AgentCLIUnavailable("Native continuation requires a verified Codex workspace session")
        try:
            resume_session_id = str(uuid.UUID(resume_session_id))
        except (ValueError, TypeError, AttributeError):
            raise AgentCLIUnavailable("A valid explicit Codex session ID is required") from None
    binary = _binary(provider)
    qmodel = shlex.quote(model)
    if provider in {"opencode", "flint"}:
        # V2's full TUI no longer accepts --model; mini does and keeps
        # scrollback available to the dashboard's terminal and supervisor.
        cmd = f"{binary} mini --standalone --model {qmodel} --agent arui-research"
        if prompt:
            cmd += f" --prompt {shlex.quote(prompt)}"
        return provider, cmd
    if provider == "claude":
        strict_settings = json.dumps({"availableModels": [model],
                                      "switchModelsOnFlag": False,
                                      "fallbackModel": [model]}, separators=(",", ":"))
        cmd = (f"{binary} --dangerously-skip-permissions --model {qmodel} "
               f"--fallback-model {qmodel} --settings {shlex.quote(strict_settings)}")
        if effort in {"low", "medium", "high", "xhigh", "max"}:
            cmd += f" --effort {shlex.quote(effort)}"
        return provider, cmd
    if provider == "openai":
        # Codex reserves its built-in "openai" provider and its interactive
        # login otherwise depends on mutable global auth state. An app-owned
        # definition fixes the vendor/endpoint and reads only the supplied key.
        endpoint = ('{name="OpenAI",base_url="https://api.openai.com/v1",'
                    'env_key="OPENAI_API_KEY",wire_api="responses",requires_openai_auth=false}')
        launch = f"{binary} resume" if resume_session_id else binary
        cmd = (f"{launch} --dangerously-bypass-approvals-and-sandbox "
               f"--no-alt-screen -c check_for_update_on_startup=false --model {qmodel} "
               "-c 'model_provider=\"arui_openai\"' "
               f"-c {shlex.quote('model_providers.arui_openai=' + endpoint)}")
        if workspace is not None:
            selected = Path(workspace).resolve()
            # The operator has already authorized this project. Codex asks
            # separately about loading project config even with sandbox
            # bypass, so scope that trust explicitly to its enclosing repo.
            repo_root = next((p for p in (selected, *selected.parents)
                              if (p / ".git").exists()), selected)
            for path in {selected, repo_root}:
                trust = "projects." + json.dumps(str(path)) + '.trust_level="trusted"'
                cmd += f" -c {shlex.quote(trust)}"
        if effort in efforts_for(model):
            cmd += f" -c model_reasoning_effort={shlex.quote(effort)}"
        if resume_session_id:
            cmd += f" --cd {shlex.quote(str(Path(workspace).resolve()))} {shlex.quote(resume_session_id)}"
        if prompt:
            cmd += f" {shlex.quote(prompt)}"
        return provider, cmd
    cmd = (f"{binary} --approval-mode=yolo --screen-reader --model {qmodel}")
    if prompt:
        cmd += f" --prompt-interactive {shlex.quote(prompt)}"
    return provider, cmd
