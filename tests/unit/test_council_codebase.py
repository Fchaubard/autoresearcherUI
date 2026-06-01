"""Regression tests for council._collect_codebase.

A real user (Francois, 2026-05-31) hit this in production:

    [council] code-bless rejected — no source files found in the
    agent workspace — the agent has not scaffolded anything yet

…even though the agent HAD scaffolded train.py, prepare.py,
program.md, ideas.md. The research agent itself diagnosed the bug:

    SKIP_DIRS was matched against the ABSOLUTE path. Every
    autoresearcherUI workspace lives under data/workspace/<name>/
    by setup convention, so 'data' was always present in p.parts and
    every file was skipped.

The fix matches SKIP_DIRS only against components relative to the
workspace. These tests pin that contract so the bug can't silently
come back.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def workspace_with_code(tmp_path):
    """Mimic the real layout: tmp_path stands in for
    /root/autoresearcherUI/data/workspace/diffusion-ensemble-researcher/

    We CREATE the literal ``data`` ancestor that triggered the bug —
    a parent named "data" — so a regression would skip every file
    inside our workspace exactly as it did in prod."""
    data = tmp_path / "data" / "workspace" / "my-project"
    data.mkdir(parents=True)
    (data / "train.py").write_text(
        "# the baseline trainer\nimport torch\nprint('hello')\n")
    (data / "prepare.py").write_text("# data prep\nimport os\n")
    (data / "ideas.md").write_text("| status | idea | what |\n")
    (data / "program.md").write_text("# project plan\n")
    # genuinely-skip-worthy junk inside the workspace
    (data / ".git").mkdir()
    (data / ".git" / "config").write_text("[core]")
    (data / "__pycache__").mkdir()
    (data / "__pycache__" / "x.pyc").write_text("binary garbage")
    return data


def test_collect_codebase_does_not_skip_files_under_ancestor_data(
        workspace_with_code):
    """The regression: every workspace under .../data/... had every
    file dropped because 'data' was an ancestor. Now matched
    relative-to-workspace only."""
    from backend.app.council import _collect_codebase
    blob = _collect_codebase(workspace_with_code)
    assert "train.py" in blob, (
        "train.py NOT collected — SKIP_DIRS is matching the ancestor "
        "'data' directory again. council._collect_codebase reverted "
        "to the pre-bugfix behavior. See test docstring.")
    assert "prepare.py" in blob
    assert "ideas.md" in blob
    assert "program.md" in blob


def test_collect_codebase_still_skips_real_workspace_subdirs(
        workspace_with_code):
    """Pin the OPPOSITE failure mode too — make sure the relative-path
    fix didn't accidentally start including .git/, __pycache__/, etc.
    that the SKIP_DIRS list still wants out."""
    from backend.app.council import _collect_codebase
    blob = _collect_codebase(workspace_with_code)
    assert ".git/config" not in blob, (
        "council included .git/config — SKIP_DIRS no longer filters "
        "real junk dirs.")
    assert "__pycache__/x.pyc" not in blob


def test_collect_codebase_empty_workspace_returns_empty(tmp_path):
    """Genuinely-empty workspace (no scaffold yet) returns ''.
    That's the ONLY case where the auto-reject 'no source files'
    should fire."""
    from backend.app.council import _collect_codebase
    empty = tmp_path / "data" / "workspace" / "fresh"
    empty.mkdir(parents=True)
    assert _collect_codebase(empty).strip() == ""


def test_collect_codebase_handles_nested_skip_dir_INSIDE_workspace(tmp_path):
    """A skip-dir directly inside the workspace should be respected
    (the original intent of SKIP_DIRS) — only the ANCESTOR match
    was the bug."""
    from backend.app.council import _collect_codebase
    ws = tmp_path / "data" / "workspace" / "p"
    ws.mkdir(parents=True)
    (ws / "main.py").write_text("import sys")
    junk = ws / "wandb" / "run-1"
    junk.mkdir(parents=True)
    (junk / "config.yaml").write_text("foo: bar")
    blob = _collect_codebase(ws)
    assert "main.py" in blob
    assert "wandb/run-1/config.yaml" not in blob
    assert "config.yaml" not in blob, (
        "wandb is in SKIP_DIRS; its contents must NOT be collected.")
