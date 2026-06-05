"""Regression test for the hero-chart 'kept runs not on plot' bug
(Francois, 2026-06-03).

The dashboard runs table had a long list of runs with metric=1.0000
flagged KEPT, but the Autoresearch-progress chart at the top showed
no frontier improvement line. Root cause: the project's
`metric_direction` had been saved as 'minimize' (default for the old
broken val_loss dropdown) and was never corrected when the user later
edited the metric to 'gsm8k_val_acc'. With minimize, 1.0 > 0.973 is
"worse", so the frontier function never flagged any run as improving.

Fix: GET /api/project self-heals by inferring direction from the
metric NAME via the same _maximize_tokens / _minimize_tokens
substring check used at onboarding, overwriting the stored value
when they disagree.
"""
from __future__ import annotations

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


def _make_project(metric: str, direction: str):
    """Create a Project row with the given metric name + direction."""
    from backend.app.db import SessionLocal
    from backend.app.models import Project
    s = SessionLocal()
    try:
        p = Project(
            id="proj-test",
            name="test",
            purpose="x",
            validation_metric=metric,
            metric_direction=direction,
            status="awaiting agent",
            gpu_count=0,
            created_at="2026-06-03T00:00:00+00:00",
        )
        s.add(p)
        s.commit()
    finally:
        s.close()


def _read_project_dir():
    from backend.app.db import SessionLocal
    from backend.app.models import Project
    s = SessionLocal()
    try:
        return s.query(Project).first().metric_direction
    finally:
        s.close()


def test_project_endpoint_self_heals_acc_with_stale_minimize(client):
    """The exact bug Francois hit: metric=gsm8k_val_acc but direction=minimize.
    /api/project should detect the mismatch and persist 'maximize'."""
    _make_project("gsm8k_val_acc", "minimize")
    r = client.get("/api/project")
    assert r.status_code == 200
    body = r.json()
    assert body["metric_direction"] == "maximize", (
        f"Self-heal failed: returned {body['metric_direction']!r}; "
        "expected 'maximize' for an accuracy metric.")
    # And it was PERSISTED back to the DB so the next request is fast.
    assert _read_project_dir() == "maximize"


def test_project_endpoint_self_heals_loss_with_stale_maximize(client):
    """Symmetric case: metric=val_loss but direction=maximize (a user
    or migration could plausibly mess this up). Should correct to minimize."""
    _make_project("val_loss", "maximize")
    r = client.get("/api/project")
    assert r.status_code == 200
    assert r.json()["metric_direction"] == "minimize"


def test_project_endpoint_does_not_meddle_when_already_correct(client):
    """Don't churn the DB on every GET — only write when there's a
    real disagreement."""
    _make_project("gsm8k_val_acc", "maximize")
    r = client.get("/api/project")
    assert r.status_code == 200
    assert r.json()["metric_direction"] == "maximize"
    # Symmetric — loss + minimize is correct, don't touch it.
    from backend.app.db import SessionLocal
    from backend.app.models import Project
    s = SessionLocal()
    p = s.query(Project).first()
    p.validation_metric = "train_loss"
    p.metric_direction = "minimize"
    s.commit(); s.close()
    r2 = client.get("/api/project")
    assert r2.json()["metric_direction"] == "minimize"


def test_project_endpoint_leaves_ambiguous_metric_alone(client):
    """If the metric name gives no signal (e.g. a fully custom name the
    heuristic can't classify), don't overwrite the user's stored
    direction. They may know something the heuristic doesn't."""
    _make_project("my_weird_custom_thing", "minimize")
    r = client.get("/api/project")
    assert r.status_code == 200
    assert r.json()["metric_direction"] == "minimize"
    # And switching to maximize still doesn't trigger a re-write.
    from backend.app.db import SessionLocal
    from backend.app.models import Project
    s = SessionLocal()
    p = s.query(Project).first()
    p.metric_direction = "maximize"
    s.commit(); s.close()
    r2 = client.get("/api/project")
    assert r2.json()["metric_direction"] == "maximize"


@pytest.mark.parametrize("metric,want", [
    ("gsm8k_val_acc",     "maximize"),
    ("gsm8k_test_acc",    "maximize"),
    ("val_acc",           "maximize"),
    ("test_accuracy",     "maximize"),
    ("squad_em",          "maximize"),
    ("exact_match",       "maximize"),
    ("pass@1",            "maximize"),
    ("auc",               "maximize"),
    ("bleu_score",        "maximize"),
    ("val_loss",          "minimize"),
    ("train_loss",        "minimize"),
    ("perplexity",        "minimize"),
    ("rmse",              "minimize"),
    ("mse",               "minimize"),
    ("fid",               "minimize"),
])
def test_infer_metric_direction(arui_env, metric, want):
    """Pin every common metric to the right direction. _infer_metric_direction
    is what the /project endpoint uses to self-heal."""
    from backend.app.api import _infer_metric_direction
    assert _infer_metric_direction(metric) == want
