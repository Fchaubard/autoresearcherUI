"""Unit tests for watchdog.onboarding.review_with_council (PR 5 of the
state-control rewrite, 2026-06-05).

The reviewer asks the council whether the ship-default watchdog params
make sense for the project's research purpose, and persists any
overrides. These tests pin:

  * skips cleanly when no Project exists
  * skips when already reviewed (unless force=True)
  * validates that only known script names + dict-shaped overrides are
    persisted
  * stamps the review even when the agent returned no overrides ({} is
    a valid "defaults are fine" verdict)
"""
from __future__ import annotations

import pytest


def test_skipped_without_project(arui_env):
    from backend.app.watchdog import onboarding
    out = onboarding.review_with_council()
    assert out["status"] == "skipped"
    assert "no onboarded" in out["reason"]


def test_skipped_when_already_reviewed(arui_env, db_session, make_project,
                                          monkeypatch):
    from backend.app.watchdog import onboarding
    from backend.app.models import Setting
    make_project(purpose="train a tiny CNN on cifar-10")
    db_session.add(Setting(key="watchdog.reviewed_at",
                            value={"at": "2026-01-01T00:00:00Z"}))
    db_session.commit()
    out = onboarding.review_with_council()
    assert out["status"] == "skipped"
    assert "already reviewed" in out["reason"]


def test_applied_when_council_returns_valid_overrides(
        arui_env, db_session, make_project, monkeypatch):
    """Reviewer returns one valid override → config updated, marker set."""
    from backend.app import council
    from backend.app.watchdog import onboarding, config as wd_cfg
    from backend.app.models import Setting
    make_project(purpose="extra-long LLM eval taking 4h per validation")

    monkeypatch.setenv("OPENAI_API_KEY", "synthetic")
    monkeypatch.setattr(council, "configured_reviewers",
                         lambda cfg: ["openai"])
    monkeypatch.setattr(council, "_settings", lambda: {})
    monkeypatch.setattr(council, "_call_reviewer",
        lambda r, sys, user, cfg: {
            "no_metric_flow": {"params": {"timeout_sec": 18000}},
            "junk_key_that_does_not_exist": {"params": {"x": 1}},
        })
    out = onboarding.review_with_council()
    assert out["status"] == "applied"
    assert "no_metric_flow" in out["overrides"]
    assert "junk_key_that_does_not_exist" not in out["overrides"]
    cfg = wd_cfg.get_config()
    assert cfg["no_metric_flow"]["params"]["timeout_sec"] == 18000
    marker = (db_session.query(Setting)
              .filter(Setting.key == "watchdog.reviewed_at").first())
    assert marker is not None


def test_applied_with_empty_overrides_still_stamps_marker(
        arui_env, db_session, make_project, monkeypatch):
    """Reviewer returning {} ('defaults are fine') still counts as a
    review — we don't ask again until force=True."""
    from backend.app import council
    from backend.app.watchdog import onboarding
    from backend.app.models import Setting
    make_project(purpose="boring vanilla MNIST classifier")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic")
    monkeypatch.setattr(council, "configured_reviewers",
                         lambda cfg: ["openai"])
    monkeypatch.setattr(council, "_settings", lambda: {})
    monkeypatch.setattr(council, "_call_reviewer",
                         lambda *a, **k: {})
    out = onboarding.review_with_council()
    assert out["status"] == "applied"
    assert out["overrides"] == {}
    marker = (db_session.query(Setting)
              .filter(Setting.key == "watchdog.reviewed_at").first())
    assert marker is not None


def test_force_runs_again_after_marker(arui_env, db_session, make_project,
                                          monkeypatch):
    from backend.app import council
    from backend.app.watchdog import onboarding
    from backend.app.models import Setting
    make_project(purpose="something")
    db_session.add(Setting(key="watchdog.reviewed_at",
                            value={"at": "2026-01-01T00:00:00Z"}))
    db_session.commit()
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic")
    monkeypatch.setattr(council, "configured_reviewers",
                         lambda cfg: ["openai"])
    monkeypatch.setattr(council, "_settings", lambda: {})
    monkeypatch.setattr(council, "_call_reviewer",
                         lambda *a, **k: {"diverging": {"enabled": False}})
    out = onboarding.review_with_council(force=True)
    assert out["status"] == "applied"
    assert "diverging" in out["overrides"]


def test_selected_reviewer_failure_never_calls_other_model(arui_env, make_project, monkeypatch):
    from backend.app import council
    from backend.app.watchdog import onboarding
    make_project(purpose="synthetic task")
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic")
    seen = []
    monkeypatch.setattr(council, "_call_reviewer", lambda reviewer, *args: seen.append(reviewer))
    result = onboarding.review_with_council()
    assert result["status"] == "error"
    assert result["model"] == council.DEFAULTS["council_gemini_model"]
    assert seen == ["gemini"]


def test_missing_selected_key_never_uses_other_configured_key(arui_env, make_project, monkeypatch):
    from backend.app import council
    from backend.app.watchdog import onboarding
    make_project(purpose="synthetic task")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic")
    monkeypatch.setattr(council, "_call_reviewer", lambda *args: pytest.fail("must not substitute the other reviewer"))
    result = onboarding.review_with_council()
    assert result["status"] == "error"
    assert result["model"] == council.DEFAULTS["council_gemini_model"]


def test_unreadable_settings_report_failure_without_selecting_defaults(arui_env, make_project, monkeypatch):
    from backend.app import council
    from backend.app.watchdog import onboarding
    make_project(purpose="synthetic task")
    def unavailable():
        raise OSError("synthetic credential-bearing database failure")
    monkeypatch.setattr(council, "_settings", unavailable)
    monkeypatch.setattr(council, "_call_reviewer", lambda *args: pytest.fail("must not call a default model"))
    result = onboarding.review_with_council()
    assert result["status"] == "error"
    assert "Cannot load the selected watchdog reviewer" in result["reason"]
    assert "credential-bearing" not in result["reason"]
