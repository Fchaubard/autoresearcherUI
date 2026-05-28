"""Unit tests for backend.app.api — exercise interesting routes via TestClient.

We mount only the API router (no static files, no startup events) so the
tests stay fast and focused.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def client(arui_env, fake_subprocess):
    """A FastAPI TestClient bound to ONLY the api router."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.app.api import router
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as c:
        yield c


def test_settings_get_returns_masked_secrets(client, setting_setter):
    setting_setter("onboarding", {
        "claude_token": "secret-claude",
        "openai_token": "secret-openai",
        "passcode": "p4ss",
        "email": "me@x.com",
    })
    r = client.get("/api/settings")
    assert r.status_code == 200
    body = r.json()
    assert body["claude_token"] == "••••••••"
    assert body["openai_token"] == "••••••••"
    assert body["passcode"] == "••••••••"
    # non-secret survives unchanged
    assert body["email"] == "me@x.com"


def test_settings_put_does_not_clobber_blank_secrets(client, setting_setter):
    setting_setter("onboarding", {"claude_token": "keepme",
                                    "email": "old@x.com"})
    r = client.put("/api/settings",
                    json={"claude_token": "", "email": "new@x.com"})
    assert r.status_code == 200
    g = client.get("/api/settings").json()
    assert g["email"] == "new@x.com"
    # claude_token preserved
    assert g["claude_token"] == "••••••••"


def test_settings_put_ignores_mask_value(client, setting_setter):
    setting_setter("onboarding", {"openai_token": "real-tok"})
    r = client.put("/api/settings", json={"openai_token": "••••••••"})
    assert r.status_code == 200
    # Original token preserved (still masked when reading back)
    g = client.get("/api/settings").json()
    assert g["openai_token"] == "••••••••"
    # ensure underlying value still real
    from backend.app.db import SessionLocal
    from backend.app.models import Setting
    s = SessionLocal()
    try:
        cur = s.query(Setting).filter(Setting.key == "onboarding").first()
        assert cur.value["openai_token"] == "real-tok"
    finally:
        s.close()


def test_settings_put_updates_real_secret(client, setting_setter):
    setting_setter("onboarding", {"claude_token": "old"})
    r = client.put("/api/settings", json={"claude_token": "newval"})
    assert r.status_code == 200
    from backend.app.db import SessionLocal
    from backend.app.models import Setting
    s = SessionLocal()
    try:
        cur = s.query(Setting).filter(Setting.key == "onboarding").first()
        assert cur.value["claude_token"] == "newval"
    finally:
        s.close()


def test_onboarding_post_registers_project(client):
    r = client.post("/api/onboarding", json={
        "project_name": "My Project",
        "repo_name": "myrepo",
        "validation_metric": "val_loss",
        "metric_direction": "minimize",
    })
    assert r.status_code == 200
    # Project row created
    from backend.app.db import SessionLocal
    from backend.app.models import Project, Setting
    s = SessionLocal()
    try:
        # settings persisted
        st = s.query(Setting).filter(Setting.key == "onboarding").first()
        assert st is not None
        assert st.value["repo_name"] == "myrepo"
    finally:
        s.close()


def test_passcode_check_off(client):
    r = client.get("/api/passcode/check")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False
    assert body["authed"] is True


def test_passcode_check_on_not_authed(client, setting_setter):
    setting_setter("onboarding", {"passcode": "secret"})
    r = client.get("/api/passcode/check")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    assert body["authed"] is False


def test_passcode_login_success(client, setting_setter):
    setting_setter("onboarding", {"passcode": "secret"})
    r = client.post("/api/passcode/login", json={"passcode": "secret"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    # cookie set
    assert any("arui_pc" in (h.lower())
                for h in r.headers.get("set-cookie", "").split(","))


def test_passcode_login_wrong(client, setting_setter):
    setting_setter("onboarding", {"passcode": "secret"})
    r = client.post("/api/passcode/login", json={"passcode": "nope"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False


def test_passcode_logout_clears_cookie(client):
    r = client.post("/api/passcode/logout")
    assert r.status_code == 200
    sc = r.headers.get("set-cookie") or ""
    assert "arui_pc" in sc.lower()


def test_paper_enter_flips_mode_and_cadence(client, make_project,
                                                 setting_setter):
    make_project()
    setting_setter("onboarding", {"cadence": "1h"})
    r = client.post("/api/paper/enter", json={
        "meta": {"venue": "ICLR 2027", "deadline_iso": ""},
        "proposal_id": "",
    })
    assert r.status_code == 200
    from backend.app import paper
    assert paper.project_mode() == "paper"
    # Cadence auto-switched to 24h since old cadence was '1h'.
    from backend.app.db import SessionLocal
    from backend.app.models import Setting
    s = SessionLocal()
    try:
        cfg = s.query(Setting).filter(Setting.key == "onboarding").first()
        assert cfg.value["cadence"] == "24h"
    finally:
        s.close()


def test_paper_enter_already_in_paper_mode(client, make_project):
    make_project()
    from backend.app import paper
    paper.set_project_mode("paper")
    r = client.post("/api/paper/enter", json={"meta": {}})
    assert r.status_code == 200
    assert "already" in r.json().get("status", "").lower()


def test_paper_decisions_resolve_invalid(client):
    from backend.app import paper
    # Build a real decision and resolve it via paper.resolve_decision
    did = paper.file_decision(source="agent", kind="cite_paper",
                                title="x", linked_citation_key="k")
    assert paper.resolve_decision(did, "approve") is True
    assert paper.resolve_decision(did, "approve") is True  # idempotent ish
    assert paper.resolve_decision("missing", "approve") is False


def test_paper_runs_queue_creates_run(client, make_project, setting_setter):
    make_project(name="myproj")
    setting_setter("onboarding", {"repo_name": "myproj"})
    # In paper mode so paper_folder resolves cmd defaults
    from backend.app import paper
    paper.set_project_mode("paper")
    r = client.post("/api/paper/runs/queue", json={
        "name": "h1", "claim_id": "c1", "role": "headline",
        "cmd": "echo hi",
    })
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True
    assert j["name"] == "h1"


def test_paper_runs_queue_requires_cmd(client):
    r = client.post("/api/paper/runs/queue", json={"name": "x"})
    j = r.json()
    assert j["ok"] is False


def test_paper_runs_queue_batch(client):
    r = client.post("/api/paper/runs/queue_batch", json={
        "runs": [{"cmd": "a"}, {"cmd": "b"}, {"cmd": "c"}],
    })
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True
    assert j["n"] == 3


def test_paper_runs_results_listing(client, make_project, make_run):
    make_project()
    make_run(id="pr1", context="paper", run_name="pr1",
             status="kept", headline_metric=0.5)
    r = client.get("/api/paper/runs/results")
    assert r.status_code == 200
    j = r.json()
    assert any(x["id"] == "pr1" for x in j["runs"])


def test_paper_runs_results_status_filter(client, make_project, make_run):
    make_project()
    make_run(id="prk", context="paper", status="kept")
    make_run(id="prc", context="paper", status="crashed")
    r = client.get("/api/paper/runs/results", params={"status": "kept"})
    ids = [x["id"] for x in r.json()["runs"]]
    assert "prk" in ids
    assert "prc" not in ids


def test_paper_run_kill_marks_crashed(client, make_project, make_run):
    make_project()
    make_run(id="pr1", context="paper", status="running",
             tmux_session="pr1")
    r = client.post("/api/paper/runs/pr1/kill")
    assert r.json()["ok"] is True
    from backend.app.db import SessionLocal
    from backend.app.models import Run
    s = SessionLocal()
    try:
        rr = s.query(Run).filter(Run.id == "pr1").first()
        assert rr.status == "crashed"
        assert rr.config.get("killed_by") == "author_agent"
    finally:
        s.close()


def test_paper_run_kill_rejects_non_paper(client, make_project, make_run):
    make_project()
    make_run(id="r1", context="research", status="running")
    r = client.post("/api/paper/runs/r1/kill")
    assert r.json()["ok"] is False


def test_runs_cleanup_preview_endpoint(client, make_project, make_run):
    """/api/runs/cleanup/preview returns a {eligible, bytes_freeable, runs} dict."""
    make_project()
    r = client.get("/api/runs/cleanup/preview")
    assert r.status_code == 200
    j = r.json()
    assert "eligible" in j
    assert "bytes_freeable" in j
    assert "runs" in j


def test_runs_cleanup_post(client, make_project):
    make_project()
    r = client.post("/api/runs/cleanup", json={"min_age_days": 2.0,
                                                  "bottom_pct": 0.5})
    assert r.status_code == 200
    j = r.json()
    assert "deleted" in j
    assert "bytes_freed" in j


def test_runs_cleanup_sota_preview(client, make_project):
    make_project()
    r = client.get("/api/runs/cleanup/preview_sota")
    assert r.status_code == 200
    assert "eligible" in r.json()


def test_runs_cleanup_sota_post(client, make_project):
    make_project()
    r = client.post("/api/runs/cleanup_sota")
    assert r.status_code == 200
    assert "deleted" in r.json()


def test_system_returns_warnings_array(client):
    r = client.get("/api/system")
    assert r.status_code == 200
    body = r.json()
    assert "warnings" in body
    assert isinstance(body["warnings"], list)
    assert "gpus" in body


def test_list_runs_empty(client):
    r = client.get("/api/runs")
    assert r.status_code == 200
    assert r.json() == []


def test_list_runs_returns_rows(client, make_project, make_run):
    make_project()
    make_run(id="r1", run_name="r1", status="kept", headline_metric=0.1)
    r = client.get("/api/runs")
    assert r.status_code == 200
    rows = r.json()
    assert any(row["id"] == "r1" for row in rows)


def test_get_project_empty(client):
    r = client.get("/api/project")
    assert r.status_code == 200
    assert r.json() == {}


def test_get_project_returns_aggregates(client, make_project, make_run):
    make_project(name="X", metric_direction="minimize")
    make_run(id="r1", status="kept", headline_metric=0.5)
    make_run(id="r2", status="discarded", headline_metric=0.8)
    r = client.get("/api/project")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "X"
    assert body["experiments_done"] >= 2


def test_paper_decision_create_requires_kind(client):
    r = client.post("/api/paper/decisions", json={"title": "x"})
    assert r.json()["ok"] is False


def test_paper_decision_create_files_decision(client):
    r = client.post("/api/paper/decisions",
                     json={"kind": "cite_paper", "title": "cite X",
                            "body_md": "why", "priority": 5,
                            "linked_citation_key": "abc"})
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True
    assert j["id"]


def test_paper_claim_update_unknown(client):
    r = client.put("/api/paper/claims/nope/update",
                    json={"ready": True})
    assert r.json()["ok"] is False


def test_paper_claim_update_ok(client, db_session):
    from backend.app.models import PaperClaim
    db_session.add(PaperClaim(id="c1", title="x", status="active"))
    db_session.commit()
    r = client.put("/api/paper/claims/c1/update",
                    json={"ready": True, "status": "completed"})
    assert r.json()["ok"] is True
    db_session.expire_all()
    c = db_session.query(PaperClaim).filter(
        PaperClaim.id == "c1").first()
    assert c.ready is True
    assert c.status == "completed"


def test_run_kill_rejects_bad_id(client):
    r = client.post("/api/runs/bad+id/kill")
    assert r.json().get("ok") is False
