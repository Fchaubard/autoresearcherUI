"""A failed selected model must remain visible and preserve the existing plan."""


def test_chat_model_failure_is_structured_and_retry_clears_error(arui_env, monkeypatch):
    from backend.app import council, scoping
    scoping.state_set({"status": "awaiting_user", "synthesis": {"recommended_direction": "Original plan"}, "messages": []})
    def fail(*args):
        raise RuntimeError("Selected model gemini-4-argon failed: HTTP 429")
    monkeypatch.setattr(council, "scope_chat", fail)
    result = scoping.chat("Compare the baseline")
    assert result["thinking"] is False
    assert "gemini-4-argon" in result["error"]
    assert result["synthesis"]["recommended_direction"] == "Original plan"
    monkeypatch.setattr(council, "scope_chat", lambda *args: "Use matched seeds.")
    assert scoping.chat("Try again")["error"] == ""


def test_finalize_failure_preserves_plan_without_claiming_success(arui_env, monkeypatch):
    from backend.app import council, scoping
    original = {"recommended_direction": "Original plan"}
    scoping.state_set({"status": "awaiting_user", "synthesis": original, "messages": []})
    monkeypatch.setattr(council, "scope_finalize", lambda *args: None)
    result = scoping.finalize()
    assert result["synthesis"] == original
    assert result["finalizing"] is False
    assert "no updated plan" in result["error"]
