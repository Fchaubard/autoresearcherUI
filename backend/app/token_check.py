"""Pre-flight token validation.

Every API token the user pastes into onboarding (Claude, OpenAI, Gemini,
Gmail-app-password, GitHub) is checked here for TWO things:

  1. the key authenticates, AND
  2. the configured/default model for that provider is actually visible to the
     key (a key that authenticates but can't see the selected model would fail
     silently downstream).

Results surface at onboarding time - before the agent launches. All validators
are best-effort, time-bounded, and run in parallel; every validator returns the
same shape::

    {"ok": bool, "detail": str, "latency_ms": int, "model_ok": bool|None}

A missing / empty token returns ``ok=True`` with ``skipped=True``. Crashes and
the global timeout are turned into STRUCTURED failures, never a hang or a raise.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import subprocess
import smtplib
import socket
import ssl
import time
import urllib.error
import urllib.request

from .model_registry import provider_for


_TIMEOUT = 8.0          # seconds per provider — keep onboarding snappy

DEFAULT_MODELS = {
    "claude": "claude-opus-5",
    "openai": "gpt-6-astra",
    "gemini": "gemini-3.8-flash",
    "opencode": "opencode/nemotron-3-ultra-free",
    "flint": "flint/local",
}


def _model_for_provider(cfg: dict, provider: str) -> str:
    """Pick a selected model that actually belongs to ``provider``.

    Every role may now use every provider, so field names such as
    ``council_gemini_model`` describe a logical slot, not the model provider.
    Never pass a slot's Claude model to the Gemini credential validator.
    """
    for field in ("research_agent_model", "author_agent_model",
                  "scoping_model", "council_gemini_model",
                  "council_openai_model", "council_claude_model",
                  "pi_agent_model"):
        model = str(cfg.get(field) or "").strip()
        if provider_for(model) == provider:
            return model
    return DEFAULT_MODELS[provider]

# Anthropic short aliases / Cowork model names that are not always in
# GET /v1/models. Treat as always-visible so selecting them never false-fails.
_CLAUDE_ALIAS_OK = {"opus", "sonnet", "haiku", "fable"}


def _result(ok: bool, detail: str, t0: float, **extra) -> dict:
    r = {"ok": ok, "detail": detail,
         "latency_ms": int((time.time() - t0) * 1000)}
    r.update(extra)
    return r


def _skip() -> dict:
    return {"ok": True, "detail": "not configured", "latency_ms": 0,
            "skipped": True, "model_ok": None}


def _model_visible(model: str, available: list[str]) -> bool:
    """Match the selected ID, allowing Gemini's resource-name prefix only.

    A sibling such as gpt-5-mini must never certify access to gpt-5.
    Provider-managed aliases are handled explicitly by their own validator.
    """
    m = (model or "").strip().lower()
    if not m:
        return True
    for a in available:
        a = (a or "").strip().lower()
        if not a:
            continue
        if m.removeprefix("models/") == a.removeprefix("models/"):
            return True
    return False


# ────────────────────────────── individual checks ─────────────────────────

def check_claude(token: str, model: str | None = None) -> dict:
    """Anthropic API: GET /v1/models (auth) + verify the model is visible."""
    if not (token or "").strip():
        return _skip()
    t0 = time.time()
    try:
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/models",
            headers={"x-api-key": token.strip(),
                     "anthropic-version": "2023-06-01"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            if r.status != 200:
                return _result(False, f"HTTP {r.status}", t0, model_ok=None)
            ids = _ids([d.get("id") for d in
                        (json.load(r).get("data") or [])])
            return _claude_model_result(model, ids, t0)
    except urllib.error.HTTPError as e:
        return _http_err("claude", e, t0)
    except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
        return _result(False, f"network error: {e}", t0, model_ok=None)


def _claude_model_result(model, ids, t0):
    if model:
        base = model.strip().lower()
        alias = any(base == a or base.startswith(a) for a in _CLAUDE_ALIAS_OK)
        if alias or _model_visible(model, ids):
            return _result(True, f"valid (model '{model}' visible)", t0,
                           model_ok=True)
        return _result(False, f"authenticated, but model '{model}' is not "
                              "visible to this key", t0, model_ok=False)
    return _result(True, "valid", t0, model_ok=None)


def check_openai(token: str, model: str | None = None) -> dict:
    if not (token or "").strip():
        return _skip()
    t0 = time.time()
    try:
        req = urllib.request.Request(
            "https://api.openai.com/v1/models",
            headers={"Authorization": f"Bearer {token.strip()}"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            if r.status != 200:
                return _result(False, f"HTTP {r.status}", t0, model_ok=None)
            ids = _ids([d.get("id") for d in
                        (json.load(r).get("data") or [])])
            return _generic_model_result(model, ids, t0)
    except urllib.error.HTTPError as e:
        return _http_err("openai", e, t0)
    except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
        return _result(False, f"network error: {e}", t0, model_ok=None)


def check_gemini(token: str, model: str | None = None) -> dict:
    if not (token or "").strip():
        return _skip()
    t0 = time.time()
    try:
        url = "https://generativelanguage.googleapis.com/v1beta/models"
        with urllib.request.urlopen(urllib.request.Request(url, headers={"x-goog-api-key": token.strip()}),
                                    timeout=_TIMEOUT) as r:
            if r.status != 200:
                return _result(False, f"HTTP {r.status}", t0, model_ok=None)
            ids = _ids([m.get("name") for m in
                        (json.load(r).get("models") or [])])
            return _generic_model_result(model, ids, t0)
    except urllib.error.HTTPError as e:
        if e.code == 400:
            body = _body(e)
            if "API_KEY" in body or "API key not valid" in body:
                return _result(False, "400 — API key not valid "
                               "(check it on aistudio.google.com)", t0,
                               model_ok=None)
            return _result(False, f"400 — {body[:120]}", t0, model_ok=None)
        return _http_err("gemini", e, t0)
    except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
        return _result(False, f"network error: {e}", t0, model_ok=None)


def check_opencode(token: str, model: str | None = None) -> dict:
    """Check the installed OpenCode catalog, without claiming quota works."""
    from .provider_config import opencode_binary
    t0 = time.time()
    binary = opencode_binary()
    if not binary:
        return _result(False, "Install OpenCode 2 CLI or set ARUI_OPENCODE_BIN", t0, model_ok=False)
    try:
        env = dict(os.environ)
        if token:
            env["OPENCODE_API_KEY"] = token.strip()
        result = subprocess.run([binary, "models", "--standalone"],
                                capture_output=True, text=True, timeout=_TIMEOUT, env=env)
        if result.returncode:
            return _result(False, "OpenCode model listing failed; OpenCode 2 is required", t0, model_ok=False)
        ids = result.stdout.splitlines()
        if model and model not in ids:
            return _result(False, f"OpenCode does not list '{model}' for this connection", t0, model_ok=False)
        return _result(True, "OpenCode model listed; inference and free quota are checked when used", t0, model_ok=True)
    except (OSError, subprocess.TimeoutExpired):
        return _result(False, "OpenCode model listing failed or timed out", t0, model_ok=None)


def check_flint(cfg: dict) -> dict:
    """Check the served model and tool-call protocol needed by coding roles."""
    from .provider_config import flint_config
    t0 = time.time()
    try:
        url, model, token = flint_config(cfg)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        with urllib.request.urlopen(urllib.request.Request(url + "/models", headers=headers),
                                    timeout=_TIMEOUT) as resp:
            ids = [item.get("id") for item in json.load(resp).get("data", [])]
        if model not in ids:
            return _result(False, f"Flint server does not list served model '{model}'", t0, model_ok=False)
        # A listed model can still be served without the tool parser. Verify
        # the protocol; this sends only a synthetic ping, no research data.
        body = {"model": model, "max_tokens": 1024,
                "messages": [{"role": "user", "content": "Call arui_ping with ok=true."}],
                "tools": [{"type": "function", "function": {"name": "arui_ping",
                           "description": "Connectivity check", "parameters": {
                               "type": "object", "properties": {"ok": {"type": "boolean"}},
                               "required": ["ok"]}}}],
                "tool_choice": {"type": "function", "function": {"name": "arui_ping"}}}
        remaining = max(0.2, _TIMEOUT - (time.time() - t0))
        req = urllib.request.Request(url + "/chat/completions", data=json.dumps(body).encode(),
                                     headers={**headers, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=remaining) as resp:
            data = json.load(resp)
        calls = data["choices"][0]["message"].get("tool_calls") or []
        if not any(c.get("function", {}).get("name") == "arui_ping" for c in calls):
            return _result(False, "Flint model is listed but tool calls failed; enable its tool parser", t0, model_ok=False)
        return _result(True, "Flint model and tool-call protocol verified", t0, model_ok=True)
    except ValueError:
        return _result(False, "Set a valid Flint server URL and served model", t0, model_ok=False)
    except urllib.error.HTTPError as exc:
        return _result(False, f"Flint server returned HTTP {exc.code}; check model, token and tool parser", t0, model_ok=False)
    except (OSError, KeyError, IndexError, TypeError):
        return _result(False, "Flint server unavailable or response incompatible", t0, model_ok=False)


def _generic_model_result(model, ids, t0):
    if model:
        if _model_visible(model, ids):
            return _result(True, f"valid (model '{model}' visible)", t0,
                           model_ok=True)
        return _result(False, f"authenticated, but model '{model}' is not "
                              "visible to this key", t0, model_ok=False)
    return _result(True, "valid", t0, model_ok=None)


def check_github(token: str, model: str | None = None) -> dict:
    """GitHub PAT: GET /user (auth + who). No models - model_ok is N/A."""
    if not (token or "").strip():
        return _skip()
    t0 = time.time()
    try:
        req = urllib.request.Request(
            "https://api.github.com/user",
            headers={"Authorization": f"token {token.strip()}",
                     "Accept": "application/vnd.github+json",
                     "User-Agent": "autoresearcherUI"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            if r.status != 200:
                return _result(False, f"HTTP {r.status}", t0, model_ok=None)
            try:
                login = json.load(r).get("login") or "?"
                return _result(True, f"valid (logged in as {login})", t0,
                               model_ok=None)
            except Exception:                              # noqa: BLE001
                return _result(True, "valid", t0, model_ok=None)
    except urllib.error.HTTPError as e:
        return _http_err("github", e, t0)
    except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
        return _result(False, f"network error: {e}", t0, model_ok=None)


def check_gmail(sender_email: str, app_pw: str) -> dict:
    """Gmail SMTP: EHLO + STARTTLS + LOGIN + QUIT. No mail is sent."""
    if not (app_pw or "").strip() or not (sender_email or "").strip():
        return _skip()
    t0 = time.time()
    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=_TIMEOUT) as s:
            s.ehlo(); s.starttls(context=ctx); s.ehlo()
            s.login(sender_email.strip(), app_pw.strip().replace(" ", ""))
        return _result(True, "valid (SMTP login OK)", t0, model_ok=None)
    except smtplib.SMTPAuthenticationError:
        return _result(False, "535 — Gmail rejected the app password. Make sure "
                              "2-Step Verification is ON and you pasted the "
                              "16-char code without spaces.", t0, model_ok=None)
    except (smtplib.SMTPException, socket.timeout, TimeoutError, OSError) as e:
        return _result(False, f"SMTP error: {e}", t0, model_ok=None)


# ─────────────────────────────── helpers ──────────────────────────────────

def _ids(raw) -> list[str]:
    return [str(x) for x in (raw or []) if x]


def _body(e) -> str:
    try:
        return e.read().decode("utf-8", errors="ignore")[:300]
    except Exception:                                      # noqa: BLE001
        return ""


def _http_err(provider: str, e, t0) -> dict:
    if e.code == 401:
        return _result(False, "401 — token rejected (invalid or revoked)", t0,
                       model_ok=None)
    if e.code == 403:
        return _result(False, "403 — token has no model access / scope", t0,
                       model_ok=None)
    if e.code == 429:
        return _result(False, "429 — rate-limited (token works but throttled)",
                       t0, model_ok=None)
    return _result(False, f"HTTP {e.code} {_body(e)[:120]}", t0, model_ok=None)


# ─────────────────────────── advisor resolution ───────────────────────────

def resolve_advisor(cfg: dict) -> dict:
    """Describe the selected advisor; missing access never changes its model."""
    from .provider_config import requirement_error

    selected = str(cfg.get("scoping_model") or DEFAULT_MODELS["gemini"]).strip()
    # Historical configurations stored a provider name instead of a model.
    # Resolving that explicit alias is independent of available credentials.
    selected = DEFAULT_MODELS.get(selected, selected)
    return {"provider": provider_for(selected), "model": selected,
            "warning": requirement_error(selected, cfg)}


# ─────────────────────────────── orchestrator ─────────────────────────────

def check_all(cfg: dict) -> dict:
    """Run every validator in parallel and return a dict keyed by token name.

    Verifies the configured/default model per provider. The global timeout and
    any validator crash are turned into STRUCTURED failures so this never hangs
    or raises. Also returns an ``advisor`` entry with the effective scoping
    provider chosen from the keys present."""
    cfg = cfg or {}
    claude_model = _model_for_provider(cfg, "claude")
    openai_model = _model_for_provider(cfg, "openai")
    gemini_model = _model_for_provider(cfg, "gemini")
    jobs = {
        "claude": (check_claude, (cfg.get("claude_token") or "", claude_model)),
        "openai": (check_openai, (cfg.get("openai_token") or "", openai_model)),
        "gemini": (check_gemini, (cfg.get("gemini_token") or "", gemini_model)),
        "github": (check_github, (cfg.get("github_token") or "", None)),
        "gmail":  (check_gmail,
                   ((cfg.get("email") or ""), cfg.get("gmail_app_pw") or "")),
    }
    selected_providers = {provider_for(str(cfg.get(field) or "")) for field in (
        "research_agent_model", "author_agent_model", "scoping_model",
        "council_gemini_model", "council_openai_model", "council_claude_model",
        "pi_agent_model")}
    if "opencode" in selected_providers or cfg.get("opencode_token"):
        jobs["opencode"] = (check_opencode, (cfg.get("opencode_token") or "",
                                               _model_for_provider(cfg, "opencode")))
    if "flint" in selected_providers or cfg.get("flint_base_url"):
        jobs["flint"] = (check_flint, (cfg,))
    out: dict[str, dict] = {}
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
            futures = {ex.submit(fn, *args): name
                       for name, (fn, args) in jobs.items()}
            try:
                for fut in concurrent.futures.as_completed(
                        futures, timeout=_TIMEOUT + 4):
                    name = futures[fut]
                    try:
                        out[name] = fut.result()
                    except Exception as e:                 # noqa: BLE001
                        out[name] = {"ok": False, "model_ok": None,
                                     "detail": f"validator crashed: {e}",
                                     "latency_ms": 0}
            except concurrent.futures.TimeoutError:
                # global budget blown - fill any provider that didn't answer
                for fut, name in futures.items():
                    if name not in out:
                        out[name] = {"ok": False, "model_ok": None,
                                     "detail": "timed out — no response in "
                                               f"{int(_TIMEOUT + 4)}s",
                                     "latency_ms": int((_TIMEOUT + 4) * 1000)}
    except Exception as e:                                 # noqa: BLE001
        for name in jobs:
            out.setdefault(name, {"ok": False, "model_ok": None,
                                  "detail": f"validator error: {e}",
                                  "latency_ms": 0})
    out["advisor"] = resolve_advisor(cfg)
    return out


def blocking_failures(results: dict) -> list[str]:
    """Names of CONFIGURED providers whose validation failed (auth or model).
    Skipped/empty tokens and the ``advisor`` meta-entry are ignored."""
    bad = []
    for name, r in (results or {}).items():
        if name == "advisor" or not isinstance(r, dict):
            continue
        if r.get("skipped"):
            continue
        if r.get("ok") is False:
            bad.append(name)
    return bad
