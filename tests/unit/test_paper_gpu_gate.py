"""The operator GPU gate must be REAL: paper runs queued before the operator
approves the ablation plan are 'proposed' (not dispatched to a GPU), and
approving the plan flips them to 'queued'.
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


def _run_status(rid_name):
    from backend.app.db import SessionLocal
    from backend.app.models import Run
    db = SessionLocal()
    try:
        r = db.query(Run).filter(Run.run_name == rid_name).first()
        return r.status if r else None
    finally:
        db.close()


def test_queue_is_proposed_before_plan_approval(client, make_project):
    make_project()
    r = client.post("/api/paper/runs/queue",
                    json={"name": "abl1", "cmd": "echo hi"})
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True
    assert j["status"] == "proposed" and j["gated"] is True
    assert _run_status("abl1") == "proposed"


def test_approve_plan_releases_proposed_runs(client, make_project):
    make_project()
    client.post("/api/paper/runs/queue", json={"name": "abl1", "cmd": "echo hi"})
    assert _run_status("abl1") == "proposed"
    from backend.app import paper_phase
    out = paper_phase.approve_plan(by="op")
    assert out["queued_count"] >= 1
    assert _run_status("abl1") == "queued"          # released to the scheduler
    # and once approved, new runs queue directly
    r2 = client.post("/api/paper/runs/queue",
                     json={"name": "abl2", "cmd": "echo hi"})
    assert r2.json()["status"] == "queued"
