"""Paper-mode runs must actually register + log to the dashboard.

The bug: paper runs were launched with ARUI_INGEST_URL=.../api/track, but the
arui SDK appends /api/track/run to that base, producing /api/track/api/track/
run (a 404) so ablation runs logged NOTHING. The env prefix must use the BASE
url and forward the ingest token.
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


def test_ingest_env_uses_base_url_not_api_track(arui_env):
    from backend.app import paper
    env = paper.paper_ingest_env_prefix("myproj")
    assert "ARUI_INGEST_URL=http://127.0.0.1:" in env
    assert "/api/track" not in env            # the path-doubling bug is gone
    assert "ARUI_PROJECT=myproj" in env
    assert "PYTHONPATH=" in env and "ARUI_REPO=" in env


def test_ingest_env_includes_token_when_passcode_set(arui_env, setting_setter):
    setting_setter("onboarding", {"passcode": "secret123"})
    from backend.app import paper
    assert "ARUI_INGEST_TOKEN=secret123" in paper.paper_ingest_env_prefix("p")


def test_ingest_env_no_token_when_no_passcode(arui_env):
    from backend.app import paper
    assert "ARUI_INGEST_TOKEN" not in paper.paper_ingest_env_prefix("p")


def test_sdk_base_plus_path_hits_real_track_endpoint(client, make_project,
                                                     setting_setter):
    # what the SDK does: POST <ARUI_INGEST_URL>/api/track/run. With the fixed
    # BASE url that resolves to a real working endpoint (the doubled path 404'd).
    setting_setter("code_bless", {"status": "approved"})
    make_project()
    r = client.post("/api/track/run",
                    json={"project": "x", "name": "pr_headline_s1",
                          "config": {"what": "headline", "is_baseline": False}})
    assert r.status_code == 200
    assert r.json().get("run_id") == "pr_headline_s1"
