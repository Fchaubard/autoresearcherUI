"""Unit tests for the track_finish status taxonomy
(RESEARCH_IMPROVEMENT_PLAN.md #4).

Goal: every finished run must land in exactly one of:
  - success_smoke   (_probe / _smoke runs)
  - kept_novel      (real, finite metric, novel config)
  - kept_replicate  (real, finite metric, explicit seed replicate)
  - crashed         (non-finite / divergent metric)
  - discarded       (deferred to other paths — track_finish never
                    produces this directly; the duplicate killer at
                    /api/track/run does)
"""
from __future__ import annotations

import pytest


@pytest.fixture
def client(arui_env, fake_subprocess):
    """A FastAPI TestClient bound to ONLY the api router so we exercise
    the real HTTP path (matches the existing test_research_pause pattern)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.app.api import router
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as c:
        yield c


def _make_proj(db_session, metric="val_acc", direction="maximize"):
    """Minimal Project row so track_finish can resolve the headline."""
    from backend.app.models import Project
    proj = Project(id="proj-test", name="t", status="running",
                   validation_metric=metric,
                   metric_direction=direction,
                   gpu_count=1)
    db_session.add(proj)
    db_session.commit()
    return proj


def _start_run(client, name, config=None):
    """Helper: POST /api/track/run and assert it landed."""
    r = client.post("/api/track/run",
                    json={"name": name, "config": config or {}})
    assert r.status_code == 200, r.text
    return r.json()


def _finish_run(client, name, summary):
    """Helper: POST /api/track/finish."""
    r = client.post("/api/track/finish",
                    json={"run_id": name, "summary": summary})
    assert r.status_code == 200, r.text
    return r.json()


def _status(db_session, run_id):
    from backend.app.models import Run
    db_session.expire_all()
    return db_session.query(Run).filter(Run.id == run_id).first().status


# ─────────────────────────── success_smoke ────────────────────────────


def test_smoke_run_becomes_success_smoke(client, db_session):
    """A _smoke run with a finite metric is logged as success_smoke —
    informational only; never on the frontier."""
    _make_proj(db_session)
    _start_run(client, "_smoke_001", {"lr": 0.001})
    _finish_run(client, "_smoke_001", {"val_acc": 1.0})
    assert _status(db_session, "_smoke_001") == "success_smoke"


def test_probe_run_becomes_success_smoke(client, db_session):
    """_probe runs follow the same rule as _smoke (both are pre-bless
    sanity tests, not real experiments)."""
    _make_proj(db_session)
    _start_run(client, "_probe_xyz", {"lr": 0.001})
    _finish_run(client, "_probe_xyz", {"val_acc": 0.5})
    assert _status(db_session, "_probe_xyz") == "success_smoke"


# ─────────────────────────── kept_novel ───────────────────────────────


def test_real_novel_finite_run_becomes_kept_novel(client, db_session):
    """A first-time config with a finite metric → kept_novel."""
    _make_proj(db_session)
    _start_run(client, "exp-A", {"lr": 0.003, "model": "tf"})
    _finish_run(client, "exp-A", {"val_acc": 0.42})
    assert _status(db_session, "exp-A") == "kept_novel"


# ─────────────────────────── kept_replicate ───────────────────────────


def test_seed_replicate_config_becomes_kept_replicate(client, db_session):
    """An explicit seed_replicate config → kept_replicate, even when
    finite + finished. The frontier excludes these."""
    _make_proj(db_session)
    # Original novel run claims the hash.
    _start_run(client, "exp-orig", {"lr": 0.003, "model": "tf"})
    # Replicate must declare itself; otherwise /api/track/run rejects
    # it with 409 and it never reaches /track/finish at all.
    _start_run(client, "exp-rep",
               {"lr": 0.003, "model": "tf",
                "idea_class": "REPRODUCE"})
    _finish_run(client, "exp-rep", {"val_acc": 0.41})
    assert _status(db_session, "exp-rep") == "kept_replicate"


def test_seed_prefix_run_becomes_kept_replicate(client, db_session):
    """run_id starting with seed_ is also a valid replicate signal."""
    _make_proj(db_session)
    _start_run(client, "exp-A", {"lr": 0.003, "model": "tf"})
    _start_run(client, "seed_2", {"lr": 0.003, "model": "tf"})
    _finish_run(client, "seed_2", {"val_acc": 0.43})
    assert _status(db_session, "seed_2") == "kept_replicate"


# ─────────────────────────── crashed ──────────────────────────────────


def test_crashed_run_status_unchanged(client, db_session):
    """No finite metric ⇒ crashed, regardless of config noveltyness."""
    _make_proj(db_session, metric="val_loss", direction="minimize")
    _start_run(client, "boom", {"lr": 9e9})
    _finish_run(client, "boom", {})              # no summary metric
    assert _status(db_session, "boom") == "crashed"


def test_divergent_loss_becomes_crashed(client, db_session):
    """Loss-style metric >= 5e4 is divergence (the existing
    _is_crashed contract); status must still be 'crashed', not
    kept_novel."""
    _make_proj(db_session, metric="val_loss", direction="minimize")
    _start_run(client, "diverge", {"lr": 0.1})
    _finish_run(client, "diverge", {"val_loss": 1e9})
    assert _status(db_session, "diverge") == "crashed"


# ─────────────────────────── frontier (UI) gate ───────────────────────


def test_only_kept_novel_counts_on_frontier(client, db_session):
    """The frontier function in static/app.js only counts kept_novel +
    (legacy) kept. Verified here by inspecting the source — keeps the
    contract visible to backend tests so a future refactor that demotes
    kept_novel can't silently break the frontier."""
    from backend.app.config import ROOT
    src = (ROOT / "backend" / "app" / "static" / "app.js").read_text()
    # Find the FRONTIER_OK set and verify exactly the right members.
    assert "FRONTIER_OK" in src
    # The whitelist contains kept_novel + the legacy kept fallback.
    assert "'kept_novel'" in src or "\"kept_novel\"" in src
    # Smoke and replicate are explicitly NOT in the whitelist.
    # (Search for the FRONTIER_OK definition line specifically.)
    idx = src.find("FRONTIER_OK")
    line_end = src.find("\n", idx)
    frontier_def = src[idx:line_end]
    assert "success_smoke" not in frontier_def
    assert "kept_replicate" not in frontier_def
