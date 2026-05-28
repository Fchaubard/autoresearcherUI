"""Passcode gate for autoresearcherUI.

After the user completes onboarding and sets a ``passcode``, every
non-public route requires that passcode. The passcode can be supplied as:

  • ``?p=<passcode>`` query string (used on first visit so the user can
    paste the dashboard URL into a new device and log in by adding the
    suffix once)
  • ``Cookie: arui_pc=<passcode>`` (set by the login screen once entered)
  • ``Authorization: Bearer <passcode>`` (handy for curl + scripts)
  • ``X-Arui-Passcode: <passcode>`` (same)

Public routes (no passcode required):
  • ``/healthz``
  • ``/static/*``     — the dashboard JS/CSS/index.html
  • ``/api/passcode/login`` + ``/api/passcode/check`` — the gate itself
  • ``/api/onboarding`` — used BEFORE a passcode is set, on first run
  • ``/api/onboarding/defaults`` — same
  • ``/api/clientlog``— browser error reporting (used pre-login too)
  • ``/p/<token>``    — read-only paper share viewer
  • ``/api/paper/share/<token>`` and ``/api/paper/share/<token>/pdf``

If no passcode is set in Settings, the gate is OFF — everything passes
through. So fresh installs and pre-onboarding states behave exactly as
they did before. Only AFTER the user explicitly sets a passcode does the
gate start enforcing.
"""
from __future__ import annotations

from typing import Iterable

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse

from .db import SessionLocal
from .models import Setting


COOKIE_NAME = "arui_pc"

# Path prefixes that are ALWAYS public (no passcode required).
_PUBLIC_PREFIXES: tuple[str, ...] = (
    "/healthz",
    "/static/",
    "/favicon",
    "/api/passcode/",          # login / check
    "/api/clientlog",          # error reports
    "/api/onboarding",         # GET defaults + POST onboarding form
    "/p/",                     # paper share viewer (token-gated separately)
    "/api/paper/share/",       # paper share JSON + pdf
)


def _saved_passcode() -> str:
    """Read the passcode out of the onboarding Setting row; '' if unset."""
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(
            Setting.key == "onboarding").first()
        if not row or not isinstance(row.value, dict):
            return ""
        return str(row.value.get("passcode") or "").strip()
    finally:
        db.close()


def _is_public(path: str) -> bool:
    if path in ("", "/"):
        # The bare index renders the dashboard shell which then calls
        # /api/passcode/check — gating the JS would just blank-screen
        # the user. Let it through; the API calls behind it are gated.
        return True
    return any(path == p.rstrip("/") or path.startswith(p)
               for p in _PUBLIC_PREFIXES)


def _extract_passcode(request: Request) -> str:
    # query string wins so the user can paste a copy-paste login URL
    p = (request.query_params.get("p") or "").strip()
    if p:
        return p
    # cookie
    p = (request.cookies.get(COOKIE_NAME) or "").strip()
    if p:
        return p
    # Authorization: Bearer <code>
    auth = (request.headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    # X-Arui-Passcode
    p = (request.headers.get("x-arui-passcode") or "").strip()
    return p


async def passcode_middleware(request: Request, call_next):
    """Block any non-public request when a passcode is set and the request
    didn't supply the correct one.

    For JSON / API requests we return 401. For top-level navigations we
    redirect to ``/?p=`` so the user lands on the login screen with their
    intent preserved (we drop the URL into the redirect's referer).
    """
    saved = _saved_passcode()
    if not saved:
        # No passcode configured → gate is OFF. Original pre-passcode
        # behavior is preserved exactly.
        return await call_next(request)
    path = request.url.path
    if _is_public(path):
        return await call_next(request)
    supplied = _extract_passcode(request)
    if supplied and supplied == saved:
        response = await call_next(request)
        # If they came in via ?p= or header, set the cookie so subsequent
        # navigations don't need to repeat it.
        if (request.query_params.get("p")
                or request.headers.get("authorization")
                or request.headers.get("x-arui-passcode")):
            response.set_cookie(
                COOKIE_NAME, saved,
                max_age=60 * 60 * 24 * 30,   # 30 days
                httponly=True, samesite="lax", path="/")
        return response
    # Gate fails — distinguish API from HTML for a sensible response.
    if path.startswith("/api/"):
        return JSONResponse({"detail": "passcode required"},
                            status_code=401)
    # HTML navigation — bounce to root which will render the login screen
    return RedirectResponse(url="/", status_code=302)


def login(request: Request, supplied: str) -> tuple[bool, str]:
    """Return (ok, message). Used by /api/passcode/login."""
    saved = _saved_passcode()
    if not saved:
        return True, "no passcode set — gate is off"
    if (supplied or "").strip() == saved:
        return True, "ok"
    return False, "wrong passcode"


def is_enabled() -> bool:
    """True iff a passcode is configured."""
    return bool(_saved_passcode())
