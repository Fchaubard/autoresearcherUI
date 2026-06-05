"""Unit tests for council._validate_directives_upsert
(RESEARCH_IMPROVEMENT_PLAN #5): 3:1 INCREMENTAL:ORTHOGONAL ratio enforced,
ORTHOGONAL/REPRODUCE required after >=2*GPU_COUNT stagnant reviews.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def env(arui_env, tmp_path):
    from backend.app import council as _c, directives as _d
    p = tmp_path / "directives.jsonl"
    _d.set_path_override(str(p))
    return _c, _d


def _stub_gpu_count(c, monkeypatch, n: int):
    monkeypatch.setattr(c, "_gpu_count_for_quota", lambda: n)


def test_empty_upsert_passes(env):
    c, _ = env
    ok, err = c._validate_directives_upsert([], prior_verdict="progress",
                                              stagnant_streak=0)
    assert ok


def test_rejects_missing_idea_class(env):
    c, _ = env
    ok, err = c._validate_directives_upsert(
        [{"type": "SCIENCE", "what": "x"}],
        prior_verdict="progress", stagnant_streak=0)
    assert not ok
    assert "idea_class" in err


def test_rejects_when_stagnant_streak_high_and_no_diverging(env, monkeypatch):
    c, _ = env
    _stub_gpu_count(c, monkeypatch, 2)
    ok, err = c._validate_directives_upsert(
        [{"type": "SCIENCE", "what": "tune lr",
          "idea_class": "INCREMENTAL"}],
        prior_verdict="stagnant", stagnant_streak=4)
    assert not ok
    assert "ORTHOGONAL or REPRODUCE" in err


def test_accepts_orthogonal_when_stagnant(env, monkeypatch):
    c, _ = env
    _stub_gpu_count(c, monkeypatch, 2)
    ok, err = c._validate_directives_upsert(
        [{"type": "SCIENCE", "what": "swap to diffusion",
          "idea_class": "ORTHOGONAL"}],
        prior_verdict="stagnant", stagnant_streak=4)
    assert ok, err


def test_accepts_reproduce_when_stagnant(env, monkeypatch):
    c, _ = env
    _stub_gpu_count(c, monkeypatch, 2)
    ok, err = c._validate_directives_upsert(
        [{"type": "SCIENCE", "what": "reproduce Smith2025",
          "idea_class": "REPRODUCE"}],
        prior_verdict="stagnant", stagnant_streak=4)
    assert ok


def test_3_to_1_ratio_enforced_with_existing_open(env, monkeypatch):
    c, d = env
    _stub_gpu_count(c, monkeypatch, 2)
    # Seed 3 open INCREMENTAL + 1 ORTHOGONAL — ratio 3:1, OK.
    for i in range(3):
        d.upsert({"type": "SCIENCE", "what": f"inc{i}",
                  "idea_class": "INCREMENTAL"})
    d.upsert({"type": "SCIENCE", "what": "orth",
              "idea_class": "ORTHOGONAL"})
    # Adding ONE more INCREMENTAL takes ratio to 4:1 — should be rejected
    ok, err = c._validate_directives_upsert(
        [{"type": "SCIENCE", "what": "another inc",
          "idea_class": "INCREMENTAL"}],
        prior_verdict="progress", stagnant_streak=0)
    assert not ok
    assert "3:1" in err


def test_3_to_1_ratio_ok_with_reproduce_counted_on_orthogonal_side(
        env, monkeypatch):
    c, d = env
    _stub_gpu_count(c, monkeypatch, 2)
    # Seed 3 INCREMENTAL + 0 ORTHOGONAL but 1 REPRODUCE — same effective
    # ratio 3:1, OK.
    for i in range(3):
        d.upsert({"type": "SCIENCE", "what": f"inc{i}",
                  "idea_class": "INCREMENTAL"})
    d.upsert({"type": "SCIENCE", "what": "rep",
              "idea_class": "REPRODUCE"})
    ok, err = c._validate_directives_upsert(
        [], prior_verdict="progress", stagnant_streak=0)
    assert ok, err


def test_quota_threshold_uses_2x_gpu_count(env, monkeypatch):
    c, _ = env
    _stub_gpu_count(c, monkeypatch, 4)
    # With 4 GPUs, quota_floor = 8 — streak of 7 should NOT yet force
    # ORTHOGONAL.
    ok, err = c._validate_directives_upsert(
        [{"type": "SCIENCE", "what": "still inc ok",
          "idea_class": "INCREMENTAL"}],
        prior_verdict="stagnant", stagnant_streak=7)
    assert ok, err
    # But 8 SHOULD force.
    ok2, err2 = c._validate_directives_upsert(
        [{"type": "SCIENCE", "what": "still inc nope",
          "idea_class": "INCREMENTAL"}],
        prior_verdict="stagnant", stagnant_streak=8)
    assert not ok2


def test_apply_directives_persists_only_when_validator_ok(env):
    c, d = env
    review = {"directives_upsert": [
        {"type": "SCIENCE", "what": "ok",
         "idea_class": "INCREMENTAL"}],
              "directives_close": []}
    report = c._apply_to_directives_jsonl(review)
    assert report["rejected"] == ""
    assert report["upserted"] == 1
    assert len(d.read_all()) == 1


def test_apply_directives_rejected_does_not_persist(env, monkeypatch):
    c, d = env
    monkeypatch.setattr(c, "_gpu_count_for_quota", lambda: 1)
    review = {"directives_upsert": [
        # Stagnant streak forces ORTHOGONAL but this entry isn't.
        {"type": "SCIENCE", "what": "more hp tuning",
         "idea_class": "INCREMENTAL"}],
              "directives_close": []}
    # Mock the stagnant_streak via the history.
    for i in range(2):
        c._append_strategic_history({"id": f"rv-{i}",
                                       "verdict": "stagnant",
                                       "top_directive_id": ""})
    report = c._apply_to_directives_jsonl(review)
    assert "ORTHOGONAL" in report["rejected"]
    assert report["upserted"] == 0
    assert d.read_all() == []
