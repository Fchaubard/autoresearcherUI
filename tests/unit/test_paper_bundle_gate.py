"""The paper bundle requires positive evidence plus automatic quality lints.
The reviewer simulator remains advisory rather than a human approval gate.
"""
import json


def _mk(tmp_path, tex):
    f = tmp_path / "paper"
    f.mkdir()
    (f / "main.tex").write_text(tex)
    return f


def _strong_claim(db):
    from backend.app.models import PaperClaim
    db.add(PaperClaim(id="pc-supported", title="Supported contribution",
                      status="active", evidence_strength="strong", ready=True))
    db.commit()


def test_bundle_blocked_by_prose_bib_and_reviewer(arui_env, tmp_path,
                                                  monkeypatch, db_session):
    from backend.app import paper, paper_compile
    f = _mk(tmp_path, "Intro with an em dash, like so, and a cite "
                      r"\cite{ghost2099}.".replace("em dash, like so",
                                                   "em—dash"))
    monkeypatch.setattr(paper, "paper_folder", lambda *a, **k: f)
    monkeypatch.setattr(paper_compile, "status",
                        lambda: {"ok": True, "pdf_exists": True})
    _strong_claim(db_session)
    gates = {b["gate"] for b in paper.bundle_blockers()}
    assert {"prose", "bib"} <= gates
    assert "compile" not in gates
    # reviewer_sim is NOT a gate under autopilot
    assert "reviewer_sim" not in gates


def test_compile_blocker_propagates(arui_env, tmp_path, monkeypatch,
                                    db_session):
    from backend.app import paper, paper_compile
    f = _mk(tmp_path, "A clean introduction about attack success rate.")
    monkeypatch.setattr(paper, "paper_folder", lambda *a, **k: f)
    monkeypatch.setattr(paper_compile, "status",
                        lambda: {"ok": False, "pdf_exists": True,
                                 "blockers": ["undefined references"]})
    _strong_claim(db_session)
    assert "compile" in {b["gate"] for b in paper.bundle_blockers()}


def test_clean_paper_has_no_blockers(arui_env, tmp_path, monkeypatch,
                                     db_session):
    from backend.app import paper, paper_compile
    f = _mk(tmp_path, "A clean introduction about attack success rate.")
    monkeypatch.setattr(paper, "paper_folder", lambda *a, **k: f)
    monkeypatch.setattr(paper_compile, "status",
                        lambda: {"ok": True, "pdf_exists": True})
    _strong_claim(db_session)
    # A supported claim plus a compile-clean, lint-clean paper clears the gate.
    assert paper.bundle_blockers() == []


def test_reviewer_sim_median_clears_bar(arui_env, tmp_path, monkeypatch,
                                        db_session):
    from backend.app import paper, paper_compile
    from backend.app.models import PaperReviewSim
    f = _mk(tmp_path, "A clean introduction about attack success rate.")
    monkeypatch.setattr(paper, "paper_folder", lambda *a, **k: f)
    monkeypatch.setattr(paper_compile, "status",
                        lambda: {"ok": True, "pdf_exists": True})
    db_session.add(PaperReviewSim(id="rs-1", model="gemini",
                                  content_md=json.dumps({"score": 7}),
                                  suggested_decisions_json=[]))
    db_session.add(PaperReviewSim(id="rs-2", model="openai",
                                  content_md=json.dumps({"score": 6}),
                                  suggested_decisions_json=[]))
    db_session.commit()
    _strong_claim(db_session)
    assert paper.reviewer_sim_median() == 6.5
    assert paper.bundle_blockers() == []           # all gates clear


def test_positive_evidence_gate_is_required_and_not_waivable(
        arui_env, tmp_path, monkeypatch):
    from backend.app import paper, paper_compile
    f = _mk(tmp_path, "A clean introduction about a provisional result.")
    monkeypatch.setattr(paper, "paper_folder", lambda *a, **k: f)
    monkeypatch.setattr(paper_compile, "status",
                        lambda: {"ok": True, "pdf_exists": True})
    blockers = paper.bundle_blockers(waive=["evidence"])
    assert "evidence" in {b["gate"] for b in blockers}
