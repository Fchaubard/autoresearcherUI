"""Unit tests for backend.app.auth — the passcode gate."""
from __future__ import annotations

import pytest


def _make_request(path="/api/runs", *, query="", cookies=None, headers=None):
    """Build a Starlette Request stub good enough for auth helpers."""
    from starlette.requests import Request
    qs = query.encode() if isinstance(query, str) else query
    raw_headers = []
    for k, v in (headers or {}).items():
        raw_headers.append((k.lower().encode(), v.encode()))
    if cookies:
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
        raw_headers.append((b"cookie", cookie_str.encode()))
    scope = {
        "type": "http", "path": path, "raw_path": path.encode(),
        "query_string": qs, "headers": raw_headers, "method": "GET",
        "scheme": "http", "server": ("test", 80), "client": ("t", 0),
        "root_path": "", "app": None,
    }
    return Request(scope)


def test_gate_off_when_no_passcode(arui_env):
    from backend.app import auth
    assert auth.is_enabled() is False
    assert auth._saved_passcode() == ""


def test_gate_on_when_passcode_set(arui_env, setting_setter):
    from backend.app import auth
    setting_setter("onboarding", {"passcode": "hunter2"})
    assert auth.is_enabled() is True
    assert auth._saved_passcode() == "hunter2"


def test_passcode_whitespace_stripped(arui_env, setting_setter):
    from backend.app import auth
    setting_setter("onboarding", {"passcode": "  swordfish  "})
    assert auth._saved_passcode() == "swordfish"


def test_is_public_prefixes(arui_env):
    from backend.app.auth import _is_public
    assert _is_public("/healthz")
    assert _is_public("/static/app.js")
    assert _is_public("/api/passcode/check")
    assert _is_public("/api/passcode/login")
    assert _is_public("/api/clientlog")
    assert _is_public("/api/onboarding")
    assert _is_public("/api/onboarding/defaults")
    assert _is_public("/p/abc123")
    assert _is_public("/api/paper/share/tok")
    # Bare index is public so the dashboard shell can boot.
    assert _is_public("/")
    assert _is_public("")
    # Anything else gated
    assert not _is_public("/api/runs")
    assert not _is_public("/api/settings")


def test_scope_and_sensitive_endpoints_are_gated(arui_env):
    """Operator decision: the onboarding FORM is the only public entry (a
    wifi-style 'claim the box fast' SOP). Once a passcode is set, EVERYTHING
    else requires it — INCLUDING the scoping section, the agent terminal, file
    + shell endpoints, and paper mode. This test pins that boundary."""
    from backend.app.auth import _is_public
    must_be_gated = (
        "/api/scope/status", "/api/scope/confirm", "/api/scope/chat",
        "/api/scope/skip", "/api/scope/start_preview",
        "/api/project", "/api/runs", "/api/settings",
        "/api/agent/raw", "/api/agent/keys", "/api/sessions/agent/attach",
        "/api/paper/runs/queue", "/api/research/conclude", "/api/reset",
    )
    for p in must_be_gated:
        assert not _is_public(p), p + " must require the passcode"
    # ...but the onboarding form + its defaults remain public (first-run SOP)
    assert _is_public("/api/onboarding")
    assert _is_public("/api/onboarding/defaults")


def test_extract_passcode_query_wins(arui_env):
    from backend.app.auth import _extract_passcode
    req = _make_request(query="p=fromquery",
                        cookies={"arui_pc": "fromcookie"},
                        headers={"authorization": "Bearer fromheader"})
    assert _extract_passcode(req) == "fromquery"


def test_extract_passcode_cookie(arui_env):
    from backend.app.auth import _extract_passcode
    req = _make_request(cookies={"arui_pc": "ck"})
    assert _extract_passcode(req) == "ck"


def test_extract_passcode_bearer(arui_env):
    from backend.app.auth import _extract_passcode
    req = _make_request(headers={"authorization": "Bearer secret"})
    assert _extract_passcode(req) == "secret"


def test_extract_passcode_x_header(arui_env):
    from backend.app.auth import _extract_passcode
    req = _make_request(headers={"x-arui-passcode": "xpass"})
    assert _extract_passcode(req) == "xpass"


def test_extract_passcode_none(arui_env):
    from backend.app.auth import _extract_passcode
    req = _make_request()
    assert _extract_passcode(req) == ""


def test_login_no_passcode_set(arui_env):
    from backend.app import auth
    req = _make_request()
    ok, msg = auth.login(req, "anything")
    assert ok is True
    assert "no passcode" in msg.lower() or "gate is off" in msg.lower()


def test_login_correct(arui_env, setting_setter):
    from backend.app import auth
    setting_setter("onboarding", {"passcode": "right"})
    req = _make_request()
    ok, msg = auth.login(req, "right")
    assert ok is True


def test_login_wrong(arui_env, setting_setter):
    from backend.app import auth
    setting_setter("onboarding", {"passcode": "right"})
    req = _make_request()
    ok, msg = auth.login(req, "wrong")
    assert ok is False
    assert "wrong" in msg.lower()


@pytest.mark.asyncio
async def test_middleware_off_no_passcode(arui_env):
    from backend.app.auth import passcode_middleware

    async def call_next(_req):
        return "OK"

    req = _make_request("/api/runs")
    out = await passcode_middleware(req, call_next)
    assert out == "OK"


@pytest.mark.asyncio
async def test_middleware_public_passes(arui_env, setting_setter):
    from backend.app.auth import passcode_middleware
    setting_setter("onboarding", {"passcode": "x"})

    async def call_next(_req):
        return "OK"

    out = await passcode_middleware(
        _make_request("/api/passcode/check"), call_next)
    assert out == "OK"


@pytest.mark.asyncio
async def test_middleware_api_401_when_missing(arui_env, setting_setter):
    from backend.app.auth import passcode_middleware
    setting_setter("onboarding", {"passcode": "secret"})

    async def call_next(_req):  # would be called only on success
        return "should-not-run"

    resp = await passcode_middleware(
        _make_request("/api/runs"), call_next)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_middleware_html_redirect_when_missing(arui_env, setting_setter):
    from backend.app.auth import passcode_middleware
    setting_setter("onboarding", {"passcode": "secret"})

    async def call_next(_req):
        return "ignored"

    resp = await passcode_middleware(
        _make_request("/some-html-page"), call_next)
    assert resp.status_code == 302


@pytest.mark.asyncio
async def test_middleware_query_passes(arui_env, setting_setter):
    from backend.app.auth import passcode_middleware
    from starlette.responses import JSONResponse
    setting_setter("onboarding", {"passcode": "secret"})

    async def call_next(_req):
        return JSONResponse({"ok": True})

    resp = await passcode_middleware(
        _make_request("/api/runs", query="p=secret"), call_next)
    assert resp.status_code == 200
    # cookie should be set since we came in via ?p=
    set_cookie = resp.headers.get("set-cookie") or ""
    assert "arui_pc=" in set_cookie


@pytest.mark.asyncio
async def test_middleware_cookie_passes(arui_env, setting_setter):
    from backend.app.auth import passcode_middleware
    from starlette.responses import JSONResponse
    setting_setter("onboarding", {"passcode": "secret"})

    async def call_next(_req):
        return JSONResponse({"ok": True})

    resp = await passcode_middleware(
        _make_request("/api/runs", cookies={"arui_pc": "secret"}),
        call_next)
    assert resp.status_code == 200
