"""Unit tests for the seed-task gate (RESEARCH_IMPROVEMENT_PLAN #7)."""
from __future__ import annotations

import pytest


def test_seed_task_blockers_none_for_empty_meta(arui_env):
    from backend.app import council
    assert council.seed_task_blockers(None) == []
    assert council.seed_task_blockers({}) == []


def test_seed_task_blockers_small_val_set(arui_env):
    from backend.app import council
    out = council.seed_task_blockers({"val_set_size": 50})
    assert out and "validation set" in out[0]


def test_seed_task_blockers_small_val_ok_when_smoke_marked(arui_env):
    from backend.app import council
    out = council.seed_task_blockers({"val_set_size": 10,
                                       "dataset_kind": "smoke"})
    assert out == []


def test_seed_task_blockers_small_val_ok_via_program_md_marker(arui_env):
    from backend.app import council
    out = council.seed_task_blockers({"val_set_size": 5,
                                       "program_md_marks_smoke": True})
    assert out == []


def test_seed_task_blockers_train_30s_flag_triggers(arui_env):
    from backend.app import council
    out = council.seed_task_blockers({"val_set_size": 200,
                                       "train_30s": True})
    assert out and "30s" in out[0]


def test_seed_task_blockers_both_signals_listed(arui_env):
    from backend.app import council
    out = council.seed_task_blockers({"val_set_size": 50,
                                       "train_30s": True})
    assert len(out) == 2


def test_seed_task_blockers_large_val_set_passes(arui_env):
    from backend.app import council
    assert council.seed_task_blockers(
        {"val_set_size": 1000, "train_30s": False}) == []


def test_bless_async_short_circuits_on_seed_task(arui_env, monkeypatch,
                                                   setting_setter):
    """The /api/council/bless path must hit the seed-task gate before
    burning council tokens."""
    from backend.app import council
    # Bypass the preflight requirement (it'd normally block first).
    monkeypatch.setattr(council, "preflight_blocking_reasons", lambda: [])
    out = council.bless_async("/tmp/whatever",
                                bless_meta={"val_set_size": 10,
                                             "train_30s": True})
    st = council.bless_status()
    assert st["status"] == "rejected"
    assert st.get("seed_task_gate") is True
    assert len(st["blockers"]) >= 1
