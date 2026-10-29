from __future__ import annotations

import json
import threading
import time

import pytest


def _wait_bless(council):
    deadline = time.monotonic() + 5
    while council._BLESS_ACTIVE_KEY is not None and time.monotonic() < deadline:
        time.sleep(.01)
    assert council._BLESS_ACTIVE_KEY is None
    return council._bless_state_get()


def _bless_setup(arui_env, monkeypatch):
    from backend.app import council
    workspace = arui_env / "source"
    workspace.mkdir()
    (workspace / "train.py").write_text("loss = 1\n")
    monkeypatch.setattr(council, "preflight_blocking_reasons", lambda: [])
    cfg = dict(council.DEFAULTS)
    monkeypatch.setattr(council, "_settings", lambda: dict(cfg))
    calls = []
    def reviewer(slot, *args):
        calls.append(slot)
        return {"approved": True, "summary": "Reviewed", "blockers": [], "suggestions": []}
    monkeypatch.setattr(council, "_call_reviewer", reviewer)
    return council, workspace, cfg, calls


def test_run_contexts_strip_only_application_review_metadata(arui_env, make_project, make_run):
    from backend.app import council
    make_project()
    config = {"lr": .1, "architecture": {"review": "real experimental value"},
              "review": {"secret_previous_prompt": "huge"}, "reviews": ["huge"],
              "strategic_review": {"old": True}, "strategic_review_id": "old"}
    run = make_run(config=config)
    contexts = [council._build_context(run.id)["this_run"],
                council._build_strategic_context([run.id])["batch"][0],
                council._build_completion_review_context([run.id], "s", "YES", "WRITE_PAPER")["evidence_runs"][0]]
    for item in contexts:
        assert item["config"] == {"lr": .1, "architecture": {"review": "real experimental value"}}
    assert run.config == config


@pytest.mark.parametrize("approved", [True, False])
def test_completed_identical_bless_uses_exact_cached_verdict(arui_env, monkeypatch, approved):
    council, workspace, cfg, calls = _bless_setup(arui_env, monkeypatch)
    def review(slot, *args):
        calls.append(slot)
        return {"approved": approved, "blockers": [] if approved else ["Fix implementation"]}
    monkeypatch.setattr(council, "_call_reviewer", review)
    status = "approved" if approved else "rejected"
    council.bless_async(str(workspace))
    first = _wait_bless(council)
    assert first["status"] == status and len(calls) == 2
    second = council.bless_async(str(workspace))
    assert second["cached"] is True and second["status"] == status
    assert council.is_code_blessed() is approved
    assert second["reviewed_at"] == first["reviewed_at"] and len(calls) == 2
    cfg["council_openai_effort"] = "medium"
    council.bless_async(str(workspace))
    assert _wait_bless(council)["cached"] is False and len(calls) == 4


@pytest.mark.parametrize("change", ["source", "truncated_tail", "model", "effort", "prompt", "reviewer", "seed_gate"])
def test_bless_key_invalidates_for_every_decision_input(arui_env, monkeypatch, change):
    council, workspace, cfg, calls = _bless_setup(arui_env, monkeypatch)
    path = workspace / "train.py"
    if change == "truncated_tail":
        path.write_text("x" * 70000)
    before = council._bless_request(str(workspace))["key"]
    meta = None
    if change in {"source", "truncated_tail"}:
        path.write_text(path.read_text() + "changed")
    elif change == "model":
        cfg["council_openai_model"] = "gpt-5.5"
    elif change == "effort":
        cfg["council_openai_effort"] = "medium"
    elif change == "prompt":
        monkeypatch.setattr(council, "_BLESS_SYSTEM", council._BLESS_SYSTEM + " New criterion.")
    elif change == "reviewer":
        cfg["council_enable_gemini"] = False
    else:
        meta = {"dataset_kind": "smoke"}
    assert council._bless_request(str(workspace), meta)["key"] != before


def test_bless_coalesces_inflight_and_refuses_changed_source_approval(arui_env, monkeypatch):
    council, workspace, cfg, calls = _bless_setup(arui_env, monkeypatch)
    entered, finish = threading.Event(), threading.Event()
    def reviewer(slot, *args):
        calls.append(slot)
        entered.set()
        assert finish.wait(5)
        return {"approved": True}
    monkeypatch.setattr(council, "_call_reviewer", reviewer)
    council.bless_async(str(workspace))
    assert entered.wait(3)
    assert council.bless_async(str(workspace))["coalesced"] is True
    (workspace / "train.py").write_text("changed = True\n")
    assert council.bless_async(str(workspace))["status"] == "busy"
    finish.set()
    result = _wait_bless(council)
    assert result["status"] == "error" and "Source changed" in result["error"]
    assert len(calls) == 2 and not council.is_code_blessed()


def test_failed_bless_is_never_cached(arui_env, monkeypatch):
    council, workspace, cfg, calls = _bless_setup(arui_env, monkeypatch)
    def failed(slot, *args):
        calls.append(slot)
        return None
    monkeypatch.setattr(council, "_call_reviewer", failed)
    for _ in range(2):
        council.bless_async(str(workspace))
        assert _wait_bless(council)["status"] == "error"
    assert len(calls) == 4


def test_changed_reviewer_selection_invalidates_existing_approval(arui_env, monkeypatch):
    council, workspace, cfg, calls = _bless_setup(arui_env, monkeypatch)
    council.bless_async(str(workspace))
    assert _wait_bless(council)["status"] == "approved"
    assert council.is_code_blessed()
    cfg["council_openai_model"] = "gpt-5.5"
    assert not council.is_code_blessed()


def test_local_model_endpoint_changes_invalidate_cache(arui_env, monkeypatch):
    council, workspace, cfg, calls = _bless_setup(arui_env, monkeypatch)
    cfg["council_openai_model"] = "flint/local"
    cfg["flint_base_url"] = "http://127.0.0.1:1234/v1"
    first = council._bless_request(str(workspace))["key"]
    cfg["flint_model"] = "explicit-new-checkpoint"
    assert council._bless_request(str(workspace))["key"] != first
    cfg["flint_model"] = ""
    cfg["flint_base_url"] = "http://127.0.0.1:4321/v1"
    assert council._bless_request(str(workspace))["key"] != first


def test_generated_progress_does_not_change_review_prompt_or_fingerprint(arui_env, monkeypatch):
    from backend.app import review_cache
    council, workspace, cfg, calls = _bless_setup(arui_env, monkeypatch)
    first = council._bless_request(str(workspace))
    for name in review_cache.PROGRESS_FILES:
        (workspace / name).write_text("Updated healthy training progress")
    after = council._bless_request(str(workspace))
    assert after["key"] == first["key"] and after["code"] == first["code"]
    for name in ["program.md", "ideas.md", "experiment.json", "literature_review.md", "DOMAIN_STATUS.md"]:
        (workspace / name).write_text("Actual research specification or config")
        changed = council._bless_request(str(workspace))
        assert changed["key"] != after["key"] and name in changed["code"]
        after = changed


@pytest.mark.parametrize("change", ["progress", "selection", "preflight"])
def test_inflight_review_distinguishes_progress_from_changed_review_evidence(arui_env, monkeypatch, change):
    council, workspace, cfg, calls = _bless_setup(arui_env, monkeypatch)
    changed = False
    def review(slot, *args):
        nonlocal changed
        if not changed:
            changed = True
            if change == "progress":
                (workspace / "AGENT_STATE.md").write_text("Run is healthy; GPU is busy")
            elif change == "selection":
                cfg["council_openai_effort"] = "medium"
            else:
                council._preflight_state_set({"changed_at_iso": "new-code-marker"})
        return {"approved": True}
    monkeypatch.setattr(council, "_call_reviewer", review)
    council.bless_async(str(workspace))
    result = _wait_bless(council)
    assert result["status"] == ("approved" if change == "progress" else "error")


def test_source_walk_prunes_large_excluded_trees_before_scanning(arui_env, monkeypatch):
    from backend.app import review_cache
    council, workspace, cfg, calls = _bless_setup(arui_env, monkeypatch)
    nested = workspace / "checkpoints" / "many" / "nested"
    nested.mkdir(parents=True)
    (nested / "weights.json").write_text("unreviewed large artifact")
    original = review_cache.os.scandir
    visited = []
    def scan(path):
        visited.append(str(path))
        return original(path)
    monkeypatch.setattr(review_cache.os, "scandir", scan)
    assert len(list(review_cache.source_files(str(workspace)))) == 1
    assert all("checkpoints" not in path for path in visited)


def _crash(arui_env, make_run, name, **overrides):
    run = make_run(id=name, status="crashed", config={"source_sha256": "a" * 64, "lr": .1,
                   "process_exit_at": name, "gpu_index": len(name), **overrides})
    folder = arui_env / "run_logs"
    folder.mkdir(exist_ok=True)
    (folder / f"{name}.log").write_text('Traceback (most recent call last):\n  File "train.py", line 4\n'
                                       f'ModuleNotFoundError: No module named package\n{ name }\n')
    return run


def test_matching_crash_reuses_diagnosis_without_reapplying_science_policy(arui_env, monkeypatch, make_project, make_run, db_session):
    from backend.app import council
    from backend.app.models import Run
    make_project()
    first = _crash(arui_env, make_run, "run-first")
    second = _crash(arui_env, make_run, "run-second")
    calls, applied, lessons = [], [], []
    def review(run_id, **kwargs):
        calls.append(run_id)
        return {"learning": "Install package; evidence run-first", "reviewer": "gemini+openai",
                "reviewed_at": "2026-10-29T00:00:00Z", "veto": ["old-idea"], "new_ideas": [{"old": True}]}
    monkeypatch.setattr(council, "deliberate", review)
    monkeypatch.setattr(council, "_apply_to_ideas_md", lambda review: applied.append(review))
    monkeypatch.setattr(council, "_append_lesson", lambda **kwargs: lessons.append(kwargs))
    council._worker(first.id)
    council._worker(second.id)
    db_session.expire_all()
    stored = db_session.query(Run).filter(Run.id == second.id).one().config["review"]
    assert calls == [first.id] and len(applied) == len(lessons) == 1
    assert stored["cached"] is True and stored["reused_from_run_id"] == first.id
    assert stored["veto"] == [] and stored["new_ideas"] == []


def test_crash_reuse_requires_same_source_config_policy_and_exception(arui_env, monkeypatch, make_project, make_run):
    from backend.app import council
    make_project()
    first = _crash(arui_env, make_run, "run-a")
    same = _crash(arui_env, make_run, "run-b")
    different = _crash(arui_env, make_run, "run-c", lr=.2)
    unversioned = _crash(arui_env, make_run, "run-d", source_sha256="")
    cfg = council._settings()
    key = council._crash_review_key(first.id, cfg)
    assert key and council._crash_review_key(same.id, cfg) == key
    assert council._crash_review_key(different.id, cfg) != key
    assert council._crash_review_key(unversioned.id, cfg) is None
    assert council._crash_review_key(first.id, {**cfg, "council_openai_effort": "medium"}) != key
    (arui_env / "run_logs" / f"{same.id}.log").write_text("exit code 1\n")
    assert council._crash_review_key(same.id, cfg) is None


def test_concurrent_matching_crashes_share_one_review(arui_env, monkeypatch, make_project, make_run):
    from backend.app import council
    make_project()
    runs = [_crash(arui_env, make_run, name) for name in ("run-a", "run-b")]
    entered, release = threading.Event(), threading.Event()
    calls = []
    def review(run_id, **kwargs):
        calls.append(run_id)
        entered.set()
        assert release.wait(3)
        return {"learning": "Repair missing dependency", "reviewer": "gemini+openai"}
    monkeypatch.setattr(council, "deliberate", review)
    monkeypatch.setattr(council, "_apply_to_ideas_md", lambda _: None)
    monkeypatch.setattr(council, "_append_lesson", lambda **kwargs: None)
    threads = [threading.Thread(target=council._worker, args=(r.id,)) for r in runs]
    threads[0].start()
    assert entered.wait(3)
    threads[1].start()
    release.set()
    for thread in threads:
        thread.join(3)
        assert not thread.is_alive()
    assert len(calls) == 1


def test_failed_crash_review_is_not_cached(arui_env, monkeypatch, make_project, make_run):
    from backend.app import council
    make_project()
    runs = [_crash(arui_env, make_run, name) for name in ("run-a", "run-b")]
    calls = []
    monkeypatch.setattr(council, "deliberate", lambda run_id, **kwargs: calls.append(run_id))
    for run in runs:
        council._worker(run.id)
    assert calls == [run.id for run in runs]


def test_first_crash_prompt_has_bounded_redacted_exception_evidence(arui_env, monkeypatch, make_project, make_run):
    from backend.app import council
    make_project()
    run = _crash(arui_env, make_run, "run-evidence")
    secret = "configured-private-value-123"
    monkeypatch.setenv("GEMINI_API_KEY", secret)
    log = ("Unrelated console content: keep outside review\n" * 300
           + "Traceback (most recent call last):\n"
           + '  File "train.py", line 10, in train\n' * 300
           + "RuntimeError: connection failed " + secret
           + "\nUnrelated output after the exception\n")
    (arui_env / "run_logs" / f"{run.id}.log").write_text(log)
    result = council._build_context(run.id)["this_run"]["failure_evidence"]
    assert "RuntimeError" in result and "[REDACTED]" in result
    assert secret not in result and "Unrelated" not in result
    assert len(result) <= 6000


def test_review_cache_expires_without_mutating_saved_evidence(arui_env, monkeypatch):
    from backend.app import review_cache
    now = time.time()
    monkeypatch.setattr(review_cache.time, "time", lambda: now)
    review_cache.put("test", "key", {"evidence": "original"})
    copy = review_cache.get("test", "key")
    copy["evidence"] = "mutated"
    assert review_cache.get("test", "key") == {"evidence": "original"}
    monkeypatch.setattr(review_cache.time, "time", lambda: now + 25 * 3600)
    assert review_cache.get("test", "key") is None


@pytest.mark.parametrize("status,eligible", [
    ("kept_novel", False), ("kept_replicate", False),
    ("kept", True), ("success", True), ("discarded", True),
])
def test_cost_controls_preserve_existing_success_review_policy(
        arui_env, make_project, make_run, status, eligible):
    from backend.app import council
    make_project()
    run = make_run(status=status)
    assert council._worth_reviewing(run.id) is eligible
    assert council._crash_review_key(run.id, council._settings()) is None
