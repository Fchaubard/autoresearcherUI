"""Regression tests for the public-release security hardening:

  • /api/onboarding cannot be used to overwrite/disable the passcode gate
    once a passcode is set (the auth-bypass blocker).
  • the file browser is confined to the allowlisted roots and refuses
    secret material (the arbitrary-read/write blocker).
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture
def client(arui_env, fake_subprocess):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.app.api import router
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as c:
        yield c


def _stored_passcode():
    from backend.app.db import SessionLocal
    from backend.app.models import Setting
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(Setting.key == "onboarding").first()
        return (row.value or {}).get("passcode") if row else None
    finally:
        db.close()


# ── onboarding auth-bypass ───────────────────────────────────────────────────
def test_onboarding_first_run_can_set_passcode(client):
    """No passcode yet → onboarding is open and may set one."""
    r = client.post("/api/onboarding", json={"purpose": "x", "passcode": "sekret"})
    assert r.status_code == 200
    assert _stored_passcode() == "sekret"


def test_onboarding_cannot_change_passcode_without_it(client, setting_setter):
    setting_setter("onboarding", {"purpose": "x", "passcode": "sekret"})
    # unauthenticated attempt to overwrite settings / reset the gate
    r = client.post("/api/onboarding",
                    json={"purpose": "pwned", "passcode": "attacker"})
    assert r.status_code == 401
    assert _stored_passcode() == "sekret"      # gate unchanged


def test_onboarding_cannot_clear_passcode_when_authed(client, setting_setter):
    setting_setter("onboarding", {"purpose": "x", "passcode": "sekret"})
    # authenticated, but payload omits passcode → must be preserved, not cleared
    r = client.post("/api/onboarding", json={"purpose": "y"},
                    headers={"X-Arui-Passcode": "sekret"})
    assert r.status_code == 200
    assert _stored_passcode() == "sekret"


def test_onboarding_authed_can_rotate_passcode(client, setting_setter):
    setting_setter("onboarding", {"purpose": "x", "passcode": "sekret"})
    r = client.post("/api/onboarding",
                    json={"purpose": "x", "passcode": "newpass"},
                    headers={"X-Arui-Passcode": "sekret"})
    assert r.status_code == 200
    assert _stored_passcode() == "newpass"


# ── file-browser confinement ─────────────────────────────────────────────────
def test_files_read_refuses_outside_allowed_roots(client):
    r = client.get("/api/files/read", params={"path": "/etc/passwd"})
    body = r.json()
    assert body["ok"] is False
    assert "outside" in body["error"]


def test_files_read_refuses_secret_components(client):
    from backend.app.config import DATA_DIR
    secret = os.path.join(str(DATA_DIR), "secrets", "keys.env")
    r = client.get("/api/files/read", params={"path": secret})
    body = r.json()
    assert body["ok"] is False
    assert "not accessible" in body["error"]


def test_files_read_allows_inside_data_dir(client):
    from backend.app.config import DATA_DIR
    fp = os.path.join(str(DATA_DIR), "hello.txt")
    with open(fp, "w") as f:
        f.write("hi there")
    r = client.get("/api/files/read", params={"path": fp})
    body = r.json()
    assert body["ok"] is True
    assert body["content"] == "hi there"
