"""Slow selected reviewers must never create parallel completion inference."""
import datetime as dt
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest


def prepare(monkeypatch, council):
    monkeypatch.delenv("ARUI_DISABLE_BG", raising=False)
    monkeypatch.setattr(council, "configured_reviewers", lambda _: ["openai"])
    monkeypatch.setattr(council, "_build_completion_review_context", lambda *args: {"synthetic": True})
    monkeypatch.setattr(council, "_auto_enter_paper_after_approval", lambda *args: None)


def request(council, summary="Evidence establishes improvement"):
    return council.review_completion_async(["run-1"], summary, "YES_CONCLUSIVELY", "WRITE_PAPER")


def approval():
    return {"verdict": "APPROVED", "reasons": [], "missing_evidence": [], "summary": "Synthetic review"}


def test_parallel_identical_claims_share_one_call_and_other_claim_is_busy(arui_env, monkeypatch):
    from backend.app import council
    from backend.app.db import SessionLocal
    from backend.app.models import Event
    prepare(monkeypatch, council)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def review(*args):
        calls.append(args)
        entered.set()
        assert release.wait(5)
        return approval()

    monkeypatch.setattr(council, "_call_reviewer", review)
    first = request(council)
    worker = council._COMPLETION_ACTIVE_THREAD
    try:
        assert entered.wait(2)
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: request(council), range(6)))
        assert all(r.get("coalesced") and r["review_id"] == first["review_id"] for r in results)
        assert len(calls) == 1
        rejected = request(council, "A different conclusion")
        assert rejected["status"] == "busy"
        assert council.conclusion_state()["summary"] == first["summary"]
        with SessionLocal() as db:
            assert db.query(Event).filter(Event.type == "council_launch").count() == 1
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    assert not council.completion_review_active()
    assert council.conclusion_state()["status"] == "approved"
    # Completed conclusions still need a new review when explicitly submitted.
    second = request(council)
    worker2 = council._COMPLETION_ACTIVE_THREAD
    worker2.join(3)
    assert not worker2.is_alive()
    assert second["review_id"] != first["review_id"]
    assert len(calls) == 2


def test_live_slow_worker_outlives_lease_grace_without_retry(arui_env, monkeypatch):
    from backend.app import council, lifecycle, supervisor
    prepare(monkeypatch, council)
    entered, release = threading.Event(), threading.Event()

    def review(*args):
        entered.set()
        assert release.wait(5)
        return approval()

    monkeypatch.setattr(council, "_call_reviewer", review)
    request(council)
    worker = council._COMPLETION_ACTIVE_THREAD
    try:
        assert entered.wait(2)
        state = council.conclusion_state()
        state["conclude_at"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat()
        council._conclusion_state_set(state)
        monkeypatch.setattr(lifecycle, "lease_alive", lambda *args, **kwargs: False)
        monkeypatch.setattr(council, "review_completion_async", lambda *args, **kwargs: pytest.fail("duplicate inference"))
        supervisor._supervise_completion_review()
        assert lifecycle.remediation_count("completion_review") == 0
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()


def test_completion_heartbeats_during_inference_and_cleans_up(arui_env, monkeypatch):
    from backend.app import council, lifecycle
    prepare(monkeypatch, council)
    entered, release, heartbeat = threading.Event(), threading.Event(), threading.Event()
    real_heartbeat = lifecycle.lease_heartbeat

    def record_heartbeat(name):
        real_heartbeat(name)
        heartbeat.set()

    def review(*args):
        entered.set()
        assert release.wait(5)
        return approval()

    monkeypatch.setattr(council, "_COMPLETION_HEARTBEAT_SEC", .02)
    monkeypatch.setattr(lifecycle, "lease_heartbeat", record_heartbeat)
    monkeypatch.setattr(council, "_call_reviewer", review)
    request(council)
    worker = council._COMPLETION_ACTIVE_THREAD
    try:
        assert entered.wait(2)
        assert heartbeat.wait(2)
        lease = lifecycle.lease_get("completion_review")
        assert lease["heartbeat_at"] > lease["started_at"]
        assert lifecycle.lease_alive("completion_review", max_age_sec=1)
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    assert lifecycle.lease_get("completion_review") is None
    assert not any(t.name == "council-completion-heartbeat" for t in threading.enumerate())


@pytest.mark.parametrize("fail", [False, True])
def test_operator_clear_blocks_late_verdict_and_remaining_calls(arui_env, monkeypatch, fail):
    from backend.app import council
    prepare(monkeypatch, council)
    monkeypatch.setattr(council, "configured_reviewers", lambda _: ["gemini", "openai"])
    entered, release = threading.Event(), threading.Event()
    calls, paper = [], []

    def review(slot, *args):
        calls.append(slot)
        entered.set()
        assert release.wait(5)
        if fail:
            raise RuntimeError("synthetic failure after cancellation")
        return approval()

    monkeypatch.setattr(council, "_call_reviewer", review)
    monkeypatch.setattr(council, "_auto_enter_paper_after_approval", lambda *args: paper.append(args))
    request(council)
    worker = council._COMPLETION_ACTIVE_THREAD
    try:
        assert entered.wait(2)
        council.clear_conclusion("Operator rejected this claim")
        assert request(council)["status"] == "busy"
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    assert council.conclusion_state()["status"] == "none"
    assert calls == ["gemini"]
    assert paper == []


def test_start_failure_is_visible_and_releases_claim_ownership(arui_env, monkeypatch):
    from backend.app import council
    prepare(monkeypatch, council)

    def cannot_start(self):
        raise RuntimeError("synthetic thread capacity error")

    monkeypatch.setattr(threading.Thread, "start", cannot_start)
    result = request(council)
    assert result["status"] == "error" and result["incomplete"]
    assert "could not start" in result["error"]
    assert not council.completion_review_active()
    assert council._COMPLETION_ACTIVE_KEY is None
