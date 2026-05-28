"""Unit tests for backend.app.paper_runner."""
from __future__ import annotations


def test_queue_run_creates_row(arui_env, db_session):
    from backend.app import paper_runner
    from backend.app.models import Run
    rid = paper_runner.queue_run(claim_id="c1", role="ablation",
                                   cmd="echo hi", n_seeds=2)
    r = db_session.query(Run).filter(Run.id == rid).first()
    assert r is not None
    assert r.context == "paper"
    assert r.paper_claim_id == "c1"
    assert r.status == "queued"
    assert r.n_seeds == 2
    assert r.config.get("cmd") == "echo hi"


def test_update_run_status_kept_sets_stale_integration(arui_env, db_session,
                                                          make_project,
                                                          make_run):
    from backend.app import paper_runner
    make_project()
    make_run(id="pr1", context="paper", status="running",
             integration_status="pending")
    assert paper_runner.update_run_status("pr1", "kept",
                                            headline_metric=0.5) is True
    db_session.expire_all()
    from backend.app.models import Run
    r = db_session.query(Run).filter(Run.id == "pr1").first()
    assert r.status == "kept"
    assert r.integration_status == "stale"
    assert r.headline_metric == 0.5
    assert r.ended_at


def test_update_run_status_rejects_non_paper(arui_env, db_session,
                                                make_project, make_run):
    from backend.app import paper_runner
    make_project()
    make_run(id="r1", context="research", status="running")
    assert paper_runner.update_run_status("r1", "kept") is False


def test_tick_assigns_ready_run_to_idle_gpu(arui_env, db_session,
                                              make_project, make_run,
                                              fake_subprocess):
    """A queued paper run with all deps satisfied gets a free GPU."""
    from backend.app import paper_runner, paper
    from backend.app.models import Gpu, Run
    make_project()
    # paper mode required for paper_folder to resolve
    paper.set_project_mode("paper")
    # Two GPUs, both idle
    db_session.add(Gpu(index=0, util_pct=1.0, vram_used_mb=100))
    db_session.add(Gpu(index=1, util_pct=2.0, vram_used_mb=50))
    # Two paper runs ready to go
    make_run(id="pr1", context="paper", status="queued",
             config={"cmd": "python train.py"})
    make_run(id="pr2", context="paper", status="queued",
             config={"cmd": "python train.py"})
    db_session.commit()
    paper_runner._tick()
    db_session.expire_all()
    runs = db_session.query(Run).all()
    statuses = {r.id: r.status for r in runs}
    assert statuses["pr1"] == "running"
    assert statuses["pr2"] == "running"
    # tmux new-session must have been called
    assert any("tmux" in c["args"][0] and "new-session" in c["args"]
               for c in fake_subprocess)


def test_tick_respects_busy_gpu(arui_env, db_session, make_project, make_run):
    from backend.app import paper_runner, paper
    from backend.app.models import Gpu, Run
    make_project()
    paper.set_project_mode("paper")
    db_session.add(Gpu(index=0, util_pct=80.0, vram_used_mb=20000))
    db_session.commit()
    make_run(id="pr1", context="paper", status="queued",
             config={"cmd": "python train.py"})
    paper_runner._tick()
    db_session.expire_all()
    r = db_session.query(Run).filter(Run.id == "pr1").first()
    # GPU busy → stays queued
    assert r.status == "queued"


def test_tick_respects_depends_on(arui_env, db_session, make_project,
                                     make_run):
    from backend.app import paper_runner, paper
    from backend.app.models import Gpu, Run
    make_project()
    paper.set_project_mode("paper")
    db_session.add(Gpu(index=0, util_pct=1.0, vram_used_mb=50))
    make_run(id="pr1", context="paper", status="queued",
             depends_on=["pr0"], config={"cmd": "python a"})
    db_session.commit()
    paper_runner._tick()
    db_session.expire_all()
    r = db_session.query(Run).filter(Run.id == "pr1").first()
    # pr0 not completed → pr1 stays queued
    assert r.status == "queued"


def test_tick_runs_when_dep_done(arui_env, db_session, make_project,
                                    make_run, fake_subprocess):
    from backend.app import paper_runner, paper
    from backend.app.models import Gpu, Run
    make_project()
    paper.set_project_mode("paper")
    db_session.add(Gpu(index=0, util_pct=1.0, vram_used_mb=50))
    make_run(id="pr0", context="paper", status="kept")
    make_run(id="pr1", context="paper", status="queued",
             depends_on=["pr0"], config={"cmd": "python b"})
    db_session.commit()
    paper_runner._tick()
    db_session.expire_all()
    r = db_session.query(Run).filter(Run.id == "pr1").first()
    assert r.status == "running"


def test_launch_run_no_cmd_marks_failed(arui_env, db_session, make_project,
                                          make_run, fake_subprocess):
    from backend.app import paper_runner, paper
    from backend.app.models import Gpu, Run
    make_project()
    paper.set_project_mode("paper")
    db_session.add(Gpu(index=0, util_pct=1.0, vram_used_mb=50))
    make_run(id="pr1", context="paper", status="queued", config={})
    db_session.commit()
    paper_runner._tick()
    db_session.expire_all()
    r = db_session.query(Run).filter(Run.id == "pr1").first()
    assert r.status == "failed"


def test_tick_picks_only_paper_runs(arui_env, db_session, make_project,
                                       make_run, fake_subprocess):
    """A research-context queued run is not picked up by Paper Runner."""
    from backend.app import paper_runner, paper
    from backend.app.models import Gpu, Run
    make_project()
    paper.set_project_mode("paper")
    db_session.add(Gpu(index=0, util_pct=1.0, vram_used_mb=50))
    make_run(id="r1", context="research", status="queued",
             config={"cmd": "x"})
    make_run(id="pr1", context="paper", status="queued",
             config={"cmd": "y"})
    db_session.commit()
    paper_runner._tick()
    db_session.expire_all()
    res = db_session.query(Run).filter(Run.id == "r1").first()
    pap = db_session.query(Run).filter(Run.id == "pr1").first()
    assert res.status == "queued"
    assert pap.status == "running"
