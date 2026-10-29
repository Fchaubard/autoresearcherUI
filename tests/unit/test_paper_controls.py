"""Pin and bundle the real paper; missing prerequisites must not claim success."""
import subprocess
from pathlib import Path
import zipfile

import pytest


@pytest.fixture
def client(arui_env):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.app.api import router
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        yield client


@pytest.fixture
def paper_repo(client, tmp_path, monkeypatch):
    from backend.app import paper
    repo = tmp_path / "research"
    folder = repo / "latex"
    folder.mkdir(parents=True)
    (folder / "main.tex").write_text(r"\includegraphics{figures/result.pdf}")
    (folder / "figures").mkdir()
    (folder / "figures" / "result.pdf").write_bytes(b"figure-v1")
    (folder / "sections").mkdir()
    (folder / "sections" / "intro.tex").write_text("Measured results.")
    for command in (["git", "init", "-q", str(repo)],
                    ["git", "-C", str(repo), "add", "latex"],
                    ["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "Paper sources"]):
        subprocess.run(command, check=True, capture_output=True)
    monkeypatch.setattr(paper, "paper_folder", lambda *a, **k: folder)
    return folder


def test_pin_uses_enclosing_project_repository(client, paper_repo):
    from backend.app.db import SessionLocal
    from backend.app.models import PaperVersion
    assert not (paper_repo / ".git").exists()
    response = client.post("/api/paper/versions/pin", json={"label": "review draft"})
    assert response.status_code == 200, response.text
    result = response.json()
    assert len(result["sha"]) == 40
    with SessionLocal() as db:
        assert db.get(PaperVersion, result["id"]).latex_commit_sha == result["sha"]


def test_pin_without_commits_is_an_actionable_error(client, tmp_path, monkeypatch):
    from backend.app import paper
    folder = tmp_path / "no-git"
    folder.mkdir()
    monkeypatch.setattr(paper, "paper_folder", lambda *a, **k: folder)
    response = client.post("/api/paper/versions/pin", json={"label": "empty"})
    assert response.status_code == 400
    assert "Commit the paper" in response.json()["detail"]


def test_bundle_preserves_assets_and_freezes_submitted_pdf(client, paper_repo, tmp_path, monkeypatch):
    from backend.app import paper
    from backend.app.db import SessionLocal
    from backend.app.models import PaperVersion
    monkeypatch.setattr(paper, "bundle_blockers", lambda *a, **k: [])
    (paper_repo / "build").mkdir()
    (paper_repo / "build/main.pdf").write_bytes(b"submitted-pdf")
    (paper_repo / "build/private.log").write_text("exclude logs")
    (paper_repo / ".env").write_text("SECRET=not-for-bundle")
    (paper_repo / "format.sty").write_text("style")
    (paper_repo / "supplement").mkdir()
    (paper_repo / "supplement/details.tex").write_text("supplementary source")
    outside = tmp_path / "outside.txt"
    outside.write_text("private")
    (paper_repo / "linked.txt").symlink_to(outside)
    response = client.post("/api/paper/submit/bundle", json={})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["ok"]
    with zipfile.ZipFile(paper_repo.parent / result["zip"]) as bundle:
        names = set(bundle.namelist())
        assert {"main.pdf", "main.tex", "sections/intro.tex", "figures/result.pdf", "format.sty", "supplement/details.tex"} <= names
        assert not {".env", "build/private.log", "linked.txt"} & names
    with SessionLocal() as db:
        row = db.get(PaperVersion, result["version_id"])
        frozen = Path(row.frozen_pdf_path)
        assert len(row.latex_commit_sha) == 40
    (paper_repo / "build/main.pdf").write_bytes(b"later-compile")
    assert frozen.read_bytes() == b"submitted-pdf"


def test_reviewer_sim_missing_models_never_claims_started(client, monkeypatch):
    from backend.app import council
    monkeypatch.setattr(council, "configured_reviewers", lambda cfg: [])
    response = client.post("/api/paper/reviewer_sim/run", json={})
    assert response.status_code == 400
    assert "reviewer" in response.json()["detail"]


def test_reviewer_sim_missing_paper_never_claims_started(client, monkeypatch):
    from backend.app import council, paper
    monkeypatch.setattr(council, "configured_reviewers", lambda cfg: ["gemini"])
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic")
    monkeypatch.setattr(paper, "paper_folder", lambda: None)
    response = client.post("/api/paper/reviewer_sim/run", json={})
    assert response.status_code == 400
    assert "paper sources" in response.json()["detail"]


def test_reviewer_sim_valid_request_starts_worker(client, paper_repo, monkeypatch):
    from backend.app import council, api
    import threading
    import time
    monkeypatch.setattr(council, "configured_reviewers", lambda cfg: ["gemini"])
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic")
    called = threading.Event()
    monkeypatch.setattr(api, "_run_reviewer_sim", called.set)
    response = client.post("/api/paper/reviewer_sim/run", json={})
    assert response.json()["status"] == "started"
    assert called.wait(2)


def test_reviewer_sim_missing_selected_key_does_not_use_other_model(client, paper_repo, monkeypatch):
    from backend.app import api
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic")
    monkeypatch.setattr(api, "_run_reviewer_sim", lambda: pytest.fail("must not substitute reviewer B"))
    response = client.post("/api/paper/reviewer_sim/run", json={})
    assert response.status_code == 400
    assert "gemini-3.8-flash" in response.json()["detail"]


def test_rebuttal_failure_does_not_switch_models(client, monkeypatch):
    from backend.app import council
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic")
    seen = []
    monkeypatch.setattr(council, "_call_reviewer", lambda reviewer, *args: seen.append(reviewer))
    response = client.post("/api/paper/rebuttal/parse", json={"reviews": ["Please explain the baseline."]})
    assert response.status_code == 502
    assert seen == ["gemini"]
    assert "No substitute model" in response.json()["detail"]


def test_incomplete_model_assessment_cannot_enter_paper_mode(client, monkeypatch):
    from backend.app import paper
    from backend.app.db import SessionLocal
    from backend.app.models import PaperProposal
    with SessionLocal() as db:
        db.add(PaperProposal(id="failed", status="error", council_responses={"_error": "Selected model unavailable"}))
        db.commit()
    monkeypatch.setattr(paper, "enter_paper_mode", lambda **kwargs: pytest.fail("incomplete assessment cannot be accepted"))
    response = client.post("/api/paper/enter", json={"proposal_id": "failed"})
    assert response.status_code == 409


def test_proposal_missing_review_marks_assessment_failed(client, monkeypatch, make_project):
    from backend.app import api, council
    from backend.app.db import SessionLocal
    from backend.app.models import PaperProposal
    make_project()
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic")
    monkeypatch.setattr(council, "_settings", lambda: {**council.DEFAULTS, "council_enable_claude_tiebreaker": False})
    calls = []
    def review(reviewer, *args):
        calls.append(reviewer)
        return {"recommendation": "proceed_to_paper", "novelty": "high", "claims": [],
                "novelty_rationale": "Synthetic evidence", "rationale_md": "Complete", "red_flags": []} if reviewer == "gemini" else None
    monkeypatch.setattr(council, "_call_reviewer", review)
    with SessionLocal() as db:
        db.add(PaperProposal(id="partial", status="in_progress", council_responses={}))
        db.commit()
    api._run_paper_proposal_council("partial")
    with SessionLocal() as db:
        proposal = db.get(PaperProposal, "partial")
        assert proposal.status == "error"
        assert "gpt-6-astra" in proposal.council_responses["_error"]
    assert sorted(calls) == ["gemini", "openai"]


@pytest.mark.parametrize("output", [{"unexpected": "answer"}, {"concerns": "not a list"}, {"concerns": [{"title": "missing required details"}]}])
def test_rebuttal_invalid_schema_cannot_claim_success(client, monkeypatch, output):
    from backend.app import council, paper
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic")
    monkeypatch.setattr(council, "_call_reviewer", lambda *args: output)
    monkeypatch.setattr(paper, "file_decision", lambda **kwargs: pytest.fail("invalid model output must not file a decision"))
    response = client.post("/api/paper/rebuttal/parse", json={"reviews": ["Explain the baseline."]})
    assert response.status_code == 502
    assert response.json()["filed"] == 0


@pytest.mark.parametrize("valid", [False, True])
def test_proposal_requires_actual_assessment_schema(client, monkeypatch, make_project, valid):
    from backend.app import api, council
    from backend.app.db import SessionLocal
    from backend.app.models import PaperProposal
    make_project()
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic")
    monkeypatch.setattr(council, "_settings", lambda: {**council.DEFAULTS,
        "council_enable_openai": False, "council_enable_claude_tiebreaker": False})
    result = {"recommendation": "proceed_to_paper"}
    if valid:
        result.update(novelty="high", claims=[], novelty_rationale="Synthetic evidence",
                      rationale_md="Ready", red_flags=[])
    monkeypatch.setattr(council, "_call_reviewer", lambda *args: result)
    with SessionLocal() as db:
        db.add(PaperProposal(id="schema", status="in_progress", council_responses={}))
        db.commit()
    api._run_paper_proposal_council("schema")
    with SessionLocal() as db:
        proposal = db.get(PaperProposal, "schema")
        assert proposal.status == ("ready" if valid else "error")
