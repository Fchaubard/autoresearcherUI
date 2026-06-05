"""Unit tests for backend.app.settings_kv (PR 7 of state-control
rewrite, 2026-06-05).

A tiny typed surface around the Setting key/value table. The point is
that new code stops re-typing ``db.query(Setting).filter(...)`` over
and over, and uses namespaced keys instead of overloading the
``onboarding`` mega-dict.
"""
from __future__ import annotations

import pytest


def test_get_returns_default_when_missing(arui_env):
    from backend.app import settings_kv
    assert settings_kv.get("watchdog.does_not_exist") is None
    assert settings_kv.get("watchdog.does_not_exist", default={}) == {}


def test_set_and_get_roundtrip(arui_env):
    from backend.app import settings_kv
    settings_kv.set("orchestrator.phase",
                     {"phase": "planning", "at": "2026-06-05T00:00:00Z",
                      "detail": {"idea_id": "x"}})
    val = settings_kv.get("orchestrator.phase")
    assert val["phase"] == "planning"
    assert val["detail"] == {"idea_id": "x"}


def test_set_overwrites_existing(arui_env):
    from backend.app import settings_kv
    settings_kv.set("health.idle_since", {"since": "2026-06-05T00:00:00Z"})
    settings_kv.set("health.idle_since", {"since": "2026-06-05T01:00:00Z"})
    assert (settings_kv.get("health.idle_since")
            == {"since": "2026-06-05T01:00:00Z"})


def test_delete_removes_row(arui_env):
    from backend.app import settings_kv
    settings_kv.set("watchdog.config", {"no_metric_flow": {"enabled": False}})
    assert settings_kv.delete("watchdog.config") is True
    assert settings_kv.get("watchdog.config") is None
    # Idempotent: deleting a missing key returns False, doesn't raise.
    assert settings_kv.delete("watchdog.config") is False


def test_list_keys_with_prefix(arui_env):
    """``list_keys(prefix='watchdog.')`` should return only the watchdog
    namespace, sorted, so the Settings introspection endpoint can render
    grouped sections."""
    from backend.app import settings_kv
    settings_kv.set("watchdog.config", {})
    settings_kv.set("watchdog.reviewed_at", {})
    settings_kv.set("pi.enabled", True)
    settings_kv.set("orchestrator.phase", {"phase": "bootstrap"})
    wd = settings_kv.list_keys(prefix="watchdog.")
    assert wd == ["watchdog.config", "watchdog.reviewed_at"]
    assert "pi.enabled" not in wd


def test_list_keys_no_prefix_returns_all(arui_env):
    from backend.app import settings_kv
    settings_kv.set("a", 1)
    settings_kv.set("b", 2)
    all_keys = settings_kv.list_keys()
    assert "a" in all_keys
    assert "b" in all_keys
