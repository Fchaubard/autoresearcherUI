"""AUTOPILOT: there is NO operator GPU gate. Paper runs queue straight to
'queued' (paper_runner launches them) and the ablation plan is always
auto-approved, so the paper never waits on a human to approve GPU spend.
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


def test_paper_runs_auto_queue_no_gate(client, make_project):
    make_project()
    r = client.post("/api/paper/runs/queue",
                    json={"name": "abl1", "cmd": "echo hi"})
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True
    # straight to queued, not gated behind an operator approval
    assert j["status"] == "queued" and j.get("gated") is False
    assert _run_status("abl1") == "queued"


def test_plan_is_always_approved(arui_env):
    from backend.app import paper_phase
    assert paper_phase.plan_approved() is True
