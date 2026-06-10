"""The dashboard phase must be COUNCIL-GATED.

The agent self-reports its phase via arui.phase(), so a self-declared
'concluding'/'complete' must NOT show as done unless the council's
completion-review actually APPROVED the conclusion. Otherwise the operator
thinks the research passed the gate when it didn't (it then gets rejected at
the paper-proposal step).
"""
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


def test_self_declared_complete_is_clamped_without_council(client, make_project):
    make_project()
    # agent self-declares it's done, but the council never approved
    client.post("/api/phase", json={"phase": "complete"})
    r = client.get("/api/phase").json()
    assert r["phase"] == "concluding"            # NOT 'complete'
    assert r.get("council_approved") is False
    assert r.get("council_status") in ("none", "rejected", "needs_more", "pending")


def test_concluding_shows_pending_while_council_reviews(client, make_project):
    make_project()
    from backend.app import council
    council._conclusion_state_set({"status": "pending"})
    client.post("/api/phase", json={"phase": "concluding"})
    r = client.get("/api/phase").json()
    assert r["phase"] == "concluding"
    assert r.get("council_approved") is False
    assert "review" in (r.get("detail", {}).get("council", "")).lower()


def test_complete_only_when_council_approved(client, make_project):
    make_project()
    from backend.app import council
    council._conclusion_state_set({"status": "approved",
                                   "council_verdict": {"verdict": "APPROVED"}})
    client.post("/api/phase", json={"phase": "complete"})
    r = client.get("/api/phase").json()
    assert r["phase"] == "complete"
    assert r.get("council_approved") is True
    assert r.get("council_status") == "approved"


def test_rejected_conclusion_keeps_phase_off_done(client, make_project):
    make_project()
    from backend.app import council
    council._conclusion_state_set({"status": "rejected"})
    client.post("/api/phase", json={"phase": "complete"})
    r = client.get("/api/phase").json()
    assert r["phase"] == "concluding"
    assert "reject" in (r.get("detail", {}).get("council", "")).lower()
