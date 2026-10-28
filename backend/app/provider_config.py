"""Shared provider configuration for settings, workers and subprocesses."""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from urllib.parse import urlsplit

from .model_registry import MODELS, provider_for

ENV_FIELDS = {
    "claude_token": "ANTHROPIC_API_KEY",
    "openai_token": "OPENAI_API_KEY",
    "gemini_token": "GEMINI_API_KEY",
    "opencode_token": "OPENCODE_API_KEY",
    "flint_base_url": "ARUI_FLINT_BASE_URL",
    "flint_model": "ARUI_FLINT_MODEL",
    "flint_token": "ARUI_FLINT_API_KEY",
    "flint_context_tokens": "ARUI_FLINT_CONTEXT_TOKENS",
    "flint_max_output_tokens": "ARUI_FLINT_MAX_OUTPUT_TOKENS",
}
TOKEN_FIELDS = {"claude": "claude_token", "openai": "openai_token",
                "gemini": "gemini_token", "opencode": "opencode_token",
                "flint": "flint_token"}


def environment_from_config(cfg: dict) -> dict[str, str]:
    return {env: str(cfg[field]).strip() for field, env in ENV_FIELDS.items()
            if cfg.get(field) and str(cfg[field]).strip()}


def config_value(field: str, cfg: dict | None = None) -> str:
    # An explicit empty value removes a configured setting; omitted fields
    # inherit service environment values.
    if cfg is not None and field in cfg:
        return str(cfg[field] or "").strip()
    return os.environ.get(ENV_FIELDS[field], "").strip()


def opencode_binary() -> str:
    override = os.environ.get("ARUI_OPENCODE_BIN", "").strip()
    candidates = ([override] if override else []) + [
        shutil.which("opencode") or "", str(Path.home() / ".opencode/bin/opencode")]
    return next((p for p in candidates if p and Path(p).is_file()
                 and os.access(p, os.X_OK)), "")


def flint_config(cfg: dict | None = None) -> tuple[str, str, str]:
    url = config_value("flint_base_url", cfg).rstrip("/")
    parsed = urlsplit(url)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("Set Flint's server URL to an http(s) endpoint ending in /v1")
    if not parsed.path.rstrip("/").endswith("/v1"):
        url += "/v1"
    model = config_value("flint_model", cfg) or "flint"
    return url, model, config_value("flint_token", cfg)


def flint_limits(cfg: dict | None = None) -> tuple[int, int]:
    """Client budgets must fit the local server, not the model's theoretical max."""
    try:
        context = int(config_value("flint_context_tokens", cfg) or "16384")
        output = int(config_value("flint_max_output_tokens", cfg) or "8192")
    except ValueError:
        raise ValueError("Flint context and output token limits must be whole numbers") from None
    if context < 1024 or output < 1 or output >= context:
        raise ValueError("Flint needs context >= 1024 tokens and an output budget smaller than its context")
    return context, output


def requirement_error(model: str, cfg: dict | None = None, *, agent: bool = False) -> str:
    provider = provider_for(model)
    if not provider:
        return f"Unknown model provider for {model!r}"
    if provider in {"opencode", "flint"} and (provider == "opencode" or agent):
        if not opencode_binary():
            return "OpenCode 2 CLI is required; install it or set ARUI_OPENCODE_BIN"
    if provider == "flint":
        try:
            flint_config(cfg)
            flint_limits(cfg)
        except ValueError as exc:
            return str(exc)
        return ""
    if provider == "opencode" and any(m["id"] == model and m.get("free") for m in MODELS):
        return ""
    if not config_value(TOKEN_FIELDS[provider], cfg):
        return f"Configure the {provider} API key for {model}"
    return ""


def provider_ready(model: str, cfg: dict | None = None) -> bool:
    return not requirement_error(model, cfg)
