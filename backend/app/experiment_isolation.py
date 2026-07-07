"""Experiment isolation contract.

Every substantive idea runs in FULL isolation so a failed experiment can never
corrupt ``main`` or get stacked on:

  - its own git BRANCH   ``autoresearch/<run_id>``
  - its own git WORKTREE ``<repo>/../worktrees/<run_id>``
  - its own tmux SESSION ``<run_id>`` (launched via ``arun``/``aidea``)

Branch, worktree, session and run all share the SAME ``<run_id>``. The branch is
committed + pushed before real runs (after smoke tests). It is merged back into
``main`` ONLY if the run improves the project's core metric per the metric
direction; a failed/non-improving experiment leaves ``main`` untouched, records
the lesson, and the branch is removed/archived. New hypotheses are NEVER stacked
on a failed experiment branch - each idea forks fresh from ``main``.

This module holds the pure naming + validation helpers so the contract is
enforceable and testable; the agent follows the same rules via its prompt and
the ``aidea`` launcher.
"""
from __future__ import annotations

import re

BRANCH_PREFIX = "autoresearch/"
_RUN_ID = re.compile(r"^[A-Za-z0-9_.\-=]+$")


def valid_run_id(run_id: str) -> bool:
    return bool(run_id) and len(run_id) <= 80 and bool(_RUN_ID.match(run_id)) \
        and not run_id.startswith("-")


def branch_name(run_id: str) -> str:
    return BRANCH_PREFIX + run_id


def worktree_dir(run_id: str) -> str:
    """Worktree lives OUTSIDE the main checkout (a sibling ``worktrees/`` dir)
    so it never pollutes ``main``'s working tree."""
    return f"worktrees/{run_id}"


def session_name(run_id: str) -> str:
    return run_id


def contract_ok(run_id: str, branch: str, worktree: str,
                session: str) -> tuple[bool, str]:
    """Verify branch / worktree / session all share the run id (the naming
    invariant). Returns ``(ok, reason)``."""
    if not valid_run_id(run_id):
        return False, f"invalid run id {run_id!r}"
    if branch != branch_name(run_id):
        return False, (f"branch must be {branch_name(run_id)!r}, "
                       f"got {branch!r}")
    if run_id not in worktree:
        return False, f"worktree {worktree!r} must contain the run id"
    if session != session_name(run_id):
        return False, f"session must be {run_id!r}, got {session!r}"
    return True, ""


def improved(new_metric: float, baseline_metric: float,
             direction: str) -> bool:
    """Did the experiment improve the project's core metric? Merge-to-main
    gate. ``direction`` is 'minimize' or 'maximize'."""
    try:
        n, b = float(new_metric), float(baseline_metric)
    except (TypeError, ValueError):
        return False
    if (direction or "minimize").lower().startswith("max"):
        return n > b
    return n < b


def is_failed_experiment_branch(branch: str, failed_run_ids: set[str]) -> bool:
    """True iff ``branch`` belongs to a failed experiment - used to REFUSE
    stacking a new hypothesis on top of it."""
    if not branch.startswith(BRANCH_PREFIX):
        return False
    return branch[len(BRANCH_PREFIX):] in (failed_run_ids or set())
