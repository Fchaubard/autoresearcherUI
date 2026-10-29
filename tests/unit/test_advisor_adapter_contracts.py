"""Provider response formats and exact selected-model failure behavior."""
import io
import urllib.error

import pytest


def test_o3_pro_uses_responses_and_reads_visible_message(arui_env, monkeypatch):
    from backend.app import council
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    calls = []
    def post(url, body, headers):
        calls.append((url, body))
        return {"output": [{"type": "reasoning", "summary": []},
                           {"type": "message", "content": [
                               {"type": "output_text", "text": '{"concerns":"OK.","messages":[]}'}]}]}
    monkeypatch.setattr(council, "_post_json_retry", post)
    assert council._safe_parse(council._call_model("o3-pro", "max", "system", "user"))["concerns"] == "OK."
    assert calls[0][0].endswith("/responses")
    assert calls[0][1]["model"] == "o3-pro"
    assert calls[0][1]["reasoning"]["effort"] == "medium"
    assert "response_format" not in calls[0][1]
    assert calls[0][1]["text"]["format"]["type"] == "json_object"
    assert calls[0][1]["input"] == [{"role": "developer", "content": "system"},
                                    {"role": "user", "content": "user"}]
    assert "instructions" not in calls[0][1]


@pytest.mark.parametrize("model", ["gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"])
def test_max_effort_uses_responses_without_changing_model(arui_env, monkeypatch, model):
    from backend.app import council
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    calls = []
    def post(url, body, headers):
        calls.append((url, body))
        return {"output": [{"type": "message", "content": [
            {"type": "output_text", "text": '{"ok":true}'}]}]}
    monkeypatch.setattr(council, "_post_json_retry", post)
    assert council._call_model(model, "max", "Return JSON.", "Check 1+1.") == '{"ok":true}'
    assert len(calls) == 1
    url, body = calls[0]
    assert url.endswith("/responses")
    assert body["model"] == model
    assert body["reasoning"]["effort"] == "max"
    assert body["input"][0] == {"role": "developer", "content": "Return JSON."}
    assert body["text"]["format"]["type"] == "json_object"


def test_o3_pro_scope_chat_uses_responses_without_json_mode(arui_env, monkeypatch):
    from backend.app import council
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    def post(url, body, headers):
        assert url.endswith("/responses") and "text" not in body
        return {"output": [{"type": "message", "content": [
            {"type": "output_text", "text": "Run a seed replication."}]}]}
    monkeypatch.setattr(council, "_post_json_retry", post)
    assert council._call_text_model("o3-pro", "system", "user") == "Run a seed replication."


@pytest.mark.parametrize("provider,model", [("gemini", "gemini-4-argon"), ("claude", "claude-opus-5")])
def test_adapters_skip_thought_blocks_and_join_visible_text(arui_env, monkeypatch, provider, model):
    from backend.app import council
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    response = ({"candidates": [{"content": {"parts": [
        {"thought": True, "text": "hidden"}, {"text": '{"ok":'}, {"text": 'true}'}]}}]}
        if provider == "gemini" else {"content": [
            {"type": "thinking", "thinking": "hidden"},
            {"type": "text", "text": '{"ok":'}, {"type": "text", "text": 'true}'}]})
    monkeypatch.setattr(council, "_post_json_retry", lambda *args: response)
    assert council._call_model(model, "low", "system", "user") == '{"ok":true}'


@pytest.mark.parametrize("model,field", [("gemini-4-argon", "gemini"), ("gpt-5-mini", "openai")])
def test_pi_normalizes_stale_effort_through_shared_adapter(arui_env, monkeypatch, model, field):
    from backend.app import council, pi
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(pi, "_settings", lambda: {"pi_agent_effort": "max"})
    calls = []
    monkeypatch.setattr(council, "_call_" + field + "_api", lambda model, effort, *args: calls.append((model, effort)) or '{"concerns":"OK.","messages":[]}')
    assert council._safe_parse(pi._call(model, pi.SYSTEM, "synthetic"))["concerns"] == "OK."
    assert calls == [(model, "medium")]


@pytest.mark.parametrize("function", ["scope_review", "scope_finalize", "scope_chat"])
def test_scoping_selected_provider_failure_never_calls_fallback(arui_env, monkeypatch, function):
    from backend.app import council
    for key in ("GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(key, "test-key")
    cfg = dict(council.DEFAULTS, scoping_model="gemini-4-argon")
    monkeypatch.setattr(council, "_settings", lambda: cfg)
    calls = []
    def fail(model, *args, **kwargs):
        calls.append(model)
        raise urllib.error.HTTPError("https://provider.invalid", 429, "limited", {}, io.BytesIO(b"secret should not appear"))
    monkeypatch.setattr(council, "_call_model", fail)
    monkeypatch.setattr(council, "_call_text_model", fail)
    args = ("purpose", "metric", "seed", [])
    if function == "scope_finalize":
        args += ({}, [])
    if function == "scope_chat":
        args = ([], {}, [], "purpose", "metric", "seed")
    with pytest.raises(RuntimeError, match="gemini-4-argon") as exc:
        getattr(council, function)(*args)
    assert "secret" not in str(exc.value)
    assert "429" in str(exc.value)
    assert calls == ["gemini-4-argon"]


def test_configured_reviewers_does_not_drop_missing_credentials(arui_env):
    from backend.app import council
    assert council.configured_reviewers(council.DEFAULTS) == ["gemini", "openai"]
    assert council.configured_reviewers(dict(council.DEFAULTS, council_enable_openai=False)) == ["gemini"]


def test_legacy_scoping_provider_alias_is_explicit_and_credential_independent(arui_env, monkeypatch):
    from backend.app import council
    monkeypatch.setenv("GEMINI_API_KEY", "available-other-provider")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = council._with_scoping_model(dict(council.DEFAULTS, scoping_model="claude"))
    assert cfg["scoping_model"] == "claude-opus-5"
    assert cfg["council_claude_model"] == "claude-opus-5"
    assert council._scoping_order(cfg) == ["claude"]


def test_failed_reviewer_cannot_become_single_reviewer_consensus(arui_env, monkeypatch):
    from backend.app import council
    monkeypatch.setattr(council, "_build_context", lambda _: {})
    monkeypatch.setattr(council, "_ctx_text", lambda _: "synthetic")
    calls = []
    def review(slot, *args):
        calls.append(slot)
        return {"verdict": "kept", "rerank_pending": [], "veto": []} if slot == "gemini" else None
    monkeypatch.setattr(council, "_call_reviewer", review)
    assert council.deliberate("synthetic") is None
    # Empty context is rejected before invocation, then a concrete fixture
    # exercises both configured participants without any live model.
    monkeypatch.setattr(council, "_build_context", lambda _: {"test": True})
    assert council.deliberate("synthetic") is None
    assert set(calls) == {"gemini", "openai"}


def test_failed_debate_turn_does_not_hold_stale_position(arui_env, monkeypatch):
    from backend.app import council
    monkeypatch.setattr(council, "_build_context", lambda _: {"test": True})
    monkeypatch.setattr(council, "_ctx_text", lambda _: "synthetic")
    counts = {"gemini": 0, "openai": 0}
    def review(slot, *args):
        assert slot != "claude"
        counts[slot] += 1
        if slot == "openai" and counts[slot] == 2:
            return None
        return {"verdict": "kept" if slot == "gemini" else "discarded", "rerank_pending": [], "veto": []}
    monkeypatch.setattr(council, "_call_reviewer", review)
    assert council.deliberate("synthetic") is None
    assert counts == {"gemini": 2, "openai": 2}


def test_failed_required_tiebreaker_does_not_select_first_reviewer(arui_env, monkeypatch):
    from backend.app import council
    monkeypatch.setattr(council, "_settings", lambda: dict(council.DEFAULTS, run_debate=False))
    monkeypatch.setattr(council, "_build_context", lambda _: {"test": True})
    monkeypatch.setattr(council, "_ctx_text", lambda _: "synthetic")
    calls = []
    def review(slot, *args):
        calls.append(slot)
        return None if slot == "claude" else {"verdict": slot, "rerank_pending": [], "veto": []}
    monkeypatch.setattr(council, "_call_reviewer", review)
    assert council.deliberate("synthetic") is None
    assert calls[-1] == "claude"


def test_strategic_review_does_not_pick_available_peer(arui_env, monkeypatch):
    from backend.app import council
    monkeypatch.setattr(council, "_available_reviewers", lambda cfg: ["gemini"])
    monkeypatch.setattr(council, "_build_strategic_context", lambda _: {"test": True})
    monkeypatch.setattr(council, "_compute_strategic_state", lambda: {"consecutive_unimplemented_count": 0})
    monkeypatch.setattr(council, "_strategic_ctx_text", lambda *args, **kwargs: "synthetic")
    calls = []
    monkeypatch.setattr(council, "_call_reviewer", lambda slot, *args: calls.append(slot))
    assert council.strategic_review(["synthetic"]) is None
    assert calls == ["openai"]


def test_bless_missing_required_reviewer_is_incomplete(arui_env, monkeypatch):
    from backend.app import council
    monkeypatch.setattr(council, "_collect_codebase", lambda _: "x = 1")
    monkeypatch.setattr(council, "_call_reviewer", lambda slot, *args: {"approved": True} if slot == "gemini" else None)
    council._bless_worker("unused")
    result = council._bless_state_get()
    assert result["status"] == "error"
    assert result["missing_reviewers"] == ["openai"]
    assert result["incomplete"] is True
    assert council.is_code_blessed() is False


def test_missing_keys_and_legacy_auto_approval_do_not_open_code_gate(arui_env):
    from backend.app import council
    assert council.is_code_blessed() is False
    council._bless_state_set({"status": "approved", "auto": True})
    assert council.is_code_blessed() is False
    council._bless_state_set({"status": "approved", "verdicts": {"gemini": {"approved": True}}})
    assert council.is_code_blessed() is True


def test_explicit_both_reviewers_disabled_is_visible_optout(arui_env, setting_setter, monkeypatch):
    from backend.app import council
    assert council.configured_reviewers({}) == ["gemini", "openai"]
    assert council.is_code_blessed() is False
    setting_setter("onboarding", {"council_enable_gemini": False,
                                  "council_enable_openai": False})
    assert council.is_code_blessed() is True
    council._bless_worker("unused")
    assert council._bless_state_get()["status"] == "skipped"
    assert council._bless_state_get()["reason"] == "reviewers_explicitly_disabled"
    entered = []
    monkeypatch.setattr(council, "_auto_enter_paper_after_approval", lambda *args: entered.append(args))
    council._completion_review_worker([], "synthetic", "synthetic", "WRITE_PAPER")
    result = council._conclusion_state_get()
    assert result["status"] == "skipped"
    assert result["council_verdict"]["verdict"] == "SKIPPED"
    assert not entered


def test_completion_missing_reviewer_never_auto_enters_paper(arui_env, monkeypatch):
    from backend.app import council
    monkeypatch.setattr(council, "_build_completion_review_context", lambda *args: {"test": True})
    monkeypatch.setattr(council, "_call_reviewer", lambda slot, *args: {"verdict": "APPROVED"} if slot == "gemini" else None)
    entered = []
    monkeypatch.setattr(council, "_auto_enter_paper_after_approval", lambda *args: entered.append(args))
    council._completion_review_worker([], "synthetic", "synthetic", "WRITE_PAPER")
    result = council._conclusion_state_get()
    assert result["status"] == "error" and result["incomplete"] is True
    assert result["council_verdict"]["missing_reviewers"] == ["openai"]
    assert not entered


@pytest.mark.parametrize("approval", ["false", "true", 0, 1, None])
def test_bless_malformed_approval_never_becomes_truthy_approval(arui_env, monkeypatch, approval):
    from backend.app import council
    monkeypatch.setattr(council, "_collect_codebase", lambda _: "x = 1")
    monkeypatch.setattr(council, "_call_reviewer", lambda slot, *args: {
        "approved": approval if slot == "gemini" else True,
        "blockers": [], "suggestions": []})
    council._bless_worker("unused")
    result = council._bless_state_get()
    assert result["status"] == "error"
    assert result["incomplete"] is True
    assert result["missing_reviewers"] == ["gemini"]
    assert council.is_code_blessed() is False


def test_flint_advisor_uses_configured_local_completion_budget(arui_env, monkeypatch):
    from backend.app import council
    monkeypatch.setenv("ARUI_FLINT_BASE_URL", "http://127.0.0.1:30000/v1")
    monkeypatch.setenv("ARUI_FLINT_MODEL", "flint-test")
    monkeypatch.setenv("ARUI_FLINT_MAX_OUTPUT_TOKENS", "6000")
    monkeypatch.setenv("ARUI_FLINT_TIMEOUT_SECONDS", "1800")
    calls = []
    def post(url, body, headers, **kwargs):
        calls.append((url, body, kwargs))
        return {"choices": [{"message": {"content": '{"ok":true}'}}]}
    monkeypatch.setattr(council, "_post_json_retry", post)
    assert council._call_model("flint/local", "", "Return JSON.", "Check the fixture.") == '{"ok":true}'
    assert calls[0][1]["model"] == "flint-test"
    assert calls[0][1]["max_tokens"] == 6000
    assert calls[0][2] == {"timeout": 1800}


def test_opencode_advisor_inherits_bounded_request_timeout(arui_env, monkeypatch):
    from backend.app import council, opencode_runtime
    seen = []
    monkeypatch.setattr(council, "_TIMEOUT", 37)
    monkeypatch.setattr(opencode_runtime, "complete", lambda *args, **kwargs: seen.append(kwargs) or "OK")
    assert council._call_additional_model("opencode/big-pickle", "system", "user") == "OK"
    assert seen == [{"timeout": 37}]


@pytest.mark.parametrize("content", [None, '{"partial":true}'])
def test_flint_exhausted_reasoning_budget_is_actionable_error(arui_env, monkeypatch, content):
    from backend.app import council
    monkeypatch.setenv("ARUI_FLINT_BASE_URL", "http://127.0.0.1:30000/v1")
    monkeypatch.setattr(council, "_post_json_retry", lambda *args, **kwargs: {"choices": [
        {"message": {"content": content, "reasoning_content": "private reasoning"}, "finish_reason": "length"}]})
    with pytest.raises(RuntimeError, match="output budget"):
        council._call_additional_model("flint/local", "system", "user")


def test_tiebreaker_has_explicit_json_schema(arui_env):
    from backend.app import council
    assert "Return JSON ONLY" in council.TIEBREAKER_SYSTEM
    for key in ("verdict", "learning", "rerank_pending", "new_ideas", "veto"):
        assert '"' + key + '"' in council.TIEBREAKER_SYSTEM


@pytest.mark.parametrize("mode", ["research", "paper"])
@pytest.mark.parametrize("failure", ["http", "non_json", "bad_messages"])
def test_pi_model_failures_are_visible_and_never_send_or_switch(arui_env, monkeypatch, capsys, mode, failure):
    from backend.app import pi
    model = "gemini-4-argon"
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_API_KEY", "available-other-provider")
    monkeypatch.setattr(pi, "_settings", lambda: dict(pi.DEFAULTS, pi_agent_model=model))
    monkeypatch.setattr(pi, "_maybe_propose_next_move", lambda: False)
    monkeypatch.setattr(pi, "_maybe_auto_conclude", lambda: False)
    monkeypatch.setattr(pi, "_idle_gpu_escalation", lambda _: None)
    monkeypatch.setattr(pi, "_build_context", lambda: {})
    monkeypatch.setattr(pi, "_build_paper_context", lambda: {})
    monkeypatch.setattr(pi, "_send_to_agent", lambda *args, **kwargs: pytest.fail("A failed PI must not send any message"))
    calls = []
    def call(chosen, *args):
        calls.append(chosen)
        if failure == "http":
            raise urllib.error.HTTPError("https://provider.invalid", 429, "private error details", {}, io.BytesIO(b"private error details"))
        return "private error details" if failure == "non_json" else '{"concerns":"Check","messages":[7]}'
    monkeypatch.setattr(pi, "_call", call)
    result = (pi.cycle_paper if mode == "paper" else pi.cycle)(force=True)
    assert result["status"] == "error"
    assert result["model"] == model and result["mode"] == mode
    assert result["messages_sent"] == 0
    assert model in result["error"] and "private error details" not in result["error"]
    assert "private error details" not in capsys.readouterr().out
    assert calls == [model]


def test_output_budget_error_guidance_survives_safe_boundary(arui_env):
    from backend.app import council
    assert "increase Flint's output limit" in council._model_error_detail(council.ModelOutputBudgetError())
    assert council._model_error_detail(RuntimeError("private body")) == "RuntimeError"


@pytest.mark.parametrize("entry", ["review_async", "note_run_finished", "propose_next_move_async"])
def test_scheduling_missing_selected_key_reports_error_without_substitution(arui_env, monkeypatch, entry):
    from backend.app import council
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "available-other-provider")
    cfg = dict(council.DEFAULTS, council_per_run_enabled=True)
    monkeypatch.setattr(council, "_settings", lambda: cfg)
    failures = []
    monkeypatch.setattr(council, "_emit_token_failure_event", lambda *args: failures.append(args))
    monkeypatch.setattr(council.threading, "Thread", lambda *args, **kwargs: pytest.fail("No job should launch when a required selected key is missing"))
    result = getattr(council, entry)(*(() if entry == "propose_next_move_async" else ("fixture",)))
    assert failures and all(call[0] == "openai" for call in failures)
    assert all("gpt-6-astra" in call[2] for call in failures)
    if isinstance(result, dict):
        assert result["started"] is False and "gpt-6-astra" in result["error"]
    else:
        assert result is False


def test_council_enabled_only_depends_on_explicit_toggles(arui_env, monkeypatch):
    from backend.app import council
    monkeypatch.setattr(council, "_settings", lambda: {})
    assert council.is_enabled() is True
    monkeypatch.setattr(council, "_settings", lambda: {
        "council_enable_gemini": False, "council_enable_openai": False})
    assert council.is_enabled() is False
