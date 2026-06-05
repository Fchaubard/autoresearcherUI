"""Unit tests for backend.app.paper — paper-mode core helpers."""
from __future__ import annotations

import datetime as dt


def test_project_mode_default_research(arui_env):
    from backend.app import paper
    assert paper.project_mode() == "research"


def test_set_project_mode_roundtrip(arui_env):
    from backend.app import paper
    paper.set_project_mode("paper")
    assert paper.project_mode() == "paper"
    paper.set_project_mode("research")
    assert paper.project_mode() == "research"


def test_set_project_mode_rejects_bad_value(arui_env):
    from backend.app import paper
    import pytest
    with pytest.raises(ValueError):
        paper.set_project_mode("typo")


def test_paper_folder_creates_path(arui_env, make_project, setting_setter):
    from backend.app import paper
    make_project(name="myproj")
    setting_setter("onboarding", {"repo_name": "myproj"})
    p = paper.paper_folder()
    assert p is not None
    assert p.exists()
    assert p.name == "paper"


def test_paper_folder_no_project(arui_env):
    from backend.app import paper
    # No project row → None
    assert paper.paper_folder() is None


def test_file_decision_creates_pending_row(arui_env, db_session):
    from backend.app import paper
    from backend.app.models import PaperDecision
    did = paper.file_decision(
        source="agent", kind="cite_paper",
        title="cite SEDD?", body_md="why this matters",
        priority=3, linked_citation_key="lou2024sedd")
    row = db_session.query(PaperDecision).filter(
        PaperDecision.id == did).first()
    assert row is not None
    assert row.status == "pending"
    assert row.kind == "cite_paper"
    assert row.priority == 3


def test_file_decision_unknown_kind_still_files(arui_env, db_session):
    from backend.app import paper
    from backend.app.models import PaperDecision
    did = paper.file_decision(
        source="agent", kind="weird_unknown_kind",
        title="something", body_md="x")
    row = db_session.query(PaperDecision).filter(
        PaperDecision.id == did).first()
    assert row is not None
    assert row.kind == "weird_unknown_kind"


def test_resolve_decision_approve_cite_marks_citation(arui_env, db_session):
    """Approving a cite_paper decision should stamp user_approved_at on
    the linked PaperCitation."""
    from backend.app import paper
    from backend.app.models import PaperCitation, PaperDecision
    db_session.add(PaperCitation(key="lou2024sedd", title="SEDD",
                                   year="2024"))
    db_session.commit()
    did = paper.file_decision(
        source="lit", kind="cite_paper", title="cite SEDD?",
        linked_citation_key="lou2024sedd")
    assert paper.resolve_decision(did, "approve") is True
    cit = db_session.query(PaperCitation).filter(
        PaperCitation.key == "lou2024sedd").first()
    assert cit.user_approved_at
    d = db_session.query(PaperDecision).filter(
        PaperDecision.id == did).first()
    assert d.status == "approved"


def test_resolve_decision_kill_claim_marks_killed(arui_env, db_session):
    from backend.app import paper
    from backend.app.models import PaperClaim
    db_session.add(PaperClaim(id="c1", title="claim",
                                status="active"))
    db_session.commit()
    did = paper.file_decision(
        source="agent", kind="kill_claim",
        title="kill", linked_claim_id="c1")
    paper.resolve_decision(did, "approve", note="weak evidence")
    c = db_session.query(PaperClaim).filter(
        PaperClaim.id == "c1").first()
    assert c.status == "killed"
    assert "weak evidence" in (c.killed_reason or "")


def test_resolve_decision_reject_doesnt_side_effect(arui_env, db_session):
    from backend.app import paper
    from backend.app.models import PaperClaim, PaperDecision
    db_session.add(PaperClaim(id="c1", title="claim", status="active"))
    db_session.commit()
    did = paper.file_decision(
        source="agent", kind="kill_claim",
        title="kill", linked_claim_id="c1")
    paper.resolve_decision(did, "reject")
    c = db_session.query(PaperClaim).filter(
        PaperClaim.id == "c1").first()
    assert c.status == "active"
    d = db_session.query(PaperDecision).filter(
        PaperDecision.id == did).first()
    assert d.status == "rejected"


def test_resolve_decision_invalid_action(arui_env, db_session):
    from backend.app import paper
    did = paper.file_decision(source="agent", kind="cite_paper",
                                title="x")
    assert paper.resolve_decision(did, "neither") is False


def test_resolve_decision_unknown_id(arui_env):
    from backend.app import paper
    assert paper.resolve_decision("nope", "approve") is False


def test_populate_claims_from_proposal(arui_env, db_session):
    """Council claims get converted into PaperClaim rows."""
    from backend.app import paper
    from backend.app.models import PaperClaim, PaperProposal
    db_session.add(PaperProposal(
        id="pp1", status="ready",
        council_responses={
            "gemini": {
                "novelty": "high",
                "rationale_md": "ok",
                "claims": [
                    {"title": "Ensembles help diffusion",
                     "summary": "they really do",
                     "evidence_strength": "suggestive"},
                    {"title": "Bigger batches diverge less",
                     "summary": "indeed",
                     "evidence_strength": "anecdotal"},
                ],
            },
            "openai": {
                "novelty": "medium",
                "claims": [
                    {"title": "Ensembles help diffusion",
                     "evidence_strength": "strong"},
                ],
            },
        }))
    db_session.commit()
    n = paper.populate_claims_from_proposal()
    assert n == 2
    rows = db_session.query(PaperClaim).order_by(PaperClaim.idx).all()
    titles = {r.title for r in rows}
    assert "Ensembles help diffusion" in titles
    # the strongest evidence wins via merge
    ens = next(r for r in rows if r.title.startswith("Ensembles"))
    assert ens.evidence_strength == "strong"
    # Provenance carries both reviewers
    assert "gemini" in (ens.council_provenance or "")
    assert "openai" in (ens.council_provenance or "")


def test_populate_claims_idempotent(arui_env, db_session):
    from backend.app import paper
    from backend.app.models import PaperClaim, PaperProposal
    db_session.add(PaperProposal(
        id="pp1", status="ready",
        council_responses={
            "gemini": {"claims": [{"title": "Some clear claim about X",
                                    "evidence_strength": "suggestive"}]},
        }))
    db_session.commit()
    n1 = paper.populate_claims_from_proposal()
    n2 = paper.populate_claims_from_proposal()
    assert n1 == 1
    assert n2 == 0
    assert db_session.query(PaperClaim).count() == 1


def test_populate_claims_no_ready_proposal(arui_env):
    from backend.app import paper
    # No proposal at all → 0
    assert paper.populate_claims_from_proposal() == 0


def test_populate_claims_picks_accepted_proposal(arui_env, db_session):
    """When the user re-accepts a previously-dismissed proposal from the
    history table, /paper/enter flips it to 'accepted' and calls
    populate_claims_from_proposal(). That helper must consider both
    'ready' AND 'accepted' rows, otherwise re-accept silently no-ops."""
    from backend.app import paper
    from backend.app.models import PaperClaim, PaperProposal
    db_session.add(PaperProposal(
        id="pp-accepted", status="accepted",
        accepted_at="2026-01-01T00:00:00+00:00",
        council_responses={
            "gemini": {"claims": [
                {"title": "An accepted-status claim worth importing",
                 "evidence_strength": "strong"}]}}))
    db_session.commit()
    n = paper.populate_claims_from_proposal()
    assert n == 1
    titles = [c.title for c in db_session.query(PaperClaim).all()]
    assert "An accepted-status claim worth importing" in titles


def test_render_claims_md(arui_env, db_session):
    from backend.app import paper
    from backend.app.models import PaperClaim
    db_session.add(PaperClaim(id="c1", idx=0, title="claim A",
                                status="active",
                                evidence_strength="strong"))
    db_session.add(PaperClaim(id="c2", idx=1, title="claim B",
                                status="killed",
                                evidence_strength="anecdotal", ready=True))
    db_session.commit()
    md = paper.render_claims_md(db_session)
    assert md.startswith("# Claims")
    assert "claim A" in md
    assert "claim B" in md
    assert "killed" in md
    assert "★" in md


def test_render_runs_md(arui_env, db_session, make_project, make_run):
    from backend.app import paper
    make_project()
    make_run(id="pr1", context="paper", run_name="pr1", paper_role="headline",
             config={"dataset": "cifar", "model": "resnet"},
             est_time_sec=60, status="queued")
    md = paper.render_runs_md(db_session)
    assert "# Paper runs" in md
    assert "pr1" in md
    assert "cifar" in md
    assert "headline" in md


def test_render_figures_md(arui_env, db_session):
    from backend.app import paper
    from backend.app.models import PaperFigure
    db_session.add(PaperFigure(id="pf1", claim_id="c1", kind="line",
                                 title="Loss vs step",
                                 status="planned"))
    db_session.commit()
    md = paper.render_figures_md(db_session)
    assert "Loss vs step" in md


def test_take_snapshot_shape(arui_env, db_session, make_project, make_run):
    from backend.app import paper
    from backend.app.models import PaperClaim, PaperDecision, PaperFigure
    make_project()
    db_session.add(PaperClaim(id="c1", title="x"))
    db_session.add(PaperFigure(id="f1", title="fig"))
    db_session.add(PaperDecision(id="d1", source="agent", kind="cite_paper",
                                   title="t", status="pending"))
    make_run(id="pr1", context="paper", status="queued")
    db_session.commit()
    snap = paper.take_snapshot()
    assert "at" in snap
    assert any(c["id"] == "c1" for c in snap["claims"])
    assert any(f["id"] == "f1" for f in snap["figures"])
    assert any(d["id"] == "d1" for d in snap["decisions_open"])
    assert "pr1" in snap["run_ids_paper"]


def test_log_budget_event_persists(arui_env, db_session):
    from backend.app import paper
    from backend.app.models import PaperBudgetEvent
    paper.log_budget_event("gpu", "ablation", cost_units=1.5,
                            cost_usd=0.25, run_id="pr1")
    row = db_session.query(PaperBudgetEvent).first()
    assert row.kind == "gpu"
    assert row.cost_units == 1.5


def test_budget_summary_aggregates(arui_env, db_session):
    from backend.app import paper
    paper.log_budget_event("gpu", "ablation", cost_units=1.0)
    paper.log_budget_event("gpu", "ablation", cost_units=2.5)
    paper.log_budget_event("llm", "author_agent", cost_units=100,
                            cost_usd=0.04)
    s = paper.budget_summary()
    assert s["gpu_hours_used"] == 3.5
    assert s["llm_usd_today"] == 0.04


def test_days_till_deadline_none_when_no_meta(arui_env):
    from backend.app import paper
    assert paper.days_till_deadline() is None


def test_days_till_deadline_basic(arui_env, db_session):
    from backend.app import paper
    from backend.app.models import PaperMeta
    future = (dt.datetime.now(dt.timezone.utc)
              + dt.timedelta(days=5)).isoformat()
    db_session.add(PaperMeta(id="pm1", deadline_iso=future))
    db_session.commit()
    d = paper.days_till_deadline()
    assert d is not None
    # rounded to 2dp; should be ~5
    assert 4.5 < d < 5.5


def test_days_till_deadline_negative_past(arui_env, db_session):
    from backend.app import paper
    from backend.app.models import PaperMeta
    past = (dt.datetime.now(dt.timezone.utc)
            - dt.timedelta(days=2)).isoformat()
    db_session.add(PaperMeta(id="pm1", deadline_iso=past))
    db_session.commit()
    d = paper.days_till_deadline()
    assert d is not None and d < 0


def test_list_commits_empty_when_no_git(arui_env, make_project,
                                          setting_setter):
    from backend.app import paper
    make_project(name="myproj")
    setting_setter("onboarding", {"repo_name": "myproj"})
    folder = paper.paper_folder()
    # No .git in folder → []
    assert paper.list_commits(folder) == []
