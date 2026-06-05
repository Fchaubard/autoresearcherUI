"""Unit tests for the stateful strategic council
(RESEARCH_IMPROVEMENT_PLAN #2): consecutive_unimplemented_count is
computed DETERMINISTICALLY by the orchestrator and the LLM is forced to
echo it; >=3 unimplemented in a row triggers ESCALATION_HALT and a HALT
directive automatically. Strategic_review_history persists across
notional reboots.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def env(arui_env, tmp_path):
    from backend.app import council as _c, directives as _d
    p = tmp_path / "directives.jsonl"
    _d.set_path_override(str(p))
    return _c, _d


def test_history_empty_when_no_reviews(env):
    c, _ = env
    assert c._strategic_history() == []
    assert c._previous_top_directive_id() == ""
    assert c._consecutive_unimplemented_count("d-001") == 0


def test_history_persistence_round_trip(env):
    c, _ = env
    c._append_strategic_history({"id": "rv-1", "verdict": "stagnant",
                                  "top_directive_id": "d-001",
                                  "previous_top_directive_id": "",
                                  "implemented": "NO"})
    c._append_strategic_history({"id": "rv-2", "verdict": "stagnant",
                                  "top_directive_id": "d-001",
                                  "previous_top_directive_id": "d-001",
                                  "implemented": "NO"})
    h = c._strategic_history()
    assert len(h) == 2
    # Newest first
    assert h[0]["id"] == "rv-2"
    assert c._previous_top_directive_id() == "d-001"


def test_consecutive_unimplemented_count_counts_streak(env):
    c, _ = env
    for i in range(4):
        c._append_strategic_history({
            "id": f"rv-{i}", "verdict": "stagnant",
            "top_directive_id": "d-001",
            "implemented": "NO" if i > 0 else "NO",
        })
    # All 4 entries match the SAME top directive with implemented=NO -> 4.
    assert c._consecutive_unimplemented_count("d-001") == 4


def test_consecutive_unimplemented_count_breaks_on_yes(env):
    c, _ = env
    c._append_strategic_history({"id": "rv-1", "verdict": "progress",
                                  "top_directive_id": "d-001",
                                  "implemented": "YES"})
    c._append_strategic_history({"id": "rv-2", "verdict": "stagnant",
                                  "top_directive_id": "d-001",
                                  "implemented": "NO"})
    c._append_strategic_history({"id": "rv-3", "verdict": "stagnant",
                                  "top_directive_id": "d-001",
                                  "implemented": "NO"})
    # Newest first: NO, NO, YES -> streak of 2 unimplemented.
    assert c._consecutive_unimplemented_count("d-001") == 2


def test_was_directive_implemented_no_directive_means_yes(env):
    c, _ = env
    assert c._was_directive_implemented("") == "YES"
    assert c._was_directive_implemented("nonexistent") == "YES"


def test_was_directive_implemented_open_means_no(env):
    c, d = env
    stored, _ = d.upsert({"type": "BLOCKER_INFRA",
                            "what": "build hash registry",
                            "idea_class": "INFRA"})
    assert c._was_directive_implemented(stored["id"]) == "NO"
    d.close(stored["id"])
    assert c._was_directive_implemented(stored["id"]) == "YES"


def test_compute_strategic_state_increments_when_unimplemented(env):
    c, d = env
    # Set up a directive that's open, then add a history entry pointing
    # at it with implemented=NO. The NEXT compute should bump ccu to 2.
    stored, _ = d.upsert({"type": "BLOCKER_INFRA", "what": "b",
                            "idea_class": "INFRA"})
    c._append_strategic_history({
        "id": "rv-1", "verdict": "stagnant",
        "top_directive_id": stored["id"], "implemented": "NO",
    })
    state = c._compute_strategic_state()
    assert state["previous_top_directive_id"] == stored["id"]
    assert state["previous_directive_implemented"] == "NO"
    # 1 prior NO entry + the +1 for the current review = 2.
    assert state["consecutive_unimplemented_count"] == 2


def test_strategic_review_forces_escalation_after_3_unimplemented(
        env, monkeypatch):
    c, d = env
    stored, _ = d.upsert({"type": "BLOCKER_INFRA", "what": "blk",
                            "idea_class": "INFRA"})
    # Seed 3 consecutive NO history entries with the same top directive
    for i in range(3):
        c._append_strategic_history({
            "id": f"rv-{i}", "verdict": "stagnant",
            "top_directive_id": stored["id"],
            "implemented": "NO",
        })
    # Confirm the deterministic ccu is now >=3
    state = c._compute_strategic_state()
    assert state["consecutive_unimplemented_count"] >= 3
    # Stub out reviewer + ensure the deterministic override fires
    monkeypatch.setattr(
        c, "_available_reviewers", lambda cfg: ["gemini"])
    monkeypatch.setattr(
        c, "_call_reviewer",
        lambda *a, **k: {"verdict": "stagnant", "learning": "",
                          "rerank_pending": [], "new_ideas": [],
                          "veto": [], "directives_upsert": [],
                          "directives_close": []})
    # Need a Project row + a batch run so _build_strategic_context works.
    from backend.app.db import SessionLocal
    from backend.app.models import Project, Run
    import datetime as dt
    db = SessionLocal()
    try:
        db.add(Project(id="p", name="x", validation_metric="acc",
                       metric_direction="maximize",
                       created_at=dt.datetime.now(
                           dt.timezone.utc).isoformat()))
        db.add(Run(id="r1", project_id="p", run_name="r1",
                   status="kept", config={},
                   created_at=dt.datetime.now(
                       dt.timezone.utc).isoformat()))
        db.commit()
    finally:
        db.close()
    out = c.strategic_review(["r1"])
    assert out is not None
    assert out["verdict"] == "ESCALATION_HALT"
    # Council was forced to add a HALT directive
    types = [str(x.get("type")) for x in (out.get("directives_upsert") or [])]
    assert "HALT" in types


def test_strategic_history_capped(env):
    c, _ = env
    for i in range(40):
        c._append_strategic_history({"id": f"rv-{i}",
                                       "verdict": "progress",
                                       "top_directive_id": ""})
    assert len(c._strategic_history()) == c._STRAT_HISTORY_MAX
