"""Unit tests for backend.app.kill_criteria.

These tests cover (a) the free-text parser — every rule shape the user is
allowed to type — and (b) the rule evaluator, which is what monitor.py
calls every reconciler tick to decide whether a live run should be
killed.

The parser is intentionally permissive (any phrasing that the user
*could* reasonably type should round-trip), and unrecognised clauses are
silently dropped so a typo in one OR-clause doesn't disable the rest of
the policy.
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace


# ───────────────────────────── parse: time ────────────────────────────────


def test_parse_time_one_hour():
    from backend.app.kill_criteria import TimeRule, parse
    rules = parse("1 hour")
    assert len(rules) == 1
    r = rules[0]
    assert isinstance(r, TimeRule)
    assert r.seconds == 3600.0


def test_parse_time_short_form():
    from backend.app.kill_criteria import TimeRule, parse
    rules = parse("2h")
    assert len(rules) == 1
    assert isinstance(rules[0], TimeRule)
    assert rules[0].seconds == 7200.0


def test_parse_time_kill_after():
    from backend.app.kill_criteria import TimeRule, parse
    rules = parse("kill after 3 hours")
    assert len(rules) == 1
    assert isinstance(rules[0], TimeRule)
    assert rules[0].seconds == 3 * 3600


def test_parse_time_minutes():
    from backend.app.kill_criteria import TimeRule, parse
    rules = parse("30 minutes")
    assert len(rules) == 1
    assert isinstance(rules[0], TimeRule)
    assert rules[0].seconds == 30 * 60


def test_parse_time_fractional_hours():
    from backend.app.kill_criteria import TimeRule, parse
    rules = parse("2.5 hours")
    assert len(rules) == 1
    assert isinstance(rules[0], TimeRule)
    assert rules[0].seconds == 2.5 * 3600


# ───────────────────────────── parse: steps ───────────────────────────────


def test_parse_steps_simple():
    from backend.app.kill_criteria import StepRule, parse
    rules = parse("1000 steps")
    assert len(rules) == 1
    assert isinstance(rules[0], StepRule)
    assert rules[0].steps == 1000


def test_parse_steps_kill_after():
    from backend.app.kill_criteria import StepRule, parse
    rules = parse("kill after 5000 steps")
    assert len(rules) == 1
    assert isinstance(rules[0], StepRule)
    assert rules[0].steps == 5000


# ──────────────────────────── parse: plateau ──────────────────────────────


def test_parse_plateau_basic():
    from backend.app.kill_criteria import PlateauRule, parse
    rules = parse("val_loss plateaus for 500 steps")
    assert len(rules) == 1
    r = rules[0]
    assert isinstance(r, PlateauRule)
    assert r.metric == "val_loss"
    assert r.steps == 500


def test_parse_plateau_kill_after_prefix():
    from backend.app.kill_criteria import PlateauRule, parse
    rules = parse("kill after val_loss plateaus for 1000 steps")
    assert len(rules) == 1
    r = rules[0]
    assert isinstance(r, PlateauRule)
    assert r.metric == "val_loss"
    assert r.steps == 1000


def test_parse_plateau_no_improvement_phrasing():
    from backend.app.kill_criteria import PlateauRule, parse
    rules = parse("val_acc has not improved for 200 steps")
    assert len(rules) == 1
    r = rules[0]
    assert isinstance(r, PlateauRule)
    assert r.metric == "val_acc"
    assert r.steps == 200


# ─────────────────────────── parse: threshold ─────────────────────────────


def test_parse_threshold_gt():
    from backend.app.kill_criteria import ThresholdRule, parse
    rules = parse("val_loss > 5.0 for 100 steps")
    assert len(rules) == 1
    r = rules[0]
    assert isinstance(r, ThresholdRule)
    assert r.metric == "val_loss"
    assert r.op == ">"
    assert r.value == 5.0
    assert r.steps == 100


def test_parse_threshold_lt():
    from backend.app.kill_criteria import ThresholdRule, parse
    rules = parse("val_acc < 0.1 for 50 steps")
    assert len(rules) == 1
    r = rules[0]
    assert isinstance(r, ThresholdRule)
    assert r.op == "<"
    assert r.value == 0.1
    assert r.steps == 50


def test_parse_threshold_above_alias():
    from backend.app.kill_criteria import ThresholdRule, parse
    rules = parse("val_loss above 5 for 100 steps")
    assert len(rules) == 1
    r = rules[0]
    assert isinstance(r, ThresholdRule)
    assert r.op == ">"
    assert r.value == 5.0


def test_parse_threshold_no_for_clause_defaults_to_one():
    from backend.app.kill_criteria import ThresholdRule, parse
    rules = parse("val_loss >= 10")
    assert len(rules) == 1
    r = rules[0]
    assert isinstance(r, ThresholdRule)
    assert r.op == ">="
    assert r.value == 10.0
    assert r.steps == 1


# ───────────────────────────── parse: OR'd ────────────────────────────────


def test_parse_or_two_rules():
    from backend.app.kill_criteria import PlateauRule, TimeRule, parse
    rules = parse("1 hour OR val_loss plateaus for 500 steps")
    assert len(rules) == 2
    assert any(isinstance(r, TimeRule) and r.seconds == 3600 for r in rules)
    assert any(isinstance(r, PlateauRule) and r.steps == 500 for r in rules)


def test_parse_or_three_rules_lower_case_or():
    from backend.app.kill_criteria import (PlateauRule, StepRule,
                                            ThresholdRule, TimeRule, parse)
    rules = parse(
        "2 hours or 10000 steps or val_loss > 100 for 5 steps or "
        "val_acc plateaus for 300 steps")
    assert len(rules) == 4
    kinds = {type(r) for r in rules}
    assert TimeRule in kinds
    assert StepRule in kinds
    assert ThresholdRule in kinds
    assert PlateauRule in kinds


def test_parse_comma_separated():
    from backend.app.kill_criteria import StepRule, TimeRule, parse
    rules = parse("1 hour, 500 steps")
    assert len(rules) == 2
    assert isinstance(rules[0], TimeRule)
    assert isinstance(rules[1], StepRule)


def test_parse_empty_returns_empty():
    from backend.app.kill_criteria import parse
    assert parse("") == []
    assert parse("   ") == []
    assert parse(None) == []  # type: ignore[arg-type]


def test_parse_unknown_phrasing_is_dropped_silently():
    """A typo in one OR'd clause shouldn't disable the rest."""
    from backend.app.kill_criteria import TimeRule, parse
    rules = parse("1 hour OR what even is this rule")
    assert len(rules) == 1
    assert isinstance(rules[0], TimeRule)


# ─────────────────────────────── check_run ────────────────────────────────


def _now_run(started_iso: str, run_id: str = "r1"):
    """Tiny stand-in for a Run row with just the fields check_run reads."""
    return SimpleNamespace(id=run_id, run_name=run_id,
                            started_at=started_iso, created_at=started_iso,
                            tmux_session="", config={})


def test_check_run_empty_rules_never_fires():
    from backend.app.kill_criteria import check_run
    run = _now_run(dt.datetime.now(dt.timezone.utc).isoformat())
    fire, reason = check_run(run, [], {})
    assert fire is False
    assert reason == ""


def test_check_run_time_rule_fires_after_elapsed():
    from backend.app.kill_criteria import TimeRule, check_run
    started = (dt.datetime.now(dt.timezone.utc)
               - dt.timedelta(hours=2)).isoformat()
    run = _now_run(started)
    fire, reason = check_run(run, [TimeRule(seconds=3600)], {})
    assert fire is True
    assert "1h" in reason or "hour" in reason or "3600" in reason


def test_check_run_time_rule_does_not_fire_when_fresh():
    from backend.app.kill_criteria import TimeRule, check_run
    started = dt.datetime.now(dt.timezone.utc).isoformat()
    run = _now_run(started)
    fire, _ = check_run(run, [TimeRule(seconds=3600)], {})
    assert fire is False


def test_check_run_step_rule_fires():
    from backend.app.kill_criteria import StepRule, check_run
    started = dt.datetime.now(dt.timezone.utc).isoformat()
    run = _now_run(started)
    metrics = {"val_loss": [[100, 1.0], [500, 0.5], [1500, 0.4]]}
    fire, _ = check_run(run, [StepRule(steps=1000)], metrics)
    assert fire is True


def test_check_run_step_rule_does_not_fire_yet():
    from backend.app.kill_criteria import StepRule, check_run
    started = dt.datetime.now(dt.timezone.utc).isoformat()
    run = _now_run(started)
    metrics = {"val_loss": [[100, 1.0], [500, 0.5]]}
    fire, _ = check_run(run, [StepRule(steps=1000)], metrics)
    assert fire is False


def test_check_run_plateau_fires_for_min_metric():
    """val_loss is a 'minimize' metric — if the best value was at step 100
    and we're now at step 700, that's 600 steps of no improvement."""
    from backend.app.kill_criteria import PlateauRule, check_run
    started = dt.datetime.now(dt.timezone.utc).isoformat()
    run = _now_run(started)
    metrics = {"val_loss": [[100, 0.5], [400, 0.7], [700, 0.6]]}
    fire, _ = check_run(run, [PlateauRule(metric="val_loss", steps=500)],
                        metrics)
    assert fire is True


def test_check_run_plateau_does_not_fire_if_improving():
    from backend.app.kill_criteria import PlateauRule, check_run
    started = dt.datetime.now(dt.timezone.utc).isoformat()
    run = _now_run(started)
    # New low every couple of steps -> no plateau
    metrics = {"val_loss": [[100, 0.9], [200, 0.8], [300, 0.7], [400, 0.6]]}
    fire, _ = check_run(run, [PlateauRule(metric="val_loss", steps=500)],
                        metrics)
    assert fire is False


def test_check_run_plateau_fires_for_max_metric():
    """val_acc — best at step 100 (0.95), still no improvement at step 800."""
    from backend.app.kill_criteria import PlateauRule, check_run
    started = dt.datetime.now(dt.timezone.utc).isoformat()
    run = _now_run(started)
    metrics = {"val_acc": [[100, 0.95], [400, 0.90], [800, 0.85]]}
    fire, _ = check_run(run, [PlateauRule(metric="val_acc", steps=500)],
                        metrics)
    assert fire is True


def test_check_run_threshold_fires_when_condition_holds_long_enough():
    """val_loss > 5.0 for 3 consecutive trailing logs."""
    from backend.app.kill_criteria import ThresholdRule, check_run
    started = dt.datetime.now(dt.timezone.utc).isoformat()
    run = _now_run(started)
    metrics = {"val_loss": [[1, 4.0], [2, 6.0], [3, 7.0], [4, 8.0]]}
    fire, _ = check_run(
        run, [ThresholdRule(metric="val_loss", op=">", value=5.0, steps=3)],
        metrics)
    assert fire is True


def test_check_run_threshold_does_not_fire_when_window_too_short():
    from backend.app.kill_criteria import ThresholdRule, check_run
    started = dt.datetime.now(dt.timezone.utc).isoformat()
    run = _now_run(started)
    metrics = {"val_loss": [[1, 4.0], [2, 6.0]]}
    fire, _ = check_run(
        run, [ThresholdRule(metric="val_loss", op=">", value=5.0, steps=3)],
        metrics)
    assert fire is False


def test_check_run_threshold_resets_on_violation():
    """The condition has to hold for the trailing N steps — a satisfying
    point in the middle resets the run."""
    from backend.app.kill_criteria import ThresholdRule, check_run
    started = dt.datetime.now(dt.timezone.utc).isoformat()
    run = _now_run(started)
    metrics = {"val_loss": [[1, 6.0], [2, 4.0], [3, 6.0], [4, 7.0]]}
    fire, _ = check_run(
        run, [ThresholdRule(metric="val_loss", op=">", value=5.0, steps=3)],
        metrics)
    # only 2 consecutive trailing satisfy
    assert fire is False


def test_check_run_or_rules_first_match_wins():
    """When ANY rule in the list fires, the run is killed."""
    from backend.app.kill_criteria import StepRule, TimeRule, check_run
    started = dt.datetime.now(dt.timezone.utc).isoformat()
    run = _now_run(started)
    metrics = {"val_loss": [[1, 0.5], [2000, 0.4]]}
    # The time rule WON'T fire (just started); the step rule WILL.
    rules = [TimeRule(seconds=3600), StepRule(steps=1000)]
    fire, reason = check_run(run, rules, metrics)
    assert fire is True
    assert "step" in reason.lower()


def test_check_run_handles_unparseable_started_at_gracefully():
    """A bogus started_at shouldn't crash check_run — time rule just
    fails to fire, and we move on to evaluate other rules."""
    from backend.app.kill_criteria import StepRule, TimeRule, check_run
    run = SimpleNamespace(id="r1", run_name="r1",
                          started_at="not-an-iso", created_at="",
                          tmux_session="", config={})
    rules = [TimeRule(seconds=60),
             StepRule(steps=10)]
    metrics = {"val_loss": [[100, 0.5]]}
    fire, reason = check_run(run, rules, metrics)
    assert fire is True            # step rule fires
    assert "step" in reason.lower()


def test_check_run_no_metrics_does_not_fire_step_rule():
    from backend.app.kill_criteria import StepRule, check_run
    started = dt.datetime.now(dt.timezone.utc).isoformat()
    run = _now_run(started)
    fire, _ = check_run(run, [StepRule(steps=100)], {})
    assert fire is False


# ─────────────────── parser end-to-end + check integration ────────────────


def test_parse_then_check_one_hour_then_elapsed():
    from backend.app.kill_criteria import check_run, parse
    rules = parse("1 hour")
    started = (dt.datetime.now(dt.timezone.utc)
               - dt.timedelta(hours=2)).isoformat()
    run = _now_run(started)
    fire, _ = check_run(run, rules, {})
    assert fire is True


def test_parse_then_check_or_policy():
    from backend.app.kill_criteria import check_run, parse
    rules = parse("1 hour OR val_loss plateaus for 100 steps")
    assert len(rules) == 2
    # Started 30 minutes ago -> time rule won't fire.
    started = (dt.datetime.now(dt.timezone.utc)
               - dt.timedelta(minutes=30)).isoformat()
    run = _now_run(started)
    # val_loss best is 0.5 at step 100, still at 0.6 at step 500 — that's
    # 400 steps of no improvement, well past the 100-step window.
    metrics = {"val_loss": [[100, 0.5], [300, 0.7], [500, 0.6]]}
    fire, reason = check_run(run, rules, metrics)
    assert fire is True
    assert "plateau" in reason.lower() or "val_loss" in reason.lower()
