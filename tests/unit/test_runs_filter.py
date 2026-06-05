"""Unit tests for /api/runs ?status= filtering.

Bug A: previously the endpoint ignored the query string and always
returned ALL runs.  These tests pin the new behavior:
- no filter → all runs
- single status → only matching runs
- comma list → union of matching runs
- unknown status → empty result
- case-insensitive matching
- mixed kept/kept_novel returned together
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


@pytest.fixture
def seeded(make_project, make_run):
    make_project()
    make_run(id="r-run-1", status="running")
    make_run(id="r-run-2", status="running")
    make_run(id="r-run-3", status="running")
    make_run(id="r-kept-1", status="kept")
    make_run(id="r-kept-2", status="kept")
    make_run(id="r-novel-1", status="kept_novel")
    make_run(id="r-crashed-1", status="crashed")
    make_run(id="r-discarded-1", status="discarded")
    return None


def _ids(rows):
    return sorted(r["id"] for r in rows)


def test_no_filter_returns_all_runs(client, seeded):
    r = client.get("/api/runs")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 8


def test_single_status_running_only(client, seeded):
    r = client.get("/api/runs?status=running")
    assert r.status_code == 200
    rows = r.json()
    assert _ids(rows) == ["r-run-1", "r-run-2", "r-run-3"]
    # all results must have status="running"
    assert all(row["status"] == "running" for row in rows)


def test_single_status_kept_excludes_crashed_and_running(client, seeded):
    r = client.get("/api/runs?status=kept")
    rows = r.json()
    assert _ids(rows) == ["r-kept-1", "r-kept-2"]


def test_comma_separated_list_kept_and_kept_novel(client, seeded):
    r = client.get("/api/runs?status=kept,kept_novel")
    rows = r.json()
    assert _ids(rows) == ["r-kept-1", "r-kept-2", "r-novel-1"]


def test_unknown_status_returns_empty(client, seeded):
    r = client.get("/api/runs?status=nonexistent_status")
    assert r.status_code == 200
    assert r.json() == []


def test_status_filter_is_case_insensitive(client, seeded):
    r = client.get("/api/runs?status=RUNNING")
    rows = r.json()
    assert _ids(rows) == ["r-run-1", "r-run-2", "r-run-3"]


def test_mixed_case_comma_list(client, seeded):
    r = client.get("/api/runs?status=Kept,KEPT_NOVEL,running")
    rows = r.json()
    assert _ids(rows) == [
        "r-kept-1", "r-kept-2", "r-novel-1",
        "r-run-1", "r-run-2", "r-run-3",
    ]


def test_empty_status_treated_as_no_filter(client, seeded):
    r = client.get("/api/runs?status=")
    rows = r.json()
    assert len(rows) == 8


def test_whitespace_in_comma_list_tolerated(client, seeded):
    r = client.get("/api/runs?status= kept , kept_novel ")
    rows = r.json()
    assert _ids(rows) == ["r-kept-1", "r-kept-2", "r-novel-1"]
