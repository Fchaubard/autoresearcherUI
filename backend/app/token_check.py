"""Pre-flight token validation.

Every API token the user pastes into onboarding (Claude, OpenAI, Gemini,
Gmail-app-password, GitHub) is sent to its provider's cheapest auth-checking
endpoint here. If the token is rejected, the dashboard surfaces a clear
"the OpenAI token is invalid" message at onboarding time — *before* the
agent has launched and silently failed downstream.

All validators are best-effort, time-bounded, and run in parallel via a
``ThreadPoolExecutor``. Every validator returns the same shape::

    {"ok": bool, "detail": str, "latency_ms": int}

A missing / empty token returns ``ok=True`` with ``detail="not configured"``
— skipping validation for an optional unspecified field is the correct
behaviour, not an error.
"""
from __future__ import annotations

import concurrent.futures
import json
import smtplib
import socket
import ssl
import time
import urllib.error
import urllib.request


_TIMEOUT = 8.0          # seconds per provider — keep onboarding snappy


def _result(ok: bool, detail: str, t0: float) -> dict:
    return {"ok": ok, "detail": detail,
            "latency_ms": int((time.time() - t0) * 1000)}


def _skip() -> dict:
    return {"ok": True, "detail": "not configured", "latency_ms": 0,
            "skipped": True}


# ────────────────────────────── individual checks ─────────────────────────

def check_claude(token: str) -> dict:
    """Anthropic API. Cheapest auth probe: GET /v1/models."""
    if not (token or "").strip():
        return _skip()
    t0 = time.time()
    try:
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/models",
            headers={"x-api-key": token.strip(),
                     "anthropic-version": "2023-06-01"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            if r.status == 200:
                return _result(True, "valid", t0)
            return _result(False, f"HTTP {r.status}", t0)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="ignore")[:300]
        except Exception:
            pass
        if e.code == 401:
            return _result(False, "401 — token rejected (invalid or revoked)",
                           t0)
        if e.code == 403:
            return _result(False, "403 — token has no model access",
                           t0)
        if e.code == 429:
            return _result(False,
                           "429 — rate-limited (token works but throttled)",
                           t0)
        return _result(False, f"HTTP {e.code} {body[:120]}", t0)
    except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
        return _result(False, f"network error: {e}", t0)


def check_openai(token: str) -> dict:
    """OpenAI API. Cheapest probe: GET /v1/models."""
    if not (token or "").strip():
        return _skip()
    t0 = time.time()
    try:
        req = urllib.request.Request(
            "https://api.openai.com/v1/models",
            headers={"Authorization": f"Bearer {token.strip()}"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            if r.status == 200:
                return _result(True, "valid", t0)
            return _result(False, f"HTTP {r.status}", t0)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return _result(False, "401 — token rejected (invalid or revoked)",
                           t0)
        if e.code == 429:
            return _result(False,
                           "429 — rate-limited (token works but throttled)",
                           t0)
        if e.code == 403:
            return _result(False, "403 — token has no model access", t0)
        return _result(False, f"HTTP {e.code}", t0)
    except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
        return _result(False, f"network error: {e}", t0)


def check_gemini(token: str) -> dict:
    """Google Generative AI. Cheapest probe: GET /v1beta/models?key=…"""
    if not (token or "").strip():
        return _skip()
    t0 = time.time()
    try:
        url = ("https://generativelanguage.googleapis.com/v1beta/models"
               f"?key={token.strip()}")
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            if r.status == 200:
                return _result(True, "valid", t0)
            return _result(False, f"HTTP {r.status}", t0)
    except urllib.error.HTTPError as e:
        if e.code == 400:    # Gemini returns 400 for bad API keys
            body = ""
            try:
                body = e.read().decode("utf-8", errors="ignore")
            except Exception:
                pass
            if "API_KEY" in body or "API key not valid" in body:
                return _result(
                    False,
                    "400 — API key not valid (check it on aistudio.google.com)",
                    t0)
            return _result(False, f"400 — {body[:120]}", t0)
        if e.code == 403:
            return _result(False, "403 — API key denied / not enabled", t0)
        if e.code == 429:
            return _result(False, "429 — quota exceeded", t0)
        return _result(False, f"HTTP {e.code}", t0)
    except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
        return _result(False, f"network error: {e}", t0)


def check_github(token: str) -> dict:
    """GitHub PAT. Cheapest probe: GET /user — confirms the token is alive
    and tells us what username it belongs to."""
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
            if r.status == 200:
                try:
                    body = json.load(r)
                    login = body.get("login") or "?"
                    return _result(True, f"valid (logged in as {login})", t0)
                except Exception:
                    return _result(True, "valid", t0)
            return _result(False, f"HTTP {r.status}", t0)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return _result(False, "401 — token rejected (invalid or revoked)",
                           t0)
        if e.code == 403:
            # Could be SSO-required or scope-restricted
            return _result(False,
                           "403 — token valid but lacks scope or SSO not "
                           "authorised for this org", t0)
        return _result(False, f"HTTP {e.code}", t0)
    except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
        return _result(False, f"network error: {e}", t0)


def check_gmail(sender_email: str, app_pw: str) -> dict:
    """Gmail SMTP. EHLO + STARTTLS + LOGIN + QUIT. No mail is actually sent —
    we just confirm the app password authenticates."""
    if not (app_pw or "").strip() or not (sender_email or "").strip():
        return _skip()
    t0 = time.time()
    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=_TIMEOUT) as s:
            s.ehlo()
            s.starttls(context=ctx)
            s.ehlo()
            s.login(sender_email.strip(), app_pw.strip().replace(" ", ""))
        return _result(True, "valid (SMTP login OK)", t0)
    except smtplib.SMTPAuthenticationError as e:
        # Gmail returns "Username and Password not accepted" + a help URL
        return _result(False,
                       "535 — Gmail rejected the app password. Make sure "
                       "2-Step Verification is ON and you pasted the "
                       "16-char code without spaces.",
                       t0)
    except (smtplib.SMTPException, socket.timeout, TimeoutError, OSError) as e:
        return _result(False, f"SMTP error: {e}", t0)


# ─────────────────────────────── orchestrator ─────────────────────────────

def check_all(cfg: dict) -> dict:
    """Run every validator in parallel and return a dict keyed by token
    name. ``cfg`` is the onboarding config (same shape we save to the
    Setting row)."""
    jobs = {
        "claude":   (check_claude, (cfg.get("claude_token") or "",)),
        "openai":   (check_openai, (cfg.get("openai_token") or "",)),
        "gemini":   (check_gemini, (cfg.get("gemini_token") or "",)),
        "github":   (check_github, (cfg.get("github_token") or "",)),
        "gmail":    (check_gmail,
                     ((cfg.get("email") or ""),
                      cfg.get("gmail_app_pw") or "")),
    }
    out: dict[str, dict] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        futures = {ex.submit(fn, *args): name
                   for name, (fn, args) in jobs.items()}
        for fut in concurrent.futures.as_completed(futures, timeout=_TIMEOUT + 4):
            name = futures[fut]
            try:
                out[name] = fut.result()
            except Exception as e:                    # noqa: BLE001
                out[name] = {"ok": False,
                             "detail": f"validator crashed: {e}",
                             "latency_ms": 0}
    return out
