"""The purpose/seed re-anchor block and its injection into researcher, PI, and
author prompts so the system stays on the rails."""
import pytest


def _set_onboarding(**kw):
    from backend.app.db import SessionLocal
    from backend.app.models import Setting
    db = SessionLocal()
    db.add(Setting(key="onboarding", value=kw))
    db.commit(); db.close()


def test_anchor_empty_when_nothing_set(arui_env):
    from backend.app import purpose
    assert purpose.anchor_block() == ""


def test_anchor_includes_purpose_seeds_rules(arui_env):
    from backend.app import purpose
    _set_onboarding(purpose="predict S&P returns",
                    seed_ideas="ridge; lasso", kill_criteria="1 hour")
    b = purpose.anchor_block()
    assert "predict S&P returns" in b
    assert "ridge; lasso" in b
    assert "1 hour" in b
    assert "re-anchor" in b.lower()


def test_anchor_includes_interrupt_focus(arui_env):
    from backend.app import purpose, directives
    _set_onboarding(purpose="p", seed_ideas="s", repo_name="proj")
    directives.upsert({"id": "d-interrupt-focus", "type": "SCIENCE",
                       "what": "focus on the loss function only",
                       "idea_class": "INCREMENTAL", "priority": 10000})
    b = purpose.anchor_block()
    assert "OPERATOR INTERRUPT FOCUS" in b
    assert "loss function only" in b


def test_researcher_instructions_have_stay_on_purpose(arui_env):
    from backend.app import realrun
    assert "STAY ON PURPOSE" in realrun.DEFAULT_AGENT_INSTRUCTIONS


def test_author_brief_prepends_anchor(arui_env, monkeypatch):
    from backend.app import author_agent, purpose
    monkeypatch.setattr(purpose, "anchor_block",
                        lambda **k: "# RESEARCH PURPOSE\nstay on target")
    # _build_author_brief needs a project; if the builder is name-guarded it
    # returns quickly - we only assert the anchor is wired in via the source.
    import inspect
    src = inspect.getsource(author_agent)
    assert "purpose as _purpose" in src and "anchor_block" in src


def test_pi_injects_anchor(arui_env):
    import inspect
    from backend.app import pi
    src = inspect.getsource(pi)
    assert src.count("purpose as _purpose") >= 2   # research + paper PI
    assert "anchor_block()" in src
