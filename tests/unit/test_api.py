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


def test_agent_raw_stream_returns_bytes_from_offset(client, monkeypatch):
    """/api/agent/raw is what the rail xterm.js subscribes to. It should
    return base64-encoded bytes for the given session+offset, and
    advance the offset.
    """
    import base64
    from backend.app import pane_stream
    sess = "agent"
    tf = pane_stream.term_file(sess)
    tf.parent.mkdir(parents=True, exist_ok=True)
    tf.write_bytes(b"\x1b[32mHello world!\x1b[0m\r\n")
    r = client.get(f"/api/agent/raw?session={sess}&offset=0")
    assert r.status_code == 200
    body = r.json()
    decoded = base64.b64decode(body["chunk"])
    assert decoded == b"\x1b[32mHello world!\x1b[0m\r\n"
    assert body["offset"] == len(decoded)
    assert body["size"] == len(decoded)
    assert "alive" in body
    # Resume from offset == size returns no new bytes.
    r2 = client.get(f"/api/agent/raw?session={sess}&offset={body['offset']}")
    body2 = r2.json()
    assert body2["chunk"] == ""
    assert body2["offset"] == body["offset"]


def test_agent_raw_stream_rejects_bad_session(client):
    """Defensive — sanitization prevents injection / path traversal."""
    r = client.get("/api/agent/raw?session=../etc/passwd&offset=0")
    assert r.status_code == 200
    body = r.json()
    assert "error" in body


def test_agent_resize_calls_tmux_resize_window(client, fake_subprocess):
    """xterm.js POSTs (cols, rows) to /api/agent/resize after FitAddon
    runs. The endpoint must call `tmux resize-window` so Claude Code
    redraws at the correct width — otherwise its 210-wide UI wraps
    mid-character and the rail terminal is illegible (the bug Francois
    hit on 2026-05-31)."""
    r = client.post("/api/agent/resize",
                    json={"session": "agent", "cols": 110, "rows": 38})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["cols"] == 110 and body["rows"] == 38
    # Confirm we issued resize-window AND a Ctrl-L redraw.
    cmds = [" ".join(c["args"]) for c in fake_subprocess]
    assert any("resize-window" in c and "-x 110" in c and "-y 38" in c
               for c in cmds), cmds
    assert any("send-keys" in c and "C-l" in c for c in cmds), cmds


def test_agent_resize_rejects_out_of_range_dimensions(client, fake_subprocess):
    """Cols/rows are bounds-checked so a malformed client can't
    crash tmux with absurd values."""
    for bad in ({"cols": 5, "rows": 30},   # cols too small
                {"cols": 1000, "rows": 30}, # cols too large
                {"cols": 100, "rows": 3},   # rows too small
                {"cols": 100, "rows": 500}, # rows too large
                ):
        r = client.post("/api/agent/resize",
                        json={"session": "agent", **bad})
        assert r.json().get("ok") is False, bad


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


# ──────────── /sessions/create — the "+ new" button bug ────────────────────
# Regression for the bug where a missing `import shlex` made the endpoint
# raise NameError, which FastAPI turned into a 500 HTML body
# ("Internal Server Error"). The frontend then crashed inside JSON.parse
# with "SyntaxError: Unexpected token 'I'…". The contract now is:
# the endpoint NEVER returns non-JSON — on any failure path it returns
# {"ok": False, "error": "<msg>"} with HTTP 200.


def _ok_subprocess_handler(fake_subprocess):
    """Make `has-session` return rc=1 (doesn't exist) and everything else
    return rc=0. The endpoint can then proceed past the existence check."""

    class FC:
        def __init__(self, stdout="", stderr="", returncode=0):
            self.stdout, self.stderr, self.returncode = (
                stdout, stderr, returncode)

    def _h(args, **kw):
        if len(args) >= 2 and args[0] == "tmux" and args[1] == "has-session":
            return FC(returncode=1)  # not present → caller may create
        return FC(returncode=0)

    fake_subprocess.set_handler(_h)


def test_sessions_create_happy_path_returns_json_ok(client, fake_subprocess):
    """A normal create call returns JSON with ok=True."""
    _ok_subprocess_handler(fake_subprocess)
    r = client.post("/api/sessions/create", json={"session": "dbg1"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert body["ok"] is True
    assert body["session"] == "dbg1"
    # verify we actually invoked tmux new-session
    assert any(
        c["args"][:2] == ["tmux", "new-session"]
        for c in fake_subprocess
    )


def test_sessions_create_bad_name_returns_json_error(client, fake_subprocess):
    """Garbage name → ok=False, valid JSON, NOT 500."""
    _ok_subprocess_handler(fake_subprocess)
    r = client.post("/api/sessions/create", json={"session": "bad name!!"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert body["ok"] is False
    assert "1-60 chars" in body["error"]


def test_sessions_create_empty_body_returns_json_error(client, fake_subprocess):
    """No body at all → still JSON ok=False, never a 500."""
    _ok_subprocess_handler(fake_subprocess)
    r = client.post("/api/sessions/create")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert body["ok"] is False
    assert body["error"]


def test_sessions_create_reserved_name(client, fake_subprocess):
    """Infra-session name is reserved — clean JSON refusal."""
    _ok_subprocess_handler(fake_subprocess)
    r = client.post("/api/sessions/create", json={"session": "agent"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "reserved" in body["error"]


def test_sessions_create_already_exists(client, fake_subprocess):
    """tmux has-session returns rc=0 → JSON 'already exists' message."""

    class FC:
        def __init__(self, returncode=0):
            self.stdout, self.stderr, self.returncode = "", "", returncode

    # rc=0 on every call means has-session reports it exists.
    fake_subprocess.set_handler(lambda a, **kw: FC(returncode=0))
    r = client.post("/api/sessions/create", json={"session": "dbg2"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "already exists" in body["error"]


def test_sessions_create_no_workspace_dir(client, fake_subprocess, arui_env):
    """DATA_DIR / 'workspace' doesn't exist → cwd falls back to /root,
    endpoint still returns JSON ok=True (not a 500)."""
    _ok_subprocess_handler(fake_subprocess)
    # arui_env's tmp data_dir has no 'workspace' subdir → exercises the
    # `ws.exists()` False branch.
    r = client.post("/api/sessions/create", json={"session": "dbg3"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["cwd"] == "/root"


def test_sessions_create_with_workspace_dir(client, fake_subprocess, arui_env):
    """If DATA_DIR/workspace/<proj>/ exists, cwd is set to it. This
    exercises the previously-unimported `shlex.quote(cwd)` line — the
    original NameError bug. With the import in place, this returns JSON
    ok=True instead of crashing."""
    ws = arui_env / "workspace" / "myproj"
    ws.mkdir(parents=True, exist_ok=True)
    _ok_subprocess_handler(fake_subprocess)
    r = client.post("/api/sessions/create", json={"session": "dbg4"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["cwd"].endswith("myproj")


def test_sessions_create_tmux_failure_returns_json_error(
    client, fake_subprocess,
):
    """If tmux new-session returns non-zero, we relay stderr as JSON
    rather than blowing up with a 500."""

    class FC:
        def __init__(self, stdout="", stderr="", returncode=0):
            self.stdout, self.stderr, self.returncode = (
                stdout, stderr, returncode)

    def _h(args, **kw):
        if args[:2] == ["tmux", "has-session"]:
            return FC(returncode=1)
        if args[:2] == ["tmux", "new-session"]:
            return FC(stderr="tmux: server not running", returncode=1)
        return FC(returncode=0)

    fake_subprocess.set_handler(_h)
    r = client.post("/api/sessions/create", json={"session": "dbg5"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "tmux" in body["error"]


def test_sessions_create_auth_import_failure_still_returns_json(
    client, fake_subprocess, monkeypatch,
):
    """If `auth._saved_passcode()` blows up, the inner try/except swallows
    it and we still get a clean JSON ok=True."""
    _ok_subprocess_handler(fake_subprocess)
    from backend.app import auth

    def boom():
        raise RuntimeError("synthetic auth blow-up")

    monkeypatch.setattr(auth, "_saved_passcode", boom)
    r = client.post("/api/sessions/create", json={"session": "dbg6"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True


def test_sessions_create_pane_stream_failure_still_returns_json(
    client, fake_subprocess, monkeypatch,
):
    """pane_stream.enable() crashing must NOT break the response."""
    _ok_subprocess_handler(fake_subprocess)
    from backend.app import pane_stream
    monkeypatch.setattr(
        pane_stream, "enable",
        lambda name: (_ for _ in ()).throw(RuntimeError("pipe-pane busted")),
    )
    r = client.post("/api/sessions/create", json={"session": "dbg7"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True


def test_sessions_create_catastrophic_failure_returns_json_not_html(
    client, fake_subprocess, monkeypatch,
):
    """Even if subprocess.run itself raises (e.g. tmux binary missing),
    the endpoint MUST return JSON ok=False, never an HTML 500 page —
    that's what made the frontend crash with 'SyntaxError: Unexpected
    token "I"' in the original bug report.
    """
    import subprocess as _sp

    def boom(*a, **kw):
        raise FileNotFoundError("tmux: command not found")

    monkeypatch.setattr(_sp, "run", boom, raising=True)
    r = client.post("/api/sessions/create", json={"session": "dbg8"})
    # The critical assertions: 200 + JSON body.
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert body["ok"] is False
    assert "tmux" in body["error"] or "failed" in body["error"]
