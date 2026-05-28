"""Unit tests for backend.app.maintenance."""
from __future__ import annotations

import datetime as dt


def _iso(days_ago: float) -> str:
    return (dt.datetime.now(dt.timezone.utc)
            - dt.timedelta(days=days_ago)).isoformat()


def test_candidate_paths_includes_log(arui_env):
    from backend.app.maintenance import _candidate_paths
    from backend.app.models import Run
    from backend.app.config import DATA_DIR
    r = Run(id="r1", run_name="r1")
    paths = _candidate_paths(r, "myrepo")
    assert any(str(p).endswith("r1.log") for p in paths)
    assert any(str(DATA_DIR) in str(p) for p in paths)


def test_candidate_paths_includes_checkpoints(arui_env):
    from backend.app.maintenance import _candidate_paths
    from backend.app.models import Run
    from backend.app.config import DATA_DIR
    repo = "myrepo"
    ckpts = DATA_DIR / "workspace" / repo / "ckpts"
    ckpts.mkdir(parents=True, exist_ok=True)
    (ckpts / "myrun_seed1.pt").write_text("x")
    (ckpts / "myrun_ema.pt").write_text("x")
    (ckpts / "myrun.bin").write_text("x")
    r = Run(id="myrun-id", run_name="myrun")
    paths = _candidate_paths(r, repo)
    path_strs = [str(p) for p in paths]
    assert any("myrun.pt" in s for s in path_strs)
    assert any("myrun_seed1.pt" in s for s in path_strs)
    assert any("myrun_ema.pt" in s for s in path_strs)
    assert any("myrun.bin" in s for s in path_strs)


def test_candidate_paths_no_repo(arui_env):
    """If repo or name is missing, only the run-logs path is returned."""
    from backend.app.maintenance import _candidate_paths
    from backend.app.models import Run
    r = Run(id="r1", run_name="")
    paths = _candidate_paths(r, "")
    # only the run_logs entry
    assert len(paths) == 1
    assert str(paths[0]).endswith("r1.log")


def test_eligible_runs_filters_by_age(arui_env, db_session, make_project,
                                       make_run):
    """Recent runs (newer than min_age_days) are excluded."""
    from backend.app.maintenance import _eligible_runs
    make_project(metric_direction="minimize")
    make_run(id="recent", status="kept", headline_metric=1.0,
             ended_at=_iso(0.5))
    make_run(id="old", status="kept", headline_metric=2.0,
             ended_at=_iso(5))
    eligible = _eligible_runs(db_session, min_age_days=2.0,
                               bottom_pct=0.5)
    ids = {r.id for r in eligible}
    assert "old" in ids
    assert "recent" not in ids


def test_eligible_runs_skips_running_and_queued(arui_env, db_session,
                                                  make_project, make_run):
    from backend.app.maintenance import _eligible_runs
    make_project(metric_direction="minimize")
    make_run(id="running", status="running", ended_at=_iso(5),
             headline_metric=1.0)
    make_run(id="queued", status="queued", ended_at=_iso(5),
             headline_metric=1.0)
    make_run(id="kept", status="kept", ended_at=_iso(5),
             headline_metric=1.0)
    eligible = _eligible_runs(db_session, 1.0, 1.0)
    ids = {r.id for r in eligible}
    assert "running" not in ids
    assert "queued" not in ids


def test_eligible_runs_skips_baselines(arui_env, db_session, make_project,
                                        make_run):
    from backend.app.maintenance import _eligible_runs
    make_project(metric_direction="minimize")
    make_run(id="base", status="kept", is_baseline=True,
             ended_at=_iso(5), headline_metric=999.0)
    make_run(id="r1", status="kept", headline_metric=1.0,
             ended_at=_iso(5))
    eligible = _eligible_runs(db_session, 1.0, 1.0)
    ids = {r.id for r in eligible}
    assert "base" not in ids


def test_eligible_runs_protects_global_best_minimize(arui_env, db_session,
                                                       make_project, make_run):
    """Even if old, the global-best run is never eligible."""
    from backend.app.maintenance import _eligible_runs
    make_project(metric_direction="minimize")
    # Three runs, all old, with metrics 0.1, 0.5, 0.9
    make_run(id="best", status="kept", headline_metric=0.1,
             ended_at=_iso(5))
    make_run(id="mid", status="kept", headline_metric=0.5,
             ended_at=_iso(5))
    make_run(id="worst", status="kept", headline_metric=0.9,
             ended_at=_iso(5))
    eligible = _eligible_runs(db_session, 1.0, 1.0)
    ids = {r.id for r in eligible}
    assert "best" not in ids
    assert "worst" in ids


def test_eligible_runs_protects_global_best_maximize(arui_env, db_session,
                                                       make_project, make_run):
    from backend.app.maintenance import _eligible_runs
    make_project(metric_direction="maximize")
    make_run(id="best", status="kept", headline_metric=0.9,
             ended_at=_iso(5))
    make_run(id="worst", status="kept", headline_metric=0.1,
             ended_at=_iso(5))
    eligible = _eligible_runs(db_session, 1.0, 1.0)
    ids = {r.id for r in eligible}
    assert "best" not in ids
    assert "worst" in ids


def test_eligible_runs_bottom_pct_math(arui_env, db_session, make_project,
                                         make_run):
    """bottom_pct=0.5 with minimize keeps the LARGER values eligible."""
    from backend.app.maintenance import _eligible_runs
    make_project(metric_direction="minimize")
    for i, m in enumerate([0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]):
        make_run(id=f"r{i}", status="kept", headline_metric=m,
                 ended_at=_iso(5))
    eligible = _eligible_runs(db_session, 1.0, 0.5)
    ids = {r.id for r in eligible}
    # global best (r0 = 0.1) is excluded; bottom-50% = bigger half
    assert "r0" not in ids
    # at least the worst few should be eligible
    assert "r9" in ids
    assert "r8" in ids


def test_eligible_runs_includes_crashes_when_no_metrics(arui_env, db_session,
                                                         make_project, make_run):
    """If no completed runs have metrics, everything in window is eligible."""
    from backend.app.maintenance import _eligible_runs
    make_project()
    make_run(id="r1", status="crashed", headline_metric=None,
             ended_at=_iso(5))
    make_run(id="r2", status="discarded", headline_metric=None,
             ended_at=_iso(5))
    eligible = _eligible_runs(db_session, 1.0, 0.5)
    ids = {r.id for r in eligible}
    assert ids == {"r1", "r2"}


def test_preview_returns_size_and_count(arui_env, db_session, make_project,
                                          make_run):
    from backend.app import maintenance
    from backend.app.config import DATA_DIR
    make_project(metric_direction="minimize")
    # Need at least two runs so the global-best protection doesn't shield
    # the single eligible run from being purged.
    make_run(id="best", run_name="best", status="kept",
             headline_metric=0.1, ended_at=_iso(5))
    make_run(id="r1", run_name="r1", status="kept", headline_metric=2.0,
             ended_at=_iso(5))
    logs = DATA_DIR / "run_logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "r1.log").write_text("a" * 100)
    out = maintenance.preview(min_age_days=1.0, bottom_pct=1.0)
    assert out["eligible"] >= 1
    assert out["bytes_freeable"] >= 100


def test_purge_old_run_logs_deletes_files(arui_env, db_session, make_project,
                                            make_run):
    from backend.app import maintenance
    from backend.app.config import DATA_DIR
    make_project(metric_direction="minimize")
    make_run(id="r1", run_name="r1", status="kept", headline_metric=2.0,
             ended_at=_iso(5))
    make_run(id="r2", run_name="r2", status="kept", headline_metric=0.5,
             ended_at=_iso(5))
    logs = DATA_DIR / "run_logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "r1.log").write_text("worst")
    (logs / "r2.log").write_text("best")
    out = maintenance.purge_old_run_logs(min_age_days=1.0, bottom_pct=1.0)
    # r2 is global best, never deleted; r1 worse → should go
    assert not (logs / "r1.log").exists()
    assert (logs / "r2.log").exists()
    assert out["bytes_freed"] >= len("worst")


def test_sota_run_ids_picks_best_and_baselines(arui_env, db_session,
                                                  make_project, make_run):
    from backend.app.maintenance import _sota_run_ids
    make_project(metric_direction="maximize")
    make_run(id="best", status="kept", headline_metric=0.95,
             ended_at=_iso(2))
    make_run(id="mid", status="kept", headline_metric=0.50,
             ended_at=_iso(2))
    make_run(id="base", status="kept", is_baseline=True,
             headline_metric=0.30, ended_at=_iso(2))
    ids = _sota_run_ids(db_session)
    assert "best" in ids
    assert "base" in ids
    assert "mid" not in ids


def test_sota_includes_running_runs(arui_env, db_session, make_project,
                                       make_run):
    """In-flight runs are always kept, regardless of metric."""
    from backend.app.maintenance import _sota_run_ids
    make_project(metric_direction="minimize")
    make_run(id="best", status="kept", headline_metric=0.01,
             ended_at=_iso(1))
    make_run(id="live", status="running", started_at=_iso(0.1))
    ids = _sota_run_ids(db_session)
    assert "live" in ids


def test_purge_keep_sota_only_deletes_non_sota(arui_env, db_session,
                                                  make_project, make_run):
    from backend.app import maintenance
    from backend.app.config import DATA_DIR
    make_project(metric_direction="minimize")
    make_run(id="best", run_name="best", status="kept",
             headline_metric=0.1, ended_at=_iso(2))
    make_run(id="r1", run_name="r1", status="kept",
             headline_metric=0.5, ended_at=_iso(2))
    logs = DATA_DIR / "run_logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "best.log").write_text("keep me")
    (logs / "r1.log").write_text("delete me")
    out = maintenance.purge_keep_sota_only()
    assert (logs / "best.log").exists()
    assert not (logs / "r1.log").exists()
    assert "best" in out["kept_run_ids"]


def test_system_warnings_disk_critical(arui_env, monkeypatch):
    """Disk free < 2GB → critical warning."""
    from backend.app import maintenance

    def fake_stats():
        return {"disk": {"free_gb": 1.0, "percent": 99.0},
                "ram": {"percent": 50}, "gpus": []}

    from backend.app import monitor as _monitor
    monkeypatch.setattr(_monitor, "system_stats", fake_stats)
    warns = maintenance.system_warnings()
    severities = [w["severity"] for w in warns]
    assert "critical" in severities


def test_system_warnings_disk_low(arui_env, monkeypatch):
    """Disk free 2..10GB → warning."""
    from backend.app import maintenance

    def fake_stats():
        return {"disk": {"free_gb": 5.0, "percent": 50.0},
                "ram": {"percent": 50}, "gpus": []}

    from backend.app import monitor as _monitor
    monkeypatch.setattr(_monitor, "system_stats", fake_stats)
    warns = maintenance.system_warnings()
    assert any(w["severity"] == "warning" for w in warns)


def test_system_warnings_hot_gpu(arui_env, monkeypatch):
    from backend.app import maintenance

    def fake_stats():
        return {"disk": {"free_gb": 50, "percent": 20}, "ram": {"percent": 30},
                "gpus": [{"index": 0, "temp_c": 92, "util_pct": 80}]}

    from backend.app import monitor as _monitor
    monkeypatch.setattr(_monitor, "system_stats", fake_stats)
    warns = maintenance.system_warnings()
    assert any("hot" in w["msg"].lower() or "°c" in w["msg"].lower()
               for w in warns)


def test_system_warnings_healthy_returns_empty(arui_env, monkeypatch):
    from backend.app import maintenance

    def fake_stats():
        return {"disk": {"free_gb": 100, "percent": 40},
                "ram": {"percent": 30},
                "gpus": [{"index": 0, "temp_c": 50, "util_pct": 30}]}

    from backend.app import monitor as _monitor
    monkeypatch.setattr(_monitor, "system_stats", fake_stats)
    assert maintenance.system_warnings() == []
