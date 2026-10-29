import json
import time
import urllib.error

import pytest


@pytest.mark.parametrize("provider,data,expected", [
    ("openai", {"usage": {"prompt_tokens": 100, "completion_tokens": 40,
      "prompt_tokens_details": {"cached_tokens": 80}, "completion_tokens_details": {"reasoning_tokens": 30}}},
     (100, 80, None, 40, 30)),
    ("openai", {"usage": {"input_tokens": 100, "output_tokens": 40,
      "input_tokens_details": {"cached_tokens": 70}, "output_tokens_details": {"reasoning_tokens": 20}}},
     (100, 70, None, 40, 20)),
    ("claude", {"usage": {"input_tokens": 10, "cache_read_input_tokens": 80,
      "cache_creation_input_tokens": 10, "output_tokens": 40}}, (100, 80, 10, 40, None)),
    ("claude", {"usage": {"input_tokens": 10, "output_tokens": 40,
      "output_tokens_details": {"thinking_tokens": 30}}}, (10, None, None, 40, 30)),
    ("gemini", {"usageMetadata": {"promptTokenCount": 100, "cachedContentTokenCount": 70,
      "candidatesTokenCount": 10, "thoughtsTokenCount": 30}}, (100, 70, None, 40, 30)),
    ("flint", {}, (None, None, None, None, None)),
])
def test_provider_usage_normalization_never_double_counts_reasoning(arui_env, provider, data, expected):
    from backend.app import advisory_usage
    result = advisory_usage.tokens(data, provider)
    assert tuple(result[k] for k in ["input_tokens", "cache_read_tokens", "cache_write_tokens",
                                    "output_tokens", "reasoning_tokens"]) == expected


def test_each_http_attempt_is_recorded_without_prompt_or_auth(arui_env, monkeypatch):
    from backend.app import council, advisory_usage
    class Response:
        status = 200
        headers = {"x-request-id": "req-good", "authorization": "secret-response"}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return json.dumps({"model": "gpt-6-astra", "usage": {"prompt_tokens": 1}}).encode()
    attempts = []
    def request(req, timeout):
        attempts.append(req)
        if len(attempts) == 1:
            raise urllib.error.HTTPError(req.full_url, 429, "secret-provider-error", {}, None)
        return Response()
    monkeypatch.setattr(council.urllib.request, "urlopen", request)
    monkeypatch.setattr(council, "_RETRY_DELAYS", (0,))
    with advisory_usage.context(role="reviewer2", operation="run_review", trigger_id="run-123"):
        council._post_json_retry("https://api.openai.com/v1/chat/completions",
            {"model": "gpt-6-astra", "reasoning_effort": "high", "messages": ["private-prompt"]},
            {"Authorization": "Bearer secret-key"})
    raw = (arui_env / "advisory_usage.jsonl").read_text()
    events = [json.loads(line) for line in raw.splitlines()]
    assert [e["attempt"] for e in events] == [1, 2]
    assert [e["http_status"] for e in events] == [429, 200]
    assert events[0]["error_type"] == "HTTPError" and events[0]["input_tokens"] is None
    assert events[1]["role"] == "reviewer2" and events[1]["effort"] == "high"
    assert events[1]["request_id"] == "req-good" and events[1]["trigger_id"] == "run-123"
    assert not any(secret in raw for secret in ["private-prompt", "secret-key", "secret-response", "secret-provider-error"])


def test_verification_traffic_and_missing_usage_are_explicit(arui_env, monkeypatch):
    from backend.app import advisory_usage
    monkeypatch.setenv("ARUI_VERIFICATION", "1")
    advisory_usage.record("https://generativelanguage.googleapis.com/v1beta/models/gemini-4-argon:generateContent",
                          {}, started=time.monotonic(), status=200)
    event = json.loads((arui_env / "advisory_usage.jsonl").read_text())
    assert event["traffic"] == "verification" and event["model"] == "gemini-4-argon"
    assert event["usage_reported"] is False and event["input_tokens"] is None


def test_accounting_write_failure_never_repeats_inference(arui_env, monkeypatch):
    from backend.app import council, advisory_usage
    calls = []
    class Response:
        status = 200
        headers = {}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return b'{"ok":true}'
    monkeypatch.setattr(council.urllib.request, "urlopen", lambda *a, **k: calls.append(1) or Response())
    def full_disk(*args):
        raise OSError("synthetic disk full")
    monkeypatch.setattr(advisory_usage.os, "open", full_disk)
    assert council._post_json_retry("https://api.openai.com/v1/responses", {"model": "gpt-6-astra"}, {}) == {"ok": True}
    assert calls == [1]


def test_service_tier_and_cache_write_lifetime_are_retained(arui_env):
    from backend.app import advisory_usage
    data = {"service_tier": "flex", "usage": {"input_tokens": 100,
            "input_tokens_details": {"cache_write_tokens": 80,
                "cache_write_tokens_details": {"ttl_30m_tokens": 30, "ttl_12h_tokens": 50}}}}
    advisory_usage.record("https://api.openai.com/v1/responses",
                          {"model": "gpt-6-astra", "service_tier": "auto"},
                          started=time.monotonic(), data=data, status=200)
    event = json.loads((arui_env / "advisory_usage.jsonl").read_text())
    assert event["requested_service_tier"] == "auto" and event["returned_service_tier"] == "flex"
    assert event["cache_write_tokens"] == 80
    assert event["cache_write_30m_tokens"] == 30 and event["cache_write_12h_tokens"] == 50
    missing = advisory_usage.tokens({}, "openai")
    assert missing["cache_write_30m_tokens"] is None and missing["cache_write_12h_tokens"] is None
