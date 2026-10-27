"""Real harmless processes exercise halts without touching production tmux."""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time

import pytest


def _wait(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.03)
    assert predicate()


@pytest.fixture
def jobs(arui_env, monkeypatch, tmp_path):
    from backend.app.config import WORKSPACE_DIR
    from backend.app.db import SessionLocal
    from backend.app.models import Setting
    root = WORKSPACE_DIR / "halt-test"
    root.mkdir(parents=True)
    other = WORKSPACE_DIR / "other-project"
    other.mkdir()
    tmux_root = tmp_path / "private-tmux"
    tmux_root.mkdir(mode=0o700)
    monkeypatch.setenv("TMUX_TMPDIR", str(tmux_root))
    monkeypatch.setenv("TMUX", "")
    with SessionLocal() as db:
        db.add(Setting(key="onboarding", value={"repo_name": "halt-test"}))
        db.commit()
    script = "import time\nwhile True: time.sleep(1)\n"
    (root / "train.py").write_text(script)
    (other / "train.py").write_text(script)
    tracked = []

    def launch(directory=root, command=None):
        p = subprocess.Popen(command or [sys.executable, "train.py"],
                             cwd=directory, start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        tracked.append(p)
        _wait(lambda: Path(f"/proc/{p.pid}/cwd").resolve() == directory)
        return p

    yield root, other, launch
    # A private tmux server and only PIDs created by this fixture are cleaned.
    subprocess.run(["tmux", "kill-server"], capture_output=True)
    from backend.app import research_processes
    for p in tracked:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        p.wait(timeout=3)
    leftovers = research_processes.discover(root)
    research_processes._stop(leftovers, [], 0.1)


def test_empty_database_halt_stops_detached_training_not_other_project(jobs):
    from backend.app import interrupt, research_processes, notify
    root, other, launch = jobs
    worker = launch()
    unrelated = launch(other)
    shell = launch(command=["bash", "-c", "sleep 300"])
    _wait(lambda: bool(research_processes.discover(root)))
    result = interrupt.hard_interrupt()
    assert result["ok"], result
    assert result["workers_stopped"] == [f"pid:{worker.pid}"]
    assert result["remaining"] == []
    assert worker.wait(timeout=2) != 0
    assert unrelated.poll() is None
    assert shell.poll() is None
    assert notify.research_paused()


def test_queue_supervisor_stops_before_it_can_spawn_more_workers(jobs):
    from backend.app import interrupt, research_processes
    root, _, launch = jobs
    (root / "dispatch_queue.py").write_text(
        "import subprocess, sys, time\nfrom pathlib import Path\n"
        "while True:\n"
        " p = subprocess.Popen([sys.executable, 'train.py'], start_new_session=True)\n"
        " with open('launched', 'a') as f: f.write(str(p.pid)+'\\n')\n"
        " p.wait()\n"
        " time.sleep(0.01)\n")
    queue = launch(command=[sys.executable, "dispatch_queue.py"])
    _wait(lambda: (root / "launched").exists())
    before = (root / "launched").read_text()
    result = interrupt.hard_interrupt()
    assert result["ok"], result
    assert result["queues_stopped"] == [f"pid:{queue.pid}"]
    queue.wait(timeout=2)
    time.sleep(0.2)
    assert (root / "launched").read_text() == before
    assert research_processes.discover(root) == []


def test_tmux_exact_scope_protects_core_and_same_prefix(jobs):
    from backend.app import interrupt, research_processes, tmux_safe
    root, other, _ = jobs
    for name, cwd in [("halt-worker", root), ("halt-worker-other", other),
                      ("agent", root), ("user-shell", root)]:
        cmd = "sleep 300" if name == "user-shell" else f"{shlex.quote(sys.executable)} train.py"
        subprocess.run(["tmux", "new-session", "-d", "-s", name,
                        "-c", str(cwd), cmd], check=True, capture_output=True)
    _wait(lambda: any(w.label == "halt-worker" for w in research_processes.discover(root)))
    result = interrupt.kill_killable_runs()
    assert result["ok"], result
    assert result["workers_stopped"] == ["halt-worker"]
    assert not tmux_safe.is_alive("halt-worker")
    assert tmux_safe.is_alive("halt-worker-other")
    assert tmux_safe.is_alive("agent")
    assert tmux_safe.is_alive("user-shell")
    # Missing exact target must never fall through to tmux prefix matching.
    assert tmux_safe.kill_session("halt-worker")[0] is False
    assert tmux_safe.is_alive("halt-worker-other")


def test_database_failure_still_stops_work_and_reports_failure(jobs, monkeypatch):
    from backend.app import interrupt, db, research_control, research_processes, notify
    root, _, launch = jobs
    worker = launch()
    research_control.remember_workspace({"repo_name": "halt-test"})
    _wait(lambda: bool(research_processes.discover(root)))
    def broken():
        raise RuntimeError("database is unavailable")
    monkeypatch.setattr(db, "SessionLocal", broken)
    result = interrupt.hard_interrupt()
    assert result["ok"] is False
    assert result["paused"] is True
    assert result["workers_stopped"] == [f"pid:{worker.pid}"]
    assert result["errors"]
    assert worker.wait(timeout=2) != 0
    assert research_control.read()["paused"] is True
    assert notify.research_paused() is True
    assert notify.set_research_paused(False)["ok"] is False
    assert research_control.read()["paused"] is True


def test_worker_child_of_agent_stops_without_killing_agent(jobs):
    from backend.app import interrupt, research_processes, tmux_safe
    root, _, _ = jobs
    (root / "main_agent.py").write_text(
        "import subprocess, sys, time\nfrom pathlib import Path\n"
        "child = subprocess.Popen([sys.executable, 'train.py'])\n"
        "Path('child-pid').write_text(str(child.pid))\n"
        "while True: time.sleep(1)\n")
    subprocess.run(["tmux", "new-session", "-d", "-s", "agent", "-c", str(root),
                    f"{shlex.quote(sys.executable)} main_agent.py"], check=True, capture_output=True)
    _wait(lambda: (root / "child-pid").exists())
    child = int((root / "child-pid").read_text())
    _wait(lambda: any(w.label == f"pid:{child}" for w in research_processes.discover(root)))
    result = interrupt.kill_killable_runs()
    assert result["ok"], result
    assert result["workers_stopped"] == [f"pid:{child}"]
    assert tmux_safe.pane_alive("agent")


def test_kill_failure_does_not_report_stopped_or_change_run_status(jobs, monkeypatch):
    from backend.app import interrupt, research_processes
    from backend.app.db import SessionLocal
    from backend.app.models import Run
    root, _, launch = jobs
    worker = launch()
    _wait(lambda: bool(research_processes.discover(root)))
    with SessionLocal() as db:
        db.add(Run(id="failed-run", project_id="p", status="running", tmux_session="failed-run"))
        db.commit()
    monkeypatch.setattr(research_processes, "_signal", lambda p, sig, errors: errors.append("permission denied"))
    original = research_processes.stop_project
    monkeypatch.setattr(research_processes, "stop_project", lambda root, registered: original(root, registered, grace=0))
    result = interrupt.hard_interrupt()
    assert not result["ok"]
    assert result["killed"] == []
    assert f"pid:{worker.pid}" in result["remaining"]
    assert worker.poll() is None
    with SessionLocal() as db:
        assert db.get(Run, "failed-run").status == "running"


def test_workspace_cannot_expand_to_parent(arui_env):
    from backend.app import research_control
    with pytest.raises(ValueError):
        research_control.workspace({"repo_name": ".."})


def test_legacy_halt_api_stops_untracked_training_and_resumes_gate(jobs):
    from backend.app import research_processes
    from backend.app.api import router
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    root, _, launch = jobs
    worker = launch()
    _wait(lambda: bool(research_processes.discover(root)))
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        result = client.post("/api/halt", json={"reason": "integration test"}).json()
        assert result["ok"] and result["halted"], result
        assert result["workers_stopped"] == [f"pid:{worker.pid}"]
        assert worker.wait(timeout=2) != 0
        assert client.post("/api/track/run", json={"name": "_probe_test"}).status_code == 423
        resumed = client.post("/api/halt/resume", json={}).json()
        assert resumed["ok"] and not resumed["halted"], resumed


def test_interrupt_blocks_probe_until_resume(jobs):
    from backend.app.api import router
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        result = client.post("/api/research/interrupt", json={}).json()
        assert result["ok"], result
        assert client.post("/api/track/run", json={"name": "_probe_test"}).status_code == 423
        resumed = client.post("/api/research/resume", json={}).json()
        assert resumed["ok"] and not resumed["paused"], resumed


def test_pause_resume_cannot_bypass_hard_halt(arui_env, fake_subprocess):
    from backend.app import notify, research_control
    assert notify.set_research_paused(True)["ok"]
    assert notify.set_research_halted(True, "operator stop")["ok"]
    resumed = notify.set_research_paused(False)
    assert not resumed["ok"] and resumed["blocked"]
    assert research_control.read()["paused"]


def test_broken_control_file_fails_closed(arui_env):
    from backend.app import notify, research_control
    research_control._path().write_text("not json")
    assert notify.research_paused()
    assert notify.research_halted()[0]


def test_training_in_interactive_shell_does_not_kill_user_shell(jobs):
    from backend.app import interrupt, research_processes, tmux_safe
    root, _, _ = jobs
    subprocess.run(["tmux", "new-session", "-d", "-s", "user-terminal", "-c", str(root)],
                   check=True, capture_output=True)
    subprocess.run(["tmux", "send-keys", "-t", "user-terminal",
                    f"{shlex.quote(sys.executable)} train.py", "Enter"], check=True, capture_output=True)
    _wait(lambda: bool(research_processes.discover(root)))
    result = interrupt.kill_killable_runs()
    assert result["ok"], result
    assert len(result["workers_stopped"]) == 1
    assert result["workers_stopped"][0].startswith("pid:")
    assert tmux_safe.pane_alive("user-terminal")


def test_mixed_tmux_session_preserves_other_project_and_shell(jobs):
    from backend.app import interrupt, research_processes, tmux_safe
    root, other, _ = jobs
    command = f"{shlex.quote(sys.executable)} train.py"
    subprocess.run(["tmux", "new-session", "-d", "-s", "mixed", "-c", str(root), command],
                   check=True, capture_output=True)
    subprocess.run(["tmux", "split-window", "-d", "-t", "mixed", "-c", str(other), command],
                   check=True, capture_output=True)
    subprocess.run(["tmux", "split-window", "-d", "-t", "mixed", "-c", str(root)],
                   check=True, capture_output=True)
    _wait(lambda: bool(research_processes.discover(root)))
    result = interrupt.kill_killable_runs()
    assert result["ok"], result
    assert len(result["workers_stopped"]) == 1
    assert result["workers_stopped"][0].startswith("pid:")
    assert tmux_safe.is_alive("mixed")
    assert len(research_processes.discover(other)) == 1


def test_resume_reports_pending_settings_warning(arui_env, fake_subprocess, monkeypatch):
    from backend.app import notify, runtime_settings
    assert notify.set_research_paused(True)["ok"]
    monkeypatch.setattr(runtime_settings, "apply_pending", lambda: {
        "warnings": ["Agent model change remains pending"]})
    result = notify.set_research_paused(False)
    assert result["ok"] and not result["paused"]
    assert result["warnings"] == ["Agent model change remains pending"]


def test_resume_reports_pending_settings_exception_without_exposing_secret(arui_env, fake_subprocess, monkeypatch):
    from backend.app import notify, runtime_settings
    assert notify.set_research_paused(True)["ok"]
    def broken():
        raise RuntimeError("test-secret-must-not-escape")
    monkeypatch.setattr(runtime_settings, "apply_pending", broken)
    result = notify.set_research_paused(False)
    assert not result["ok"]
    assert "RuntimeError" in result["errors"][0]
    assert "test-secret" not in str(result)


def test_halt_resume_keeps_gate_while_halt_directive_open(arui_env, fake_subprocess, monkeypatch):
    from backend.app import notify, directives, research_control
    assert notify.set_research_halted(True, "operator stop")["ok"]
    monkeypatch.setattr(directives, "open_halt", lambda: {"id": "unresolved-halt"})
    result = notify.set_research_halted(False)
    assert not result["ok"] and result["reason"] == "halt_directive"
    assert research_control.read()["halted"]


def test_live_model_change_waits_for_agent_child_then_watcher_applies(jobs, monkeypatch):
    from backend.app import runtime_settings, agent_watcher, research_processes, realrun, tmux_safe
    from backend.app.db import SessionLocal
    from backend.app.models import Setting
    root, _, _ = jobs
    # Training completes only when this test allows it, recording a checkpoint.
    (root / "train.py").write_text(
        "from pathlib import Path\nimport time\n"
        "while not Path('finish-training').exists(): time.sleep(0.03)\n"
        "Path('checkpoint.txt').write_text('training completed')\n")
    (root / "main_agent.py").write_text(
        "import subprocess, sys, time\nfrom pathlib import Path\n"
        "child = subprocess.Popen([sys.executable, 'train.py'])\n"
        "Path('child-pid').write_text(str(child.pid))\n"
        "while True: time.sleep(1)\n")
    (root / "replacement_agent.py").write_text("import time\nwhile True: time.sleep(1)\n")
    subprocess.run(["tmux", "new-session", "-d", "-s", "agent", "-c", str(root),
                    f"{shlex.quote(sys.executable)} main_agent.py"], check=True, capture_output=True)
    _wait(lambda: (root / "child-pid").exists())
    child = int((root / "child-pid").read_text())
    _wait(lambda: child in research_processes.active_session_workloads("agent"))
    original_parent = next(pid for name, pid, _ in research_processes.panes() if name == "agent")
    with SessionLocal() as db:
        cfg = db.query(Setting).filter_by(key="onboarding").first()
        cfg.value = {**cfg.value, "research_agent_model": "gemini-4-argon"}
        db.commit()
    runtime_settings._set_pending(["agent"])
    starts = []
    monkeypatch.setenv("ARUI_CLAUDE_BIN", "test-only")
    def start_latest(cfg, *, resume=False):
        starts.append((cfg["research_agent_model"], resume))
        subprocess.run(["tmux", "new-session", "-d", "-s", "agent", "-c", str(root),
                        f"{shlex.quote(sys.executable)} replacement_agent.py"],
                       check=True, capture_output=True)
        return object()
    monkeypatch.setattr(realrun, "start_real", start_latest)
    result = runtime_settings.apply_pending()
    assert result["pending"] == ["agent"]
    assert result["pending_reasons"] == {"agent": "active_experiments"}
    assert result["warnings"] == []
    assert starts == []
    assert next(pid for name, pid, _ in research_processes.panes() if name == "agent") == original_parent
    assert research_processes._process(child) is not None
    # A periodic retry while busy must likewise preserve the live training.
    assert agent_watcher._check_pending_settings()["pending"] == ["agent"]
    assert research_processes._process(child) is not None
    assert starts == []
    (root / "finish-training").touch()
    _wait(lambda: (root / "checkpoint.txt").exists())
    _wait(lambda: not research_processes.active_session_workloads("agent"))
    applied = agent_watcher._check_pending_settings()
    assert applied["applied"] == ["agent"] and applied["pending"] == []
    assert starts == [("gemini-4-argon", True)]
    assert tmux_safe.pane_alive("agent")
    assert (root / "checkpoint.txt").read_text() == "training completed"
    assert runtime_settings._get_pending() == []


def test_pending_model_change_does_not_inspect_or_restart_while_paused(arui_env, fake_subprocess, monkeypatch):
    from backend.app import runtime_settings, notify, research_processes, agent_watcher
    runtime_settings._set_pending(["agent"])
    assert notify.set_research_paused(True)["ok"]
    def unexpected(*args, **kwargs):
        raise AssertionError("paused research must not inspect or restart agent workloads")
    monkeypatch.setattr(research_processes, "active_session_workloads", unexpected)
    result = agent_watcher._check_pending_settings()
    assert result["pending"] == ["agent"]
    assert result["pending_reasons"] == {"agent": "research_paused"}
    assert result["warnings"] == []


def test_pending_inspection_failure_keeps_saved_intent(arui_env, fake_subprocess, monkeypatch):
    from backend.app import runtime_settings, research_processes, tmux_safe, agent_watcher
    runtime_settings._set_pending(["agent"])
    monkeypatch.setattr(tmux_safe, "pane_alive", lambda role: True)
    def broken(*args):
        raise OSError("cannot inspect")
    monkeypatch.setattr(research_processes, "active_session_workloads", broken)
    result = agent_watcher._check_pending_settings()
    assert result["pending"] == ["agent"]
    assert result["pending_reasons"] == {"agent": "inspection_failed"}
    assert result["warnings"]
    assert runtime_settings._get_pending() == ["agent"]
