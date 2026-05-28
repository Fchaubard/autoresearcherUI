"""Unit tests for backend.app.models — every model exposes .dict()."""
from __future__ import annotations


def test_project_dict_roundtrip(arui_env):
    from backend.app.models import Project
    p = Project(id="p1", name="x", validation_metric="val_loss")
    d = p.dict()
    assert d["id"] == "p1"
    assert d["name"] == "x"
    assert d["validation_metric"] == "val_loss"
    # All declared columns appear
    assert set(d.keys()) >= {
        "id", "name", "repo_url", "purpose", "validation_metric",
        "metric_direction", "time_budget_sec", "status",
        "baseline_run_id", "gpu_count", "created_at"}


def test_run_dict_includes_paper_columns(arui_env):
    from backend.app.models import Run
    r = Run(id="r1", project_id="p1", run_name="r1",
            paper_role="headline", n_seeds=3, depends_on=["r0"])
    d = r.dict()
    assert d["paper_role"] == "headline"
    assert d["n_seeds"] == 3
    assert d["depends_on"] == ["r0"]
    assert "context" in d


def test_idea_dict_default_values(arui_env):
    from backend.app.models import Idea
    i = Idea(id="i1", project_id="p1", idea_id="foo")
    d = i.dict()
    assert d["idea_id"] == "foo"
    assert d["status"] in ("not_implemented", None) or isinstance(
        d["status"], str)


def test_event_dict_roundtrip(arui_env):
    from backend.app.models import Event
    e = Event(id="e1", type="run_finished", message="hi")
    assert e.dict()["type"] == "run_finished"


def test_chat_message_dict(arui_env):
    from backend.app.models import ChatMessage
    cm = ChatMessage(id="c1", role="agent", content="hello")
    assert cm.dict()["role"] == "agent"
    assert cm.dict()["content"] == "hello"


def test_journal_entry_dict(arui_env):
    from backend.app.models import JournalEntry
    j = JournalEntry(id="j1", title="t", body="b")
    assert j.dict()["title"] == "t"


def test_gpu_dict(arui_env):
    from backend.app.models import Gpu
    g = Gpu(index=0, model="A40")
    d = g.dict()
    assert d["index"] == 0
    assert d["model"] == "A40"


def test_setting_dict(arui_env):
    from backend.app.models import Setting
    s = Setting(key="onboarding", value={"foo": "bar"})
    assert s.dict()["value"] == {"foo": "bar"}


def test_paper_meta_dict(arui_env):
    from backend.app.models import PaperMeta
    pm = PaperMeta(id="pm1", venue="ICLR 2026", phase="proposal")
    d = pm.dict()
    assert d["venue"] == "ICLR 2026"
    assert d["phase"] == "proposal"


def test_paper_proposal_dict(arui_env):
    from backend.app.models import PaperProposal
    pp = PaperProposal(id="pp1", status="ready",
                       council_responses={"gemini": {}})
    assert pp.dict()["status"] == "ready"


def test_paper_claim_dict(arui_env):
    from backend.app.models import PaperClaim
    c = PaperClaim(id="pc1", title="claim", evidence_strength="strong")
    d = c.dict()
    assert d["title"] == "claim"
    assert d["evidence_strength"] == "strong"


def test_paper_figure_dict(arui_env):
    from backend.app.models import PaperFigure
    f = PaperFigure(id="pf1", kind="bar", title="fig",
                    panels_json=[{"x": [1]}])
    d = f.dict()
    assert d["kind"] == "bar"
    assert d["panels_json"] == [{"x": [1]}]


def test_paper_baseline_dict(arui_env):
    from backend.app.models import PaperBaseline
    b = PaperBaseline(id="pb1", name="SOTA", value=0.42)
    assert b.dict()["value"] == 0.42


def test_paper_citation_dict(arui_env):
    from backend.app.models import PaperCitation
    c = PaperCitation(key="lou2024sedd", title="SEDD", year="2024")
    assert c.dict()["key"] == "lou2024sedd"


def test_paper_section_dict(arui_env):
    from backend.app.models import PaperSection
    s = PaperSection(id="sec1", slug="intro", title="Intro")
    assert s.dict()["slug"] == "intro"


def test_paper_version_dict(arui_env):
    from backend.app.models import PaperVersion
    v = PaperVersion(id="pv1", label="v0", snapshot_json={"k": 1})
    assert v.dict()["label"] == "v0"
    assert v.dict()["snapshot_json"] == {"k": 1}


def test_paper_decision_dict(arui_env):
    from backend.app.models import PaperDecision
    d = PaperDecision(id="pd1", kind="cite_paper", title="cite x",
                      status="pending")
    assert d.dict()["kind"] == "cite_paper"


def test_paper_review_sim_dict(arui_env):
    from backend.app.models import PaperReviewSim
    r = PaperReviewSim(id="prs1", version_id="v0", model="gpt")
    assert r.dict()["model"] == "gpt"


def test_paper_budget_event_dict(arui_env):
    from backend.app.models import PaperBudgetEvent
    e = PaperBudgetEvent(id="be1", kind="gpu", cost_units=1.5,
                         cost_usd=0.30)
    d = e.dict()
    assert d["cost_units"] == 1.5
    assert d["kind"] == "gpu"


def test_dataset_registry_dict(arui_env):
    from backend.app.models import DatasetRegistry
    d = DatasetRegistry(name="cifar10", version="1.0")
    assert d.dict()["name"] == "cifar10"


def test_mode_history_dict(arui_env):
    from backend.app.models import ModeHistory
    m = ModeHistory(id="mh1", from_mode="research", to_mode="paper")
    assert m.dict()["to_mode"] == "paper"
