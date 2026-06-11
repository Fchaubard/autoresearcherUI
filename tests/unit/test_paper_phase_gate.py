"""Enumeration is a HARD step: the author cannot advance into run_ablations /
reviewer_simulator / submission_ready while figures exist but no run is tagged
to any of them. (The PI is for errors/timeouts, not for core steps.)

NOTE: imports happen INSIDE each test, after arui_env re-imports the backend.
"""


def _add_figure(db, fid="pf-1"):
    from backend.app.models import PaperFigure
    db.add(PaperFigure(id=fid, title="Figure 1"))
    db.commit()


def _add_tagged_run(db, fid="pf-1", rid="pr-1"):
    from backend.app.models import Run
    db.add(Run(id=rid, run_name="r", status="queued", context="paper",
               paper_figure_id=fid))
    db.commit()


def test_blocks_run_ablations_with_figures_but_no_runs(arui_env, db_session):
    from backend.app import paper_phase as pp
    _add_figure(db_session)
    r = pp.set_phase("paper.run_ablations")
    assert r.get("blocked") is True and r.get("ok") is False
    assert pp.get_phase().get("phase") != "paper.run_ablations"


def test_blocks_reviewer_and_submission_too(arui_env, db_session):
    from backend.app import paper_phase as pp
    _add_figure(db_session)
    assert pp.set_phase("paper.reviewer_simulator").get("blocked") is True
    assert pp.set_phase("paper.submission_ready").get("blocked") is True


def test_allows_once_runs_are_tagged(arui_env, db_session):
    from backend.app import paper_phase as pp
    _add_figure(db_session)
    _add_tagged_run(db_session)
    r = pp.set_phase("paper.run_ablations")
    assert r.get("ok") is True and not r.get("blocked")


def test_not_gated_when_no_figures(arui_env, db_session):
    from backend.app import paper_phase as pp
    assert pp.set_phase("paper.run_ablations").get("ok") is True


def test_earlier_phases_never_gated(arui_env, db_session):
    from backend.app import paper_phase as pp
    _add_figure(db_session)            # figures but no runs
    for ph in ("paper.whittle_claims", "paper.lit_review", "paper.draft_v0",
               "paper.plan_ablations", "paper.build_gantt"):
        assert pp.set_phase(ph).get("ok") is True
