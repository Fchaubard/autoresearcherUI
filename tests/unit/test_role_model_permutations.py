"""Cross-provider role/model permutation regression matrix."""
from __future__ import annotations

import random

import pytest


ROLE_FIELDS = (
    "research_agent_model",
    "author_agent_model",
    "scoping_model",
    "council_gemini_model",
    "council_openai_model",
    "council_claude_model",
    "pi_agent_model",
)
MODELS = (
    "claude-opus-5",
    "claude-fable-5-1",
    "gpt-6-astra",
    "gpt-5.6-sol",
    "gpt-5.5",
    "gemini-3.8-flash",
    "gemini-3.1-pro-preview",
)


def _case(index: int) -> dict[str, str]:
    rng = random.Random(20260903 + index)
    return {field: rng.choice(MODELS) for field in ROLE_FIELDS}


@pytest.mark.parametrize("index", range(10))
def test_role_model_permutation(index, arui_env, monkeypatch):
    from backend.app import agent_cli, council, token_check
    from backend.app.model_registry import AGENT_CLI_MODELS, provider_for

    cfg = _case(index)
    # Every selected role model must resolve through the canonical registry.
    assert all(provider_for(model) in {"claude", "openai", "gemini"}
               for model in cfg.values())

    # Credential validators may choose any selected model for their provider,
    # but can never receive a model belonging to a different provider.
    for provider in ("claude", "openai", "gemini"):
        selected = token_check._model_for_provider(cfg, provider)
        assert provider_for(selected) == provider

    # Both autonomous coding roles must map to the matching provider CLI.
    expected_binary = {"claude": "claude", "openai": "codex",
                       "gemini": "gemini"}
    monkeypatch.setattr(agent_cli, "_binary",
                        lambda provider: expected_binary[provider])
    for field in ("research_agent_model", "author_agent_model"):
        model = cfg[field]
        assert model in AGENT_CLI_MODELS
        provider, command = agent_cli.command(model, "high", "read brief")
        assert expected_binary[provider] in command

    # Logical council slots are provider-agnostic and become available based
    # on the selected model's provider credential, not their legacy names.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    cfg.update({"council_enable_gemini": True,
                "council_enable_openai": True,
                "council_enable_claude_tiebreaker": True})
    assert council._available_reviewers(cfg) == ["gemini", "openai"]
    assert council._claude_available(cfg) is True

    # Scoping preserves the exact selected model by placing it in the logical
    # slot for its actual provider.
    scoped = council._with_scoping_model(cfg)
    provider = provider_for(cfg["scoping_model"])
    slot = {"claude": "council_claude_model",
            "openai": "council_openai_model",
            "gemini": "council_gemini_model"}[provider]
    assert scoped[slot] == cfg["scoping_model"]
