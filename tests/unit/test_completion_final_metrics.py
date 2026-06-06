"""Regression test for council completion-review `final_metrics`.

Bug (overnight watch, 2026-06-06): `_build_completion_review_context`
computed each evidence run's `final_metrics` by querying a HARDCODED list
of metric keys carried over from an earlier project
(`utility_f1`, `fpr_block`, `asr_hidden`, ...). For any other research
task — e.g. the glitch-tokens project, which logs `jaccard`,
`behavior_shift_delta`, `paired_ci_low` — every hardcoded key missed, so
`final_metrics` was `{}` and the agent's real evidence was structurally
invisible to the completion reviewers. The openai reviewer then rejected
otherwise-complete conclusions for "no evidence."

Fix: query ALL keys the run actually logged (metrics.query(rid) with
wanted=None falls back to keys(rid)). These tests guard that contract.
"""
from __future__ import annotations


def _log(metrics, rid, key, value, step=0):
    metrics.append(rid, [{"key": key, "step": step,
                          "value": value, "wall_time": 0.0}])


def test_final_metrics_surfaces_project_specific_keys(
        arui_env, make_project, make_run):
    from backend.app import metrics, council
    make_project(name="glitch-tokens-mistral",
                 validation_metric="glitch_token_behavior_shift",
                 metric_direction="minimize")
    rid = "run-glitch-1"
    make_run(id=rid, status="kept_novel", headline_metric=0.2147)
    # Log metrics that are NOT in the old hardcoded backdoor-project list.
    _log(metrics, rid, "jaccard", 0.0123)
    _log(metrics, rid, "behavior_shift_delta", 0.2147)
    _log(metrics, rid, "paired_ci_low", -0.04)
    _log(metrics, rid, "paired_ci_high", 0.05)

    ctx = council._build_completion_review_context(
        [rid], summary="done", answer_to_purpose="null result",
        recommendation="WRITE_PAPER")
    assert ctx is not None
    ev = {e["id"]: e for e in ctx["evidence_runs"]}[rid]
    fm = ev["final_metrics"]
    # The real evidence is now visible to the reviewer.
    assert fm.get("jaccard") == 0.0123
    assert fm.get("behavior_shift_delta") == 0.2147
    assert fm.get("paired_ci_low") == -0.04
    assert fm.get("paired_ci_high") == 0.05
    # And it is no longer the empty dict that triggered the bug.
    assert fm != {}


def test_final_metrics_empty_when_nothing_logged(
        arui_env, make_project, make_run):
    """A run that logged nothing yields {} (not an error)."""
    from backend.app import council
    make_project()
    rid = "run-bare"
    make_run(id=rid)
    ctx = council._build_completion_review_context(
        [rid], summary="s", answer_to_purpose="a", recommendation="r")
    assert ctx is not None
    ev = {e["id"]: e for e in ctx["evidence_runs"]}[rid]
    assert ev["final_metrics"] == {}


def test_final_metrics_takes_last_value(
        arui_env, make_project, make_run):
    """final_metrics reports the FINAL logged value of each key."""
    from backend.app import metrics, council
    make_project()
    rid = "run-series"
    make_run(id=rid)
    for step, v in enumerate([0.9, 0.5, 0.3142]):
        _log(metrics, rid, "custom_metric", v, step=step)
    ctx = council._build_completion_review_context(
        [rid], summary="s", answer_to_purpose="a", recommendation="r")
    ev = {e["id"]: e for e in ctx["evidence_runs"]}[rid]
    assert ev["final_metrics"]["custom_metric"] == 0.3142
