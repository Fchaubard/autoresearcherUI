"""Unit tests for backend.app.watchdog (PR 4 of state-control rewrite,
2026-06-05).

The watchdog is the non-LLM monitoring harness that scans every RUNNING
run against a registry of "scripts" and pages the agent via tmux when
something fires. These tests pin:

  * each ship-default script's check() against synthetic runs / metrics;
  * the runner's per-(run_id, code) de-dup ledger;
  * the per-project watchdog.config merge logic;
  * the on_fire policy (kill_run / page_agent / page_message).
"""
from __future__ import annotations

import datetime as dt
import math
import types
import pytest


def _iso(seconds_ago: float = 0) -> str:
    return (dt.datetime.now(dt.timezone.utc)
            - dt.timedelta(seconds=seconds_ago)).isoformat()


# ────────────────────────── config tests ──────────────────────────────


def test_default_config_lists_all_scripts(arui_env):
    from backend.app import watchdog as wd
    cfg = wd.get_config()
    assert "no_metric_flow" in cfg
    assert "nan_loss" in cfg
    assert "diverging" in cfg
    assert "gpu_oom" in cfg
    assert "crashed_silently" in cfg
    assert "done_signal" in cfg


def test_set_config_overrides_only_named_scripts(arui_env):
    """Overriding one script must NOT clobber the rest."""
    from backend.app import watchdog as wd
    wd.set_config({"no_metric_flow": {"params": {"timeout_sec": 7200}}})
    cfg = wd.get_config()
    assert cfg["no_metric_flow"]["params"]["timeout_sec"] == 7200
    # Other scripts keep their defaults
    assert cfg["nan_loss"]["enabled"] is True
    assert cfg["diverging"]["params"]["window_steps"] == 200


def test_set_config_can_disable_a_script(arui_env):
    from backend.app import watchdog as wd
    wd.set_config({"diverging": {"enabled": False}})
    assert wd.get_config()["diverging"]["enabled"] is False


def test_list_scripts_exposes_describe_for_onboarding(arui_env):
    """The onboarding modal renders one row per script with its
    describe() string and default_params dict."""
    from backend.app import watchdog as wd
    rows = wd.list_scripts()
    assert isinstance(rows, list) and len(rows) >= 6
    for r in rows:
        assert r["name"]
        assert r["describe"]
        assert isinstance(r["default_params"], dict)


# ───────────────────────── script tests ───────────────────────────────


def test_no_metric_flow_silent_for_fresh_run(arui_env):
    from backend.app.watchdog.scripts import no_metric_flow as nmf
    run = types.SimpleNamespace(
        id="r1", run_name="fresh", started_at=_iso(seconds_ago=30),
        created_at=_iso(seconds_ago=30))
    fake = types.SimpleNamespace(last_activity=lambda _id: None)
    assert nmf.check(run, fake, nmf.DEFAULT_PARAMS) is None


def test_no_metric_flow_fires_after_timeout(arui_env):
    from backend.app.watchdog.scripts import no_metric_flow as nmf
    run = types.SimpleNamespace(
        id="r2", run_name="slow", started_at=_iso(seconds_ago=2000),
        created_at=_iso(seconds_ago=2000))
    fake = types.SimpleNamespace(last_activity=lambda _id: None)
    issue = nmf.check(run, fake, nmf.DEFAULT_PARAMS)
    assert issue is not None
    assert issue.code == "no_metric_flow"
    assert "slow" in issue.summary


def test_nan_loss_fires_on_nan_metric(arui_env):
    from backend.app.watchdog.scripts import nan_loss
    run = types.SimpleNamespace(
        id="r3", run_name="diverger", started_at=_iso(seconds_ago=120),
        created_at=_iso(seconds_ago=120))
    fake = types.SimpleNamespace(query=lambda rid, keys: {
        "train_loss": [[1, 0.5], [2, 1.0], [3, float("nan")]],
    })
    issue = nan_loss.check(run, fake, nan_loss.DEFAULT_PARAMS)
    assert issue is not None
    assert issue.code == "nan_loss"
    assert issue.evidence["key"] == "train_loss"


def test_nan_loss_silent_on_clean_metrics(arui_env):
    from backend.app.watchdog.scripts import nan_loss
    run = types.SimpleNamespace(
        id="r4", run_name="clean", started_at=_iso(seconds_ago=120),
        created_at=_iso(seconds_ago=120))
    fake = types.SimpleNamespace(query=lambda rid, keys: {
        "train_loss": [[1, 0.5], [2, 0.4], [3, 0.3]],
    })
    assert nan_loss.check(run, fake, nan_loss.DEFAULT_PARAMS) is None


def test_diverging_fires_on_climbing_loss(arui_env):
    from backend.app.watchdog.scripts import diverging
    run = types.SimpleNamespace(
        id="r5", run_name="climber", started_at=_iso(seconds_ago=120),
        created_at=_iso(seconds_ago=120))
    pts = [[i, 1.0 + 0.05 * i] for i in range(300)]   # 1.0 → ~16
    fake = types.SimpleNamespace(query=lambda rid, keys: {
        "train_loss": pts,
    })
    issue = diverging.check(run, fake, diverging.DEFAULT_PARAMS)
    assert issue is not None
    assert issue.code == "diverging"
    assert issue.evidence["ratio"] > 1.5


def test_diverging_silent_on_decreasing_loss(arui_env):
    from backend.app.watchdog.scripts import diverging
    run = types.SimpleNamespace(
        id="r6", run_name="healthy", started_at=_iso(seconds_ago=120),
        created_at=_iso(seconds_ago=120))
    pts = [[i, max(0.01, 1.0 - 0.002 * i)] for i in range(300)]
    fake = types.SimpleNamespace(query=lambda rid, keys: {
        "train_loss": pts,
    })
    assert diverging.check(run, fake, diverging.DEFAULT_PARAMS) is None


# ─────────────────────────── runner tests ─────────────────────────────


@pytest.fixture
def stub_tmux(monkeypatch):
    """Prevent the runner from actually invoking tmux during tests."""
    import subprocess
    calls = []
    def fake_run(cmd, *a, **kw):
        calls.append({"cmd": cmd, "kwargs": kw})
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_runner_dedups_per_run_per_code(arui_env, make_project, make_run,
                                          stub_tmux, monkeypatch):
    """Two run_once() calls in a row must not page or emit twice for
    the same (run_id, code) pair."""
    from backend.app.watchdog import runner
    runner.reset_ledger()
    make_project()
    make_run(id="rA", run_name="staler",
             status="running",
             started_at=_iso(seconds_ago=2000),
             created_at=_iso(seconds_ago=2000))
    fired1 = runner.run_once()
    fired2 = runner.run_once()
    nmf1 = [f for f in fired1 if f["script"] == "no_metric_flow"]
    nmf2 = [f for f in fired2 if f["script"] == "no_metric_flow"]
    assert len(nmf1) == 1
    assert len(nmf2) == 0


def test_runner_dry_run_does_not_record_in_ledger(
        arui_env, make_project, make_run, stub_tmux):
    """dry_run mode lets the unit tests verify what WOULD fire without
    touching state or tmux."""
    from backend.app.watchdog import runner
    runner.reset_ledger()
    make_project()
    make_run(id="rB", run_name="dry", status="running",
             started_at=_iso(seconds_ago=2000),
             created_at=_iso(seconds_ago=2000))
    fired_dry = runner.run_once(dry_run=True)
    fired_real = runner.run_once()        # ledger empty → fires now
    assert len([f for f in fired_dry if f["script"] == "no_metric_flow"]) == 1
    assert len([f for f in fired_real if f["script"] == "no_metric_flow"]) == 1


def test_runner_skips_disabled_scripts(arui_env, make_project, make_run,
                                          stub_tmux):
    from backend.app import watchdog as wd
    from backend.app.watchdog import runner
    runner.reset_ledger()
    wd.set_config({"no_metric_flow": {"enabled": False}})
    make_project()
    make_run(id="rC", run_name="silent", status="running",
             started_at=_iso(seconds_ago=2000),
             created_at=_iso(seconds_ago=2000))
    fired = runner.run_once()
    assert all(f["script"] != "no_metric_flow" for f in fired)
