"""Unit tests for backend.app.health.service (PR 2 of the state-control
rewrite, 2026-06-05).

The health service is the single source of truth for "is the loop OK?".
The pill, the modal, the PI nudges, and the idle-GPU email all consume
the same HealthSnapshot. These tests pin the contract:

  * compute() never raises and always returns a well-formed snapshot.
  * The phase comes from the orchestrator.phase Setting if present, and
    from a DB-derived fallback if not.
  * Detectors are independently active; one failing detector doesn't
    blank the rest.
  * idle_gpus only fires when the phase suggests work SHOULD be
    happening (not during council_review / concluding / etc).
  * no_metric_flow fires per-run, not just one.
"""
from __future__ import annotations

import datetime as dt
import pytest


def _iso(seconds_ago: float = 0) -> str:
    return (dt.datetime.now(dt.timezone.utc)
            - dt.timedelta(seconds=seconds_ago)).isoformat()


def test_compute_returns_well_formed_snapshot(arui_env):
    """No projects, no runs — should still return a snapshot, never
    raise. Phase falls back to 'bootstrap'."""
    from backend.app.health import service
    snap = service.compute()
    assert snap.phase.phase == "bootstrap"
    assert snap.phase.fallback_used is True
    assert snap.issues == []
    assert "all systems nominal" in snap.summary.lower()


def test_phase_read_from_setting(arui_env, db_session):
    from backend.app.health import service
    from backend.app.models import Setting
    db_session.add(Setting(
        key="orchestrator.phase",
        value={"phase": "planning", "at": _iso(),
               "detail": {"idea_id": "x"}}))
    db_session.commit()
    snap = service.compute()
    assert snap.phase.phase == "planning"
    assert snap.phase.fallback_used is False
    assert snap.phase.detail == {"idea_id": "x"}


def test_phase_derived_fallback_with_running_run(
        arui_env, db_session, make_project, make_run):
    from backend.app.health import service
    make_project()
    make_run(id="r1", status="running")
    snap = service.compute()
    assert snap.phase.phase == "watching_runs"
    assert snap.phase.fallback_used is True


def test_idle_gpus_fires_in_watching_runs_phase(
        arui_env, db_session, make_project):
    """All GPUs idle for >threshold AND phase=watching_runs → issue."""
    from backend.app.health import service
    from backend.app.models import Gpu, Setting
    make_project()
    # phase = watching_runs
    db_session.add(Setting(
        key="orchestrator.phase",
        value={"phase": "watching_runs", "at": _iso(), "detail": {}}))
    # All idle
    for i in range(3):
        db_session.add(Gpu(index=i, util_pct=0, vram_used_mb=0))
    # Idle window opened 20 min ago
    db_session.add(Setting(
        key="health.idle_since",
        value={"since": _iso(seconds_ago=1200)}))
    db_session.commit()
    snap = service.compute()
    codes = [i.code for i in snap.issues]
    assert "idle_gpus" in codes
    issue = next(i for i in snap.issues if i.code == "idle_gpus")
    assert "idle" in issue.summary.lower()


def test_idle_gpus_silent_in_council_review_phase(
        arui_env, db_session, make_project):
    """During council_review the GPUs are SUPPOSED to be idle — don't
    raise an idle_gpus issue then."""
    from backend.app.health import service
    from backend.app.models import Gpu, Setting
    make_project()
    db_session.add(Setting(
        key="orchestrator.phase",
        value={"phase": "council_review", "at": _iso(), "detail": {}}))
    for i in range(3):
        db_session.add(Gpu(index=i, util_pct=0, vram_used_mb=0))
    db_session.add(Setting(
        key="health.idle_since",
        value={"since": _iso(seconds_ago=1200)}))
    db_session.commit()
    snap = service.compute()
    codes = [i.code for i in snap.issues]
    assert "idle_gpus" not in codes


def test_no_metric_flow_fires_per_run(
        arui_env, db_session, make_project, make_run):
    """Two RUNNING runs both with stale metrics → two no_metric_flow
    issues so the modal can list each with its own kill button."""
    from backend.app.health import service
    make_project()
    long_ago = _iso(seconds_ago=2000)
    make_run(id="rA", status="running",
             created_at=long_ago, started_at=long_ago)
    make_run(id="rB", status="running",
             created_at=long_ago, started_at=long_ago)
    snap = service.compute()
    nmf = [i for i in snap.issues if i.code == "no_metric_flow"]
    assert len(nmf) == 2
    assert {i.evidence["run_id"] for i in nmf} == {"rA", "rB"}


def test_no_metric_flow_skips_freshly_started_runs(
        arui_env, db_session, make_project, make_run):
    """A run that started 30s ago should not trip the no_metric_flow
    detector even if it hasn't logged anything yet."""
    from backend.app.health import service
    make_project()
    make_run(id="fresh", status="running",
             created_at=_iso(seconds_ago=30),
             started_at=_iso(seconds_ago=30))
    snap = service.compute()
    nmf = [i for i in snap.issues if i.code == "no_metric_flow"]
    assert nmf == []


def test_per_project_watchdog_param_override(
        arui_env, db_session, make_project, make_run):
    """When watchdog.config sets a longer no_metric_flow.timeout_sec,
    a run idle for 20 min should NOT fire."""
    from backend.app.health import service
    from backend.app.models import Setting
    make_project()
    long_ago = _iso(seconds_ago=1200)   # 20 min
    make_run(id="slow", status="running",
             created_at=long_ago, started_at=long_ago)
    db_session.add(Setting(
        key="watchdog.config",
        value={"no_metric_flow": {"params": {"timeout_sec": 7200}}}))
    db_session.commit()
    snap = service.compute()
    nmf = [i for i in snap.issues if i.code == "no_metric_flow"]
    assert nmf == []


def test_compute_swallows_detector_exceptions(
        arui_env, db_session, monkeypatch):
    """One bad detector must not blank the rest of the snapshot."""
    from backend.app.health import service, issues as detectors
    def boom(*_a, **_kw):
        raise RuntimeError("synthetic")
    monkeypatch.setattr(detectors, "idle_gpus", boom)
    snap = service.compute()
    assert snap.phase.phase  # still set
    # The bad detector contributed no issues but didn't kill the call.


def test_summary_includes_phase_and_top_issue(
        arui_env, db_session, make_project, make_run):
    """The pill summary should read 'phase — top issue'."""
    from backend.app.health import service
    from backend.app.models import Setting
    make_project()
    long_ago = _iso(seconds_ago=2000)
    make_run(id="r1", status="running",
             created_at=long_ago, started_at=long_ago)
    db_session.add(Setting(
        key="orchestrator.phase",
        value={"phase": "watching_runs", "at": _iso(), "detail": {}}))
    db_session.commit()
    snap = service.compute()
    assert snap.summary.startswith("watching_runs — ")
    assert "metric" in snap.summary.lower()


def test_tick_persists_snapshot(arui_env, db_session):
    """tick() should save the snapshot under 'health.snapshot' so
    other components can read the last-known value without recomputing."""
    from backend.app.health import service
    from backend.app.models import Setting
    snap = service.tick()
    row = (db_session.query(Setting)
           .filter(Setting.key == "health.snapshot").first())
    assert row is not None
    assert row.value["phase"]["phase"] == snap.phase.phase
