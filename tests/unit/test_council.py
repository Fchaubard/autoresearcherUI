"""Unit tests for backend.app.council — pure helpers + mocked LLMs."""
from __future__ import annotations


def test_strip_fences_no_fence(arui_env):
    from backend.app.council import _strip_fences
    assert _strip_fences("hello world") == "hello world"


def test_strip_fences_plain_backticks(arui_env):
    from backend.app.council import _strip_fences
    out = _strip_fences("```json\n{\"a\":1}\n```")
    assert out.startswith("{") and out.endswith("}")


def test_strip_fences_no_lang_label(arui_env):
    from backend.app.council import _strip_fences
    out = _strip_fences("```\n{\"x\":2}\n```")
    assert "{\"x\":2}" in out


def test_safe_parse_clean_json(arui_env):
    from backend.app.council import _safe_parse
    out = _safe_parse('{"verdict":"keep","learning":"good"}')
    assert out["verdict"] == "keep"
    assert out["learning"] == "good"
    # defaults are populated
    assert out["rerank_pending"] == []
    assert out["veto"] == []


def test_safe_parse_extracts_embedded_object(arui_env):
    from backend.app.council import _safe_parse
    out = _safe_parse(
        "I think the answer is: {\"verdict\":\"discard\"} ok?")
    assert out["verdict"] == "discard"


def test_safe_parse_garbage_returns_none(arui_env):
    from backend.app.council import _safe_parse
    assert _safe_parse("not json at all") is None


def test_safe_parse_returns_none_for_non_dict(arui_env):
    from backend.app.council import _safe_parse
    assert _safe_parse("[1, 2, 3]") is None


def test_agreement_matching_verdicts(arui_env):
    from backend.app.council import _agreement
    a = {"verdict": "keep", "rerank_pending": ["i1", "i2", "i3"],
         "veto": ["bad"]}
    b = {"verdict": "keep", "rerank_pending": ["i3", "i1", "i2"],
         "veto": ["bad"]}
    assert _agreement(a, b) is True


def test_agreement_different_verdicts(arui_env):
    from backend.app.council import _agreement
    a = {"verdict": "keep", "rerank_pending": [], "veto": []}
    b = {"verdict": "discard", "rerank_pending": [], "veto": []}
    assert _agreement(a, b) is False


def test_agreement_different_vetos(arui_env):
    from backend.app.council import _agreement
    a = {"verdict": "keep", "rerank_pending": [], "veto": ["x"]}
    b = {"verdict": "keep", "rerank_pending": [], "veto": ["y"]}
    assert _agreement(a, b) is False


def test_agreement_different_top3(arui_env):
    from backend.app.council import _agreement
    a = {"verdict": "keep", "rerank_pending": ["i1", "i2", "i3"], "veto": []}
    b = {"verdict": "keep", "rerank_pending": ["i4", "i5", "i6"], "veto": []}
    assert _agreement(a, b) is False


def test_settings_returns_defaults(arui_env):
    from backend.app.council import _settings, DEFAULTS
    s = _settings()
    for k, v in DEFAULTS.items():
        assert s[k] == v


def test_settings_merges_user_overrides(arui_env, setting_setter):
    from backend.app.council import _settings
    setting_setter("onboarding", {"council_gemini_model": "gemini-1.5-flash",
                                    "run_debate": False})
    s = _settings()
    assert s["council_gemini_model"] == "gemini-1.5-flash"
    assert s["run_debate"] is False


def test_available_reviewers_empty_without_keys(arui_env, monkeypatch):
    from backend.app.council import _available_reviewers, _settings
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert _available_reviewers(_settings()) == []


def test_available_reviewers_with_keys(arui_env, monkeypatch):
    from backend.app.council import _available_reviewers, _settings
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setenv("OPENAI_API_KEY", "y")
    s = _available_reviewers(_settings())
    assert "gemini" in s
    assert "openai" in s


def test_is_enabled_false_without_keys(arui_env, monkeypatch):
    from backend.app.council import is_enabled
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert is_enabled() is False


def test_claude_available(arui_env, monkeypatch):
    from backend.app.council import _claude_available, _settings
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    assert _claude_available(_settings()) is True


def test_review_async_no_reviewers(arui_env, monkeypatch):
    """If no reviewer keys, review_async returns False without launching."""
    from backend.app import council
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert council.review_async("anything") is False
