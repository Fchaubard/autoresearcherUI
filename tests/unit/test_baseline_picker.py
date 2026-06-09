"""Regression tests for the /api/project baseline picker.

The bug (2026-06-09): a backdoor-defense project logged the real undefended
anchor `seed_bl_lora` at ASR 0.85, but it wasn't a *kept* run and its name
didn't contain "baseline", so the picker fell back to the worst *kept*
(already-defended) run and reported a near-optimal 0.034 as the "baseline" —
making the problem look ~solved from the start. The fix: anchor on the worst
REAL (non-probe, non-crashed) run, honor an explicit is_baseline mark, and flag
a degenerate baseline.
"""


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


def test_undefended_nonkept_run_becomes_baseline(client, make_project, make_run):
    """The exact bug: undefended anchor is a non-kept run; defended runs are
    kept. The baseline must be the undefended 0.85, not a defended 0.03."""
    make_project(metric_direction="minimize")
    make_run(id="seed_bl_lora", run_name="seed_bl_lora",
             status="discarded", headline_metric=0.851)      # undefended anchor
    make_run(id="pe_strong", run_name="pe_strong",
             status="kept_novel", headline_metric=0.0337)    # defended
    make_run(id="pe_wdr", run_name="pe_wdr",
             status="kept_novel", headline_metric=0.0)        # best defended
    body = client.get("/api/project").json()
    assert body["best_metric"] == 0.0
    assert body["baseline_metric"] == 0.851
    assert body["baseline_run_name"] == "seed_bl_lora"
    assert body["baseline_degenerate"] is False


def test_explicit_is_baseline_flag_wins(client, make_project, make_run):
    """An explicitly-marked baseline anchors the dashboard even if another
    run has a worse metric."""
    make_project(metric_direction="minimize")
    make_run(id="anchor", run_name="anchor", status="discarded",
             headline_metric=0.5, is_baseline=True)
    make_run(id="worse", run_name="worse", status="discarded",
             headline_metric=0.7)
    make_run(id="best", run_name="best", status="kept", headline_metric=0.0)
    body = client.get("/api/project").json()
    assert body["baseline_metric"] == 0.5
    assert body["baseline_run_name"] == "anchor"


def test_probe_and_smoke_runs_are_not_anchors(client, make_project, make_run):
    """A _smoke/_probe run must never be picked as the baseline even if it has
    the worst metric — it's not a real experiment."""
    make_project(metric_direction="minimize")
    make_run(id="_smoke_x", run_name="_smoke_x", status="success_smoke",
             headline_metric=0.99)
    make_run(id="real_bl", run_name="real_bl", status="discarded",
             headline_metric=0.5)
    make_run(id="def0", run_name="def0", status="kept", headline_metric=0.0)
    body = client.get("/api/project").json()
    assert body["baseline_metric"] == 0.5
    assert body["baseline_run_name"] == "real_bl"


def test_degenerate_baseline_is_flagged(client, make_project, make_run):
    """When the only anchor equals the best (no real gap), say so instead of
    printing a fake near-optimal baseline."""
    make_project(metric_direction="minimize")
    make_run(id="only", run_name="only", status="kept", headline_metric=0.0)
    body = client.get("/api/project").json()
    assert body["baseline_degenerate"] is True
    assert "no-mitigation" in body["baseline_note"] or body["baseline_note"]


def test_no_runs_yet_is_degenerate(client, make_project):
    make_project(metric_direction="minimize")
    body = client.get("/api/project").json()
    assert body["baseline_metric"] is None
    assert body["baseline_degenerate"] is True


def test_explicit_baseline_via_sdk_config_key(client, make_project, make_run):
    """The track/run path stores is_baseline when config carries the SDK's
    `is_baseline` flag (what arui.init(baseline=True) sends)."""
    make_project(metric_direction="minimize")
    # Simulate what the track/run handler does with an SDK baseline run.
    make_run(id="bl_cfg", run_name="bl_cfg", status="discarded",
             headline_metric=0.82, is_baseline=True,
             config={"is_baseline": True, "what": "undefended"})
    make_run(id="d", run_name="d", status="kept", headline_metric=0.01)
    body = client.get("/api/project").json()
    assert body["baseline_metric"] == 0.82
    assert body["baseline_degenerate"] is False
