"""Paper-mode run counts must be consistent + paper-scoped.

Regression for the "numbers don't look right" bug: the status pill read 143
done (all-mode kept_novel leaked in via paper_claim_id) while the gantt showed
~74, the Today card read 0 (filtered on the dead 'kept'/'success' names), and
the email reported crashes as "done". The single source of truth is
paper_phase._derive_progress_from_db.
"""
from __future__ import annotations


def test_derive_progress_is_paper_scoped_and_taxonomy_correct(
        db_session, make_project, make_run):
    from backend.app import paper_phase
    make_project()
    # paper ablations
    make_run(context="paper", status="kept_novel", paper_claim_id="c1")
    make_run(context="paper", status="kept_novel", paper_claim_id="c1")
    make_run(context="paper", status="kept_replicate", paper_claim_id="c1")
    make_run(context="paper", status="crashed")
    make_run(context="paper", status="crashed")
    make_run(context="paper", status="queued")
    make_run(context="paper", status="running")
    make_run(context="paper", status="discarded")
    # research-mode runs that carry a claim id MUST NOT leak into paper counts
    make_run(context="research", status="kept_novel", paper_claim_id="c1")
    make_run(context="research", status="kept_novel", paper_claim_id="c2")

    abl = paper_phase._derive_progress_from_db(db_session)["ablations"]
    assert abl["kept"] == 3           # 2 kept_novel + 1 kept_replicate (paper)
    assert abl["crashed"] == 2        # crashes tracked separately
    assert abl["done"] == 4           # kept(3) + discarded(1); NOT the crashes
    assert abl["running"] == 1
    assert abl["queued"] == 1
    # the two research kept_novel runs are excluded entirely
    assert abl["kept"] != 5


def test_derive_progress_zero_when_no_paper_runs(db_session, make_project,
                                                 make_run):
    from backend.app import paper_phase
    make_project()
    make_run(context="research", status="kept_novel")
    abl = paper_phase._derive_progress_from_db(db_session)["ablations"]
    assert abl["done"] == 0 and abl["kept"] == 0 and abl["crashed"] == 0
