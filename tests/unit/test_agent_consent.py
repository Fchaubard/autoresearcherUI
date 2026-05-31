"""Unit tests for RealAgent's Claude Code consent / API-key handling.

The poll-based auto-accept script (in agent.py) and the
_ensure_claude_settings() helper are the two places that determine
whether a fresh Claude Code install boots cleanly or gets stuck on a
consent screen. These tests pin down both.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_api_key_truncation_matches_claude_code_format():
    """Claude Code shows API keys as ``<first-7>...<last-20>``. Our
    truncation must match that exact format because the
    ``customApiKeyResponses.approved`` field is matched literally."""
    from backend.app.agent import RealAgent
    key = "sk-ant-api03-" + "X" * 80 + "Vx9jKu6kGQw-kUHWKQAA"
    trunc = RealAgent._api_key_truncation(key)
    assert trunc.startswith("sk-ant-")
    assert trunc.endswith("Vx9jKu6kGQw-kUHWKQAA")
    assert "..." in trunc
    # Format: 7 chars + "..." + 20 chars  → 30 chars total
    assert len(trunc) == 7 + 3 + 20


def test_api_key_truncation_short_key_returned_as_is():
    """Anything too short to truncate meaningfully is returned verbatim
    — the poll-based handler will catch the dialog anyway."""
    from backend.app.agent import RealAgent
    assert RealAgent._api_key_truncation("") == ""
    assert RealAgent._api_key_truncation("short") == "short"


def test_ensure_claude_settings_writes_apikey_helper_and_approval(tmp_path,
                                                                   monkeypatch):
    """Calling _ensure_claude_settings(key) should populate both
    ~/.claude.json and ~/.claude/settings.json with the apiKeyHelper,
    consent flags, AND the customApiKeyResponses.approved truncation
    so the "Use this API key?" prompt is skipped."""
    monkeypatch.setenv("HOME", str(tmp_path))
    from backend.app.agent import RealAgent
    key = "sk-ant-api03-" + "Y" * 80 + "Vx9jKu6kGQw-kUHWKQAA"
    RealAgent._ensure_claude_settings(key)
    for rel in (".claude.json", ".claude/settings.json"):
        cfg = json.loads((tmp_path / rel).read_text())
        assert cfg["apiKeyHelper"] == "printenv ANTHROPIC_API_KEY"
        assert cfg["bypassPermissionsModeAccepted"] is True
        assert cfg["hasTrustDialogAccepted"] is True
        # The "Use this API key?" dialog is skipped via this exact field.
        car = cfg["customApiKeyResponses"]
        assert isinstance(car["approved"], list)
        assert RealAgent._api_key_truncation(key) in car["approved"]
        assert car["rejected"] == []


def test_ensure_claude_settings_merges_existing_approvals(tmp_path, monkeypatch):
    """If the user already has approved keys on disk (from a prior
    manual click), the merge must preserve them, not clobber."""
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg_path = tmp_path / ".claude.json"
    cfg_path.write_text(json.dumps({
        "customApiKeyResponses": {
            "approved": ["sk-ant-...PRIOR_KEY_HASH"],
            "rejected": [],
        }
    }))
    (tmp_path / ".claude").mkdir(exist_ok=True)
    (tmp_path / ".claude" / "settings.json").write_text("{}")
    from backend.app.agent import RealAgent
    key = "sk-ant-api03-" + "Z" * 80 + "NEWNEWNEWNEWNEWNEWNE"
    RealAgent._ensure_claude_settings(key)
    cfg = json.loads(cfg_path.read_text())
    approved = cfg["customApiKeyResponses"]["approved"]
    # Both prior and new truncations are present.
    assert "sk-ant-...PRIOR_KEY_HASH" in approved
    assert RealAgent._api_key_truncation(key) in approved


def test_ensure_claude_settings_no_key_skips_approval(tmp_path, monkeypatch):
    """Called with no key (e.g. backend startup before onboarding),
    don't write customApiKeyResponses — there's nothing to approve."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from backend.app.agent import RealAgent
    RealAgent._ensure_claude_settings("")
    cfg = json.loads((tmp_path / ".claude.json").read_text())
    assert "customApiKeyResponses" not in cfg
    # But the rest is still pre-written so consent dialogs are pre-accepted.
    assert cfg["apiKeyHelper"] == "printenv ANTHROPIC_API_KEY"
    assert cfg["bypassPermissionsModeAccepted"] is True


def test_consent_script_handles_all_three_dialogs():
    """The poll script embedded in RealAgent.start() must contain
    detection branches for ALL THREE Claude Code consent dialogs:
      (a) 'use this API key' — type 1 (Yes; default is No,recommended)
      (b) 'trust this folder' — Enter (default is Yes)
      (c) 'Bypass Permissions' / 'Yes, I accept' — type 2 (default is No,exit)
    Regression test for the 'Use this API key?' dialog Francois hit
    on 2026-05-31."""
    # Read the agent.py source and confirm each branch is present.
    src = (Path(__file__).resolve().parents[2]
           / "backend" / "app" / "agent.py").read_text()
    assert "use *this *API *key" in src
    assert "trust *this *folder" in src
    assert "Bypass *Permissions" in src
    # And the dialog-specific keystroke for each:
    assert "sent_apikey" in src
    assert "sent_trust" in src
    assert "sent_bypass" in src
