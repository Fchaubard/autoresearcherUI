"""Tests pinning the "default plots always logged" contract.

User bug (Francois, 2026-06-04): the run drawer's "All plots" section
showed `val_loss, val_acc, lr, train_loss, train_acc, time_per_step,
samples_per_sec` all marked "(not logged)". Two-pronged fix:

  A. `arui.log_defaults(...)` always emits all seven keys, auto-computing
     `lr` from the optimizer, `time_per_step` from a stopwatch, and
     `samples_per_sec` from `batch_size / time_per_step`.

  B. /api/track/finish audits the run's logged keys and emits a warning
     Event for every missing required default.

These tests pin both contracts so regressions are loud.
"""
from __future__ import annotations

import math

import pytest


# ───────────────────────── A. SDK helper contract ─────────────────────────

def test_log_defaults_emits_all_required_keys(monkeypatch):
    """`arui.log_defaults(...)` must produce a logged payload containing
    every key in REQUIRED_DEFAULT_KEYS — even when the caller only knows
    train_loss. Missing values become NaN so the metric store records
    them as present-but-empty rather than absent.
    """
    import arui

    # Bypass the network: stub the Run.log method to capture the call.
    captured: list[tuple[dict, int | None]] = []

    class _FakeRun:
        def log(self, metrics: dict, step: int | None = None) -> None:
            captured.append((dict(metrics), step))

    monkeypatch.setattr(arui, "_active", _FakeRun())
    # Mimic a torch optimizer surface.
    fake_optim = type("Opt", (), {"param_groups": [{"lr": 3e-4}]})()

    payload = arui.log_defaults(
        optimizer=fake_optim, step=42, batch_size=8,
        train_loss=0.71)

    # Every required key MUST be in the logged metrics dict.
    for key in arui.REQUIRED_DEFAULT_KEYS:
        assert key in payload, f"log_defaults dropped required key {key!r}"
    # And the call actually went out to the run.
    assert len(captured) == 1
    logged, step = captured[0]
    assert step == 42
    for key in arui.REQUIRED_DEFAULT_KEYS:
        assert key in logged
    # lr was auto-extracted from the optimizer.
    assert logged["lr"] == pytest.approx(3e-4)
    # train_loss was forwarded verbatim.
    assert logged["train_loss"] == pytest.approx(0.71)
    # val_loss/val_acc not provided → NaN sentinel (still present).
    assert math.isnan(logged["val_loss"])
    assert math.isnan(logged["val_acc"])


def test_log_defaults_computes_time_per_step_and_samples_per_sec(monkeypatch):
    """The stopwatch path: the SECOND call to log_defaults() produces a
    non-NaN `time_per_step` (and `samples_per_sec` when batch_size is
    known). First call has no prior step so both are NaN."""
    import arui

    captured: list[dict] = []

    class _FakeRun:
        def log(self, metrics, step=None):
            captured.append(dict(metrics))

    monkeypatch.setattr(arui, "_active", _FakeRun())
    # Reset stopwatch state so we don't inherit a clock from a prior test.
    arui._step_clock.clear()

    # Mock time.time so we can deterministically assert the delta.
    fake_now = [1000.0]
    monkeypatch.setattr(arui.time, "time", lambda: fake_now[0])

    arui.log_defaults(step=0, batch_size=32, train_loss=1.0)
    fake_now[0] = 1002.0       # +2.0 s elapsed
    arui.log_defaults(step=1, batch_size=32, train_loss=0.9)

    first, second = captured
    # First call: no prior tick → NaN.
    assert math.isnan(first["time_per_step"])
    assert math.isnan(first["samples_per_sec"])
    # Second call: 2 s elapsed, 32 samples → 16 samples/s.
    assert second["time_per_step"] == pytest.approx(2.0)
    assert second["samples_per_sec"] == pytest.approx(16.0)


def test_log_defaults_requires_init():
    """Calling log_defaults() before init() must raise — same contract as
    arui.log(). Prevents the agent from silently no-op'ing logs."""
    import arui
    arui._active = None
    with pytest.raises(RuntimeError):
        arui.log_defaults(step=0)


def test_required_default_keys_match_backend(arui_env):
    """The SDK's REQUIRED_DEFAULT_KEYS and the backend's
    REQUIRED_DEFAULT_METRICS MUST stay in sync. If anyone adds a key in
    one place and forgets the other, the audit either misses it or
    fires spuriously."""
    import arui
    from backend.app import api
    assert tuple(arui.REQUIRED_DEFAULT_KEYS) == api.REQUIRED_DEFAULT_METRICS


# ──────────────────────── B. backend audit contract ────────────────────────

def test_finish_warns_when_default_metric_missing(arui_env, make_project,
                                                  make_run, monkeypatch):
    """track_finish must emit one warning Event per required default
    metric that the run never logged. Pinned so the dashboard's missing-
    plot symptom can't come back silently."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.app import api, metrics
    from backend.app.db import SessionLocal
    from backend.app.models import Event
    # The council code-bless gate would block /api/track/run; bypass.
    monkeypatch.setattr("backend.app.council.is_code_blessed",
                        lambda: True, raising=True)
    # Avoid notify side-effects (real email plumbing).
    monkeypatch.setattr("backend.app.notify.on_run_finished",
                        lambda *_a, **_kw: None, raising=True)

    proj = make_project()
    rid = "run-default-audit"
    make_run(id=rid, project_id=proj.id, run_name=rid)

    # The run logged ONLY train_loss + val_loss — the other five defaults
    # are missing and must surface as warning Events.
    metrics.append(rid, [
        {"key": "train_loss", "step": 0, "value": 1.0, "wall_time": 0.0},
        {"key": "val_loss", "step": 0, "value": 0.9, "wall_time": 0.0},
    ])

    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app) as client:
        r = client.post("/api/track/finish",
                        json={"run_id": rid, "summary": {"val_loss": 0.42}})
        assert r.status_code == 200

    # Pull the missing_default_metric events out of the DB.
    db = SessionLocal()
    try:
        evs = (db.query(Event)
               .filter(Event.run_id == rid,
                       Event.type == "missing_default_metric")
               .all())
    finally:
        db.close()
    # Should be exactly the five defaults we did NOT log.
    missing_in_events = {e.message.split("'")[1] for e in evs}
    expected_missing = (set(api.REQUIRED_DEFAULT_METRICS)
                        - {"train_loss", "val_loss"})
    assert missing_in_events == expected_missing, (
        f"Expected missing={expected_missing}, got events for "
        f"{missing_in_events}")
    # Severity must be warning so the drawer surfaces it loudly.
    for ev in evs:
        assert ev.severity == "warning"
        assert ev.actor == "system"


def test_finish_emits_no_warnings_when_all_required_logged(
        arui_env, make_project, make_run, monkeypatch):
    """Sanity: a well-behaved run that logs all seven defaults gets ZERO
    missing_default_metric events. Without this we can't tell whether
    the audit is firing spuriously."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.app import api, metrics
    from backend.app.db import SessionLocal
    from backend.app.models import Event
    monkeypatch.setattr("backend.app.council.is_code_blessed",
                        lambda: True, raising=True)
    monkeypatch.setattr("backend.app.notify.on_run_finished",
                        lambda *_a, **_kw: None, raising=True)

    proj = make_project()
    rid = "run-default-ok"
    make_run(id=rid, project_id=proj.id, run_name=rid)
    metrics.append(rid, [
        {"key": k, "step": 0, "value": 0.0, "wall_time": 0.0}
        for k in api.REQUIRED_DEFAULT_METRICS
    ])

    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app) as client:
        r = client.post("/api/track/finish",
                        json={"run_id": rid, "summary": {"val_loss": 0.42}})
        assert r.status_code == 200

    db = SessionLocal()
    try:
        evs = (db.query(Event)
               .filter(Event.run_id == rid,
                       Event.type == "missing_default_metric")
               .all())
    finally:
        db.close()
    assert evs == [], (
        f"Expected no missing-metric events for a fully-logged run, "
        f"got {[e.message for e in evs]}")


def test_finish_skips_audit_for_probe_runs(
        arui_env, make_project, make_run, monkeypatch):
    """A `_probe`/`_smoke` run is a pre-flight sanity check, not a real
    experiment — the audit must NOT fire for those, otherwise the agent
    gets spammed with warnings every time it runs an importable-only
    smoke test."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.app import api
    from backend.app.db import SessionLocal
    from backend.app.models import Event
    monkeypatch.setattr("backend.app.council.is_code_blessed",
                        lambda: True, raising=True)
    monkeypatch.setattr("backend.app.notify.on_run_finished",
                        lambda *_a, **_kw: None, raising=True)

    proj = make_project()
    rid = "_probe-import-only"
    make_run(id=rid, project_id=proj.id, run_name=rid)
    # No metrics logged at all — a strict audit would emit 7 warnings.
    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app) as client:
        r = client.post("/api/track/finish",
                        json={"run_id": rid, "summary": {}})
        assert r.status_code == 200

    db = SessionLocal()
    try:
        evs = (db.query(Event)
               .filter(Event.run_id == rid,
                       Event.type == "missing_default_metric")
               .all())
    finally:
        db.close()
    assert evs == [], (
        "_probe / _smoke runs must skip the required-metric audit")


# ──────────────────────── C. agent prompt contract ────────────────────────

def test_setup_prompt_demands_default_plots():
    """The agent's setup prompt MUST tell the agent to call
    arui.log_defaults so the defaults always get logged. Pins the
    structural side of the fix — without this, even a perfect SDK does
    nothing because the agent doesn't know it exists."""
    from backend.app import realrun
    text = realrun.DEFAULT_AGENT_INSTRUCTIONS
    assert "REQUIRED PLOTS" in text, (
        "setup_prompt is missing the REQUIRED PLOTS section — the "
        "agent has no instruction to log the seven defaults")
    assert "arui.log_defaults" in text
    for key in ("val_loss", "val_acc", "lr", "train_loss",
                "train_acc", "time_per_step", "samples_per_sec"):
        assert key in text, (
            f"setup_prompt does not name required default plot {key!r}")
