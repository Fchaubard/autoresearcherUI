"""The author must be able to FILE claims it whittles (create, not just update).
Without a create endpoint, an author entering paper mode without a council
proposal was stuck at 0 claims and the flow stalled.
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


def test_create_claim(client):
    r = client.post("/api/paper/claims",
                    json={"title": "WDR neutralizes rare-string backdoors",
                          "evidence_strength": "strong", "novelty": "medium"})
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True and j["id"].startswith("pc-")
    # it persisted
    from backend.app.db import SessionLocal
    from backend.app.models import PaperClaim
    db = SessionLocal()
    try:
        c = db.query(PaperClaim).filter(PaperClaim.id == j["id"]).first()
        assert c is not None and "WDR" in c.title
        assert c.evidence_strength == "strong" and c.status == "active"
    finally:
        db.close()


def test_create_claim_requires_title(client):
    assert client.post("/api/paper/claims", json={}).json()["ok"] is False


def test_created_claims_get_sequential_idx(client):
    a = client.post("/api/paper/claims", json={"title": "claim A"}).json()["id"]
    b = client.post("/api/paper/claims", json={"title": "claim B"}).json()["id"]
    from backend.app.db import SessionLocal
    from backend.app.models import PaperClaim
    db = SessionLocal()
    try:
        idxs = sorted(c.idx for c in db.query(PaperClaim).all())
        assert idxs == [0, 1] and a != b
    finally:
        db.close()
