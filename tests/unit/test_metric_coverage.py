"""Unit tests for the /api/runs/{id}/metric_coverage endpoint.

Bug B (Francois, 2026-06-04): users saw "(not logged)" on real
kept_novel runs even after the REQUIRED PLOTS / log_defaults shipped.
The coverage endpoint surfaces, for one run, exactly which of the seven
required defaults are logged vs missing — so we can both debug live and
drive the drawer's per-key hint.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def client(arui_env):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.app.api import router
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as c:
        yield c


def test_coverage_empty_run_returns_all_missing(client, make_project, make_run):
    """A brand-new run with nothing logged → missing == required, logged == []."""
    make_project()
    rid = "run-empty"
    make_run(id=rid)
    r = client.get(f"/api/runs/{rid}/metric_coverage")
    assert r.status_code == 200
    body = r.json()
    assert body["run_id"] == rid
    from backend.app import api
    assert tuple(body["required"]) == api.REQUIRED_DEFAULT_METRICS
    assert body["logged"] == []
    assert sorted(body["missing"]) == sorted(api.REQUIRED_DEFAULT_METRICS)


def test_coverage_partial_run(client, make_project, make_run):
    """Run logged train_loss + val_loss → those two appear in `logged`,
    the rest in `missing`."""
    from backend.app import metrics
    make_project()
    rid = "run-partial"
    make_run(id=rid)
    metrics.append(rid, [
        {"key": "train_loss", "step": 0, "value": 1.0, "wall_time": 0.0},
        {"key": "val_loss", "step": 0, "value": 0.9, "wall_time": 0.0},
    ])
    body = client.get(f"/api/runs/{rid}/metric_coverage").json()
    assert sorted(body["logged"]) == ["train_loss", "val_loss"]
    assert "val_acc" in body["missing"]
    assert "lr" in body["missing"]
    assert "train_loss" not in body["missing"]


def test_coverage_full_run_no_missing(client, make_project, make_run):
    """A run that logged all seven required keys → missing == []."""
    from backend.app import metrics, api
    make_project()
    rid = "run-full"
    make_run(id=rid)
    metrics.append(rid, [
        {"key": k, "step": 0, "value": 1.0, "wall_time": 0.0}
        for k in api.REQUIRED_DEFAULT_METRICS
    ])
    body = client.get(f"/api/runs/{rid}/metric_coverage").json()
    assert body["missing"] == []
    assert sorted(body["logged"]) == sorted(api.REQUIRED_DEFAULT_METRICS)


def test_coverage_includes_extra_keys_in_all_keys(client, make_project,
                                                  make_run):
    """The endpoint also reports `all_keys` — every key the run logged,
    including ones outside the required-defaults set. That's what the
    drawer's "Other metrics" section is built from."""
    from backend.app import metrics
    make_project()
    rid = "run-extras"
    make_run(id=rid)
    metrics.append(rid, [
        {"key": "train_loss", "step": 0, "value": 1.0, "wall_time": 0.0},
        {"key": "my_custom_score", "step": 0, "value": 0.5, "wall_time": 0.0},
    ])
    body = client.get(f"/api/runs/{rid}/metric_coverage").json()
    assert "my_custom_score" in body["all_keys"]
    assert "train_loss" in body["all_keys"]


def test_coverage_unknown_run_returns_full_missing(client):
    """An unknown run_id is not a 500 — the endpoint just reports nothing
    logged. Lets the drawer render even for ad-hoc / archived runs."""
    body = client.get("/api/runs/does-not-exist/metric_coverage").json()
    assert body["logged"] == []
    from backend.app import api
    assert sorted(body["missing"]) == sorted(api.REQUIRED_DEFAULT_METRICS)
