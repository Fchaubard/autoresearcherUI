"""A timed-out live probe cannot leave a detached CLI inferring behind it."""
import os
import subprocess
import sys
import time

import psutil


def test_timeout_cleanup_kills_detached_descendant(tmp_path):
    from scripts.probe_advisor_roles import _kill_worker_tree
    child_pid_file = tmp_path / "child.pid"
    source = """import pathlib, subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'], start_new_session=True)
pathlib.Path(sys.argv[1]).write_text(str(child.pid))
time.sleep(60)
"""
    proc = subprocess.Popen([sys.executable, "-c", source, str(child_pid_file)], start_new_session=True)
    child = None
    try:
        deadline = time.monotonic() + 5
        while not child_pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert child_pid_file.exists()
        child = psutil.Process(int(child_pid_file.read_text()))
        assert os.getpgid(child.pid) != proc.pid
        _kill_worker_tree(proc)
        assert proc.wait(timeout=5) != 0
        deadline = time.monotonic() + 5
        while child.is_running() and child.status() != psutil.STATUS_ZOMBIE and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not child.is_running() or child.status() == psutil.STATUS_ZOMBIE
    finally:
        _kill_worker_tree(proc)
        proc.wait(timeout=5)
        if child is not None and child.is_running():
            child.kill()


def test_provider_error_metadata_keeps_safe_category_not_message():
    from scripts.probe_advisor_roles import _error_metadata
    class ProviderError(RuntimeError):
        error_category = "quota_exhausted"
    metadata = _error_metadata(ProviderError("private headers or request details"))
    assert metadata == {"error_type": "ProviderError", "error_category": "quota_exhausted"}


def test_scoping_probe_retains_original_opencode_error(arui_env, monkeypatch):
    from unittest.mock import patch
    from backend.app import opencode_runtime, provider_config
    from scripts.probe_advisor_roles import probe_role
    monkeypatch.setattr(provider_config, "opencode_binary", lambda: "/bin/true")
    def fail(*args, **kwargs):
        raise opencode_runtime.OpenCodeRequestError(429)
    monkeypatch.setattr(opencode_runtime, "complete", fail)
    # probe_role deliberately changes its process environment; isolate those
    # changes when exercising its boundary in the unit-test process.
    with patch.dict(os.environ):
        result = probe_role("opencode/big-pickle", "scoping_chat")
    assert result["status"] == "provider_error"
    assert result["http_status"] == 429
    assert result["error_category"] == "quota_exhausted"
    assert result["dispatched_models"] == ["opencode/big-pickle"]
