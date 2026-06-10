"""The paper bundle is GATED: compile-clean + no em-dash/slop + complete
citations + a reviewer_sim median that clears the bar (or operator waiver).
"""
import json


def _mk(tmp_path, tex):
    f = tmp_path / "paper"
    f.mkdir()
    (f / "main.tex").write_text(tex)
    return f


def test_bundle_blocked_by_prose_bib_and_reviewer(arui_env, tmp_path,
                                                  monkeypatch):
    from backend.app import paper, paper_compile
    f = _mk(tmp_path, "Intro with an em dash, like so, and a cite "
                      r"\cite{ghost2099}.".replace("em dash, like so",
                                                   "em—dash"))
    monkeypatch.setattr(paper, "paper_folder", lambda *a, **k: f)
    monkeypatch.setattr(paper_compile, "status",
                        lambda: {"ok": True, "pdf_exists": True})
    gates = {b["gate"] for b in paper.bundle_blockers()}
    assert {"prose", "bib", "reviewer_sim"} <= gates
    assert "compile" not in gates


def test_compile_blocker_propagates(arui_env, tmp_path, monkeypatch):
    from backend.app import paper, paper_compile
    f = _mk(tmp_path, "A clean introduction about attack success rate.")
    monkeypatch.setattr(paper, "paper_folder", lambda *a, **k: f)
    monkeypatch.setattr(paper_compile, "status",
                        lambda: {"ok": False, "pdf_exists": True,
                                 "blockers": ["undefined references"]})
    assert "compile" in {b["gate"] for b in paper.bundle_blockers()}


def test_operator_waiver_removes_gate(arui_env, tmp_path, monkeypatch):
    from backend.app import paper, paper_compile
    f = _mk(tmp_path, "A clean introduction about attack success rate.")
    monkeypatch.setattr(paper, "paper_folder", lambda *a, **k: f)
    monkeypatch.setattr(paper_compile, "status",
                        lambda: {"ok": True, "pdf_exists": True})
    # only reviewer_sim blocks (it never ran); operator can waive it
    assert {b["gate"] for b in paper.bundle_blockers()} == {"reviewer_sim"}
    assert paper.bundle_blockers(waive=["reviewer_sim"]) == []


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
    assert paper.reviewer_sim_median() == 6.5
    assert paper.bundle_blockers() == []           # all gates clear
