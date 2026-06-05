"""Unit tests for adversarial-debate-when-stagnant
(RESEARCH_IMPROVEMENT_PLAN #10).

The DEBATE_SYSTEM prompt now contains a conditional adversarial block
that fires when the user message carries "PRIOR STRATEGIC VERDICT:
stagnant" or "regressing". The strategic context builder injects that
marker when the prior verdict was stagnant/regressing/escalation_halt.
"""
from __future__ import annotations

import pytest


def test_debate_system_contains_adversarial_block(arui_env):
    from backend.app import council
    assert "ADVERSARIAL MODE" in council.DEBATE_SYSTEM
    assert "ORTHOGONAL" in council.DEBATE_SYSTEM


def test_strategic_ctx_text_injects_prior_verdict_marker(arui_env):
    from backend.app import council
    ctx = {"all_prior_runs_oneliners": "",
            "lessons_so_far": "", "all_prior_runs_count": 0}
    out = council._strategic_ctx_text(ctx, strategic_state={
        "previous_top_directive_id": "d-001",
        "previous_directive_implemented": "NO",
        "consecutive_unimplemented_count": 2,
        "prior_verdicts": ["stagnant", "stagnant", "progress"],
    })
    assert "PRIOR STRATEGIC VERDICT: stagnant" in out


def test_strategic_ctx_text_progress_does_not_inject_adversarial_marker(
        arui_env):
    from backend.app import council
    ctx = {"all_prior_runs_oneliners": "",
            "lessons_so_far": "", "all_prior_runs_count": 0}
    out = council._strategic_ctx_text(ctx, strategic_state={
        "previous_top_directive_id": "",
        "previous_directive_implemented": "YES",
        "consecutive_unimplemented_count": 0,
        "prior_verdicts": ["progress"],
    })
    # the literal "PRIOR STRATEGIC VERDICT: stagnant" string MUST NOT
    # appear when the prior verdict was healthy
    assert "PRIOR STRATEGIC VERDICT: stagnant" not in out
    assert "PRIOR STRATEGIC VERDICT: regressing" not in out


def test_strategic_ctx_text_includes_deterministic_count(arui_env):
    from backend.app import council
    ctx = {"all_prior_runs_oneliners": "",
            "lessons_so_far": "", "all_prior_runs_count": 0}
    out = council._strategic_ctx_text(ctx, strategic_state={
        "previous_top_directive_id": "d-abc",
        "previous_directive_implemented": "NO",
        "consecutive_unimplemented_count": 7,
        "prior_verdicts": [],
    })
    assert "consecutive_unimplemented_count: 7" in out
    assert "previous_top_directive_id: d-abc" in out


def test_consecutive_stagnant_count_zero_when_healthy(arui_env):
    from backend.app import council
    council._append_strategic_history({"id": "rv-1",
                                         "verdict": "progress",
                                         "top_directive_id": ""})
    assert council._consecutive_stagnant_count() == 0


def test_consecutive_stagnant_count_tracks_streak(arui_env):
    from backend.app import council
    for v in ("progress", "stagnant", "stagnant", "regressing"):
        council._append_strategic_history({"id": f"rv-{v}",
                                             "verdict": v,
                                             "top_directive_id": ""})
    # Newest first: regressing, stagnant, stagnant, progress -> streak 3.
    assert council._consecutive_stagnant_count() == 3


def test_consecutive_stagnant_count_escalation_halt_counts(arui_env):
    from backend.app import council
    for v in ("stagnant", "escalation_halt"):
        council._append_strategic_history({"id": f"rv-{v}",
                                             "verdict": v,
                                             "top_directive_id": ""})
    assert council._consecutive_stagnant_count() == 2
