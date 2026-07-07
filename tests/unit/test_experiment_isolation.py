"""Experiment isolation contract: naming invariant, merge-only-if-improved,
and the no-stacking-on-failed-branches rule."""
import backend.app.experiment_isolation as ei


def test_naming_shares_run_id():
    rid = "diff-t-schedule"
    assert ei.branch_name(rid) == "autoresearch/diff-t-schedule"
    assert rid in ei.worktree_dir(rid)
    assert ei.session_name(rid) == rid


def test_contract_ok_requires_matching_names():
    rid = "run7"
    ok, _ = ei.contract_ok(rid, "autoresearch/run7", "worktrees/run7", "run7")
    assert ok
    bad, why = ei.contract_ok(rid, "autoresearch/other", "worktrees/run7", "run7")
    assert not bad and "branch must be" in why
    bad, why = ei.contract_ok(rid, "autoresearch/run7", "worktrees/run7", "sess")
    assert not bad and "session must be" in why


def test_invalid_run_id_rejected():
    assert not ei.valid_run_id("-bad")
    assert not ei.valid_run_id("has space")
    assert ei.valid_run_id("m70=lr1e-4=s0")


def test_improved_respects_direction():
    assert ei.improved(0.4, 0.5, "minimize")        # lower is better
    assert not ei.improved(0.6, 0.5, "minimize")
    assert ei.improved(0.9, 0.8, "maximize")        # higher is better
    assert not ei.improved(0.7, 0.8, "maximize")
    assert not ei.improved("nan", 0.5, "minimize")  # bad value -> not improved


def test_no_stacking_on_failed_branch():
    failed = {"run3", "run5"}
    assert ei.is_failed_experiment_branch("autoresearch/run3", failed)
    assert not ei.is_failed_experiment_branch("autoresearch/run9", failed)
    assert not ei.is_failed_experiment_branch("main", failed)
