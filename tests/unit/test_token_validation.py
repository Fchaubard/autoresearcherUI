"""Full token validation: model visibility, advisor resolution, and robust
crash/timeout handling that returns structured failures (never hangs/raises)."""
import json

import backend.app.token_check as tc


class FakeResp:
    def __init__(self, status, payload):
        self.status = status
        self._data = json.dumps(payload).encode()
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self, *a): return self._data


def _urlopen(payload, status=200):
    return lambda req, timeout=None: FakeResp(status, payload)


# ── advisor resolution ──────────────────────────────────────────────────────
def test_advisor_only_claude_uses_claude():
    adv = tc.resolve_advisor({"claude_token": "x", "scoping_model": "gemini"})
    assert adv["provider"] == "claude"      # NOT a gemini default
    assert adv["warning"]                    # notes the switch


def test_advisor_configured_provider_with_key_wins():
    adv = tc.resolve_advisor({"gemini_token": "g", "claude_token": "c",
                              "scoping_model": "gemini"})
    assert adv["provider"] == "gemini"
    assert adv["warning"] == ""


def test_advisor_none_configured_warns():
    adv = tc.resolve_advisor({"scoping_model": "gemini"})
    assert "no advisor key" in adv["warning"]


# ── model visibility ────────────────────────────────────────────────────────
def test_model_visible_lenient_and_gemini_prefix():
    assert tc._model_visible("gemini-2.5-pro",
                             ["models/gemini-2.5-pro", "models/gemini-2.0-flash"])
    assert not tc._model_visible("gpt-5", ["claude-opus-4-6"])


def test_check_claude_model_visible(monkeypatch):
    monkeypatch.setattr(tc.urllib.request, "urlopen",
                        _urlopen({"data": [{"id": "claude-opus-4-6"}]}))
    r = tc.check_claude("key", "claude-opus-4-6")
    assert r["ok"] is True and r["model_ok"] is True


def test_check_claude_model_not_visible(monkeypatch):
    monkeypatch.setattr(tc.urllib.request, "urlopen",
                        _urlopen({"data": [{"id": "claude-haiku-4-5"}]}))
    r = tc.check_claude("key", "claude-opus-4-6")
    assert r["ok"] is False and r["model_ok"] is False
    assert "not visible" in r["detail"]


def test_check_claude_fable_alias_always_ok(monkeypatch):
    # /v1/models does not list the 'fable' alias; it must still validate.
    monkeypatch.setattr(tc.urllib.request, "urlopen",
                        _urlopen({"data": [{"id": "claude-opus-4-6"}]}))
    r = tc.check_claude("key", "fable")
    assert r["ok"] is True and r["model_ok"] is True


def test_check_openai_model_visible(monkeypatch):
    monkeypatch.setattr(tc.urllib.request, "urlopen",
                        _urlopen({"data": [{"id": "gpt-5"}, {"id": "o3"}]}))
    assert tc.check_openai("k", "gpt-5")["model_ok"] is True


# ── check_all robustness ────────────────────────────────────────────────────
def test_check_all_empty_skips_and_has_advisor():
    out = tc.check_all({})
    for name in ("claude", "openai", "gemini", "github", "gmail"):
        assert out[name].get("skipped") is True
    assert "advisor" in out
    assert tc.blocking_failures(out) == []


def test_check_all_crash_is_structured(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(tc, "check_claude", _boom)
    out = tc.check_all({"claude_token": "x"})
    assert out["claude"]["ok"] is False
    assert "crash" in out["claude"]["detail"]


def test_blocking_failures_ignores_skipped_and_advisor():
    results = {
        "claude": {"ok": True, "skipped": True},
        "openai": {"ok": False, "detail": "401"},
        "advisor": {"provider": "claude"},
    }
    assert tc.blocking_failures(results) == ["openai"]
