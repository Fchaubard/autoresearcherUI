"""Shared pytest fixtures for the unit test suite.

Each test gets a brand-new ARUI_DATA_DIR pointing at a tmp_path, and a
fresh SQLAlchemy engine bound to a SQLite file inside that directory.
We do this by clearing the cached backend.app modules and re-importing
them so module-level constants pick up the new env var.
"""
from __future__ import annotations

import importlib
import os
import sys
import uuid
from pathlib import Path

import pytest


# Backend modules that read DATA_DIR / DB_PATH at import time.
# We reload these for every test so isolation actually works.
_RELOAD_MODULES = [
    "backend.app.config",
    "backend.app.db",
    "backend.app.models",
    "backend.app.bus",
    "backend.app.metrics",
    "backend.app.monitor",
    "backend.app.maintenance",
    "backend.app.charts",
    "backend.app.notify",
    "backend.app.paper",
    "backend.app.paper_runner",
    "backend.app.paper_compile",
    "backend.app.lit_agent",
    "backend.app.author_agent",
    "backend.app.council",
    "backend.app.pi",
    "backend.app.auth",
    "backend.app.archive",
    "backend.app.realrun",
    "backend.app.orchestrator",
    "backend.app.agent",
    "backend.app.pane_stream",
    "backend.app.agent_watcher",
    "backend.app.repo",
    "backend.app.authkeys",
    "backend.app.novelty",
    "backend.app.stuck_detector",
    "backend.app.directives",
    "backend.app.scoping",
    "backend.app.lifecycle",
    "backend.app.supervisor",
    "backend.app.interrupt",
    "backend.app.experiment_isolation",
    "backend.app.purpose",
    "backend.app.api",
    "backend.app",
    "backend",
]


def _purge_backend_modules() -> None:
    # Try to dispose the SQLAlchemy engine cleanly so the SQLite file isn't
    # held open across temp-dir teardown.
    try:
        db_mod = sys.modules.get("backend.app.db")
        if db_mod is not None and hasattr(db_mod, "engine"):
            try:
                db_mod.engine.dispose()
            except Exception:
                pass
    except Exception:
        pass
    for name in list(sys.modules):
        if name == "backend" or name.startswith("backend."):
            del sys.modules[name]


@pytest.fixture
def arui_env(tmp_path, monkeypatch):
    """Point ARUI_DATA_DIR at a tmp dir, clean import cache, init the DB."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("ARUI_DATA_DIR", str(data_dir))
    # Tests may create/kill canonical sessions such as "agent" and "author".
    # Never inherit a developer's live tmux server, even when pytest is run
    # without special environment variables or from inside a tmux pane.
    # Keep the Unix socket path short even for long pytest test names.
    tmux_dir = tmp_path.parent / ("tmux-" + uuid.uuid4().hex[:10])
    tmux_dir.mkdir(mode=0o700)
    monkeypatch.setenv("TMUX_TMPDIR", str(tmux_dir))
    monkeypatch.setenv("TMUX", "")
    # Don't let the production background daemon loops (paper-runner, author
    # feed, telemetry, notify scheduler) spawn during unit tests. They are
    # `while True` daemons that outlive the test, then keep hitting whichever DB
    # is current — racing the test under inspection (e.g. a paper-runner tick
    # calls lifecycle.set_phase, which resets the remediation counter the
    # supervisor test just incremented → order-dependent failures). Tests drive
    # the tick/worker functions directly, so they never need the loops.
    monkeypatch.setenv("ARUI_DISABLE_BG", "1")
    # Keep the project workspace under the tmp data dir for test isolation.
    # Pointing it at <data>/workspace makes WORKSPACE_DIR/<name> resolve to
    # the historical data/workspace/<name> layout the tests construct, so the
    # production default (repo ROOT) never leaks real files into the repo.
    monkeypatch.setenv("ARUI_WORKSPACE_DIR", str(data_dir / "workspace"))
    # Don't accidentally let any test contact real services.
    # NOTE: backend.app.council loads .deploy/keys.env at import time and
    # may set these — we delete AFTER the import below.
    _purge_backend_modules()
    # Force re-import so config.DATA_DIR / db.engine pick up the env var.
    from backend.app import db, models, council  # noqa: F401
    db.init_db()
    # Now scrub any keys council may have just loaded from keys.env so
    # unit tests never accidentally hit a real LLM.
    for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
              "OPENCODE_API_KEY", "ARUI_FLINT_API_KEY", "ARUI_FLINT_BASE_URL", "ARUI_FLINT_MODEL"):
        monkeypatch.delenv(k, raising=False)
    yield data_dir
    # Drain any short-lived worker threads a test spawned (council reviewers,
    # author feeders, etc.) so they cannot outlive the test, re-resolve
    # SessionLocal to the NEXT test's DB, and race it (writing leases / phases
    # that flip assertions). The long-running daemon LOOPS are disabled via
    # ARUI_DISABLE_BG above, so the only live threads here are workers that
    # finish quickly; we give them a short bounded join.
    import threading
    import time
    deadline = time.time() + 1.0
    for thr in list(threading.enumerate()):
        if thr is threading.main_thread() or not thr.is_alive():
            continue
        try:
            thr.join(timeout=max(0.0, deadline - time.time()))
        except Exception:                              # noqa: BLE001
            pass
    # Stop this test's private server so real sessions cannot leak to another
    # test. Explicit env keeps cleanup safe even if a test changes TMUX.
    import subprocess
    try:
        subprocess.run(["tmux", "kill-server"], capture_output=True, timeout=5,
                       env={**os.environ, "TMUX": "", "TMUX_TMPDIR": str(tmux_dir)})
    except Exception:                                  # noqa: BLE001
        pass
    _purge_backend_modules()


@pytest.fixture
def db_session(arui_env):
    """A SessionLocal session that's closed at teardown."""
    from backend.app.db import SessionLocal
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def make_project(db_session):
    """Factory that creates a Project row and returns it."""
    from backend.app.models import Project

    def _make(**overrides):
        defaults = dict(
            id="proj-test",
            name="test-project",
            validation_metric="val_loss",
            metric_direction="minimize",
            time_budget_sec=3600,
            status="running",
            gpu_count=4,
        )
        defaults.update(overrides)
        proj = Project(**defaults)
        db_session.add(proj)
        db_session.commit()
        return proj

    return _make


@pytest.fixture
def make_run(db_session):
    """Factory that creates a Run row and returns it."""
    import datetime as dt
    import uuid

    from backend.app.models import Run

    def _make(**overrides):
        rid = overrides.pop("id", None) or f"run-{uuid.uuid4().hex[:8]}"
        defaults = dict(
            id=rid,
            project_id="proj-test",
            idea_id=f"idea-{rid}",
            run_name=rid,
            status="kept",
            is_baseline=False,
            config={},
            created_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        )
        defaults.update(overrides)
        run = Run(**defaults)
        db_session.add(run)
        db_session.commit()
        return run

    return _make


@pytest.fixture
def setting_setter(db_session):
    """Helper to write a Setting key."""
    from backend.app.models import Setting

    def _set(key, value):
        row = db_session.query(Setting).filter(Setting.key == key).first()
        if row:
            row.value = value
        else:
            db_session.add(Setting(key=key, value=value))
        db_session.commit()

    return _set


@pytest.fixture
def fake_subprocess(monkeypatch):
    """Stub out subprocess.run everywhere with a recorder.

    Returns the list of recorded calls. The default return is a tame
    completed process with stdout="" and returncode=0. Specific tests
    can override the side_effect via fake_subprocess.set_handler.
    """
    import subprocess

    class FakeCompleted:
        def __init__(self, stdout="", stderr="", returncode=0):
            self.stdout = stdout
            self.stderr = stderr
            self.returncode = returncode

    class CallList(list):
        """A list that also carries a handler-setter to swap behavior."""

        def set_handler(self, fn):
            self._handler = fn

    calls = CallList()
    calls._handler = lambda args, **kw: FakeCompleted()

    def fake_run(args, **kw):
        calls.append({"args": list(args), "kwargs": kw})
        return calls._handler(args, **kw)

    # patch subprocess.run on the subprocess module — every importer sees this
    monkeypatch.setattr(subprocess, "run", fake_run, raising=True)
    return calls


@pytest.fixture
def no_network(monkeypatch):
    """Block any actual outbound HTTP call. Tests that need a specific
    response should still monkeypatch the relevant helper."""
    import urllib.request

    def boom(*a, **kw):
        raise RuntimeError("network access blocked in unit tests")

    monkeypatch.setattr(urllib.request, "urlopen", boom)


class NudgeTerminal:
    """A tiny editable prompt model; capture reflects actual key effects."""
    def __init__(self, success_after=2, marker="›", failed_key=None):
        self.calls, self.draft, self.history = [], "", ""
        self.enters, self.clears, self.busy = 0, 0, False
        self.success_after, self.marker, self.failed_key = success_after, marker, failed_key
        self.edit_after_enter, self.capture_failure, self.cursor_override = None, None, None
        self.placeholder, self.placeholder_dim, self.cursor_x_override = "", True, None

    def screen(self):
        if self.busy:
            return self.history + "Working (3s · esc to interrupt)\n"
        return self.history + self.marker + " " + (self.draft or self.placeholder) + "\nbypass permissions on\n"

    def __call__(self, cmd, **kwargs):
        from subprocess import CompletedProcess
        self.calls.append(cmd)
        if cmd[:2] == ["tmux", "capture-pane"]:
            if self.enters and self.capture_failure is not None:
                return CompletedProcess(cmd, self.capture_failure, stdout=b"")
            screen = self.screen()
            if '-e' in cmd and self.placeholder and not self.draft and self.placeholder_dim:
                screen = screen.replace(self.marker + ' ' + self.placeholder,
                                        self.marker + ' \x1b[2m' + self.placeholder + '\x1b[0m')
            return CompletedProcess(cmd, 0, stdout=screen.encode())
        if cmd[:2] == ["tmux", "display-message"]:
            cursor = len(self.history.splitlines()) + max(1, len(self.draft.splitlines())) - 1
            row = cursor if self.cursor_override is None else self.cursor_override
            column = 2 + len(self.draft.split('\n')[-1]) if self.cursor_x_override is None else self.cursor_x_override
            value = f'{row} {column}' if '#{cursor_x}' in cmd[-1] else str(row)
            return CompletedProcess(cmd, 0, stdout=value.encode())
        failed = ("-l" in cmd if self.failed_key == "-l" else cmd[-1] == self.failed_key)
        if failed:
            return CompletedProcess(cmd, 1, stdout=b"")
        if "-l" in cmd:
            self.draft = cmd[-1]
        elif cmd[-1] == "Enter":
            self.enters += 1
            if self.edit_after_enter is not None:
                self.draft = self.edit_after_enter
            elif self.success_after is not None and self.enters >= self.success_after:
                self.draft, self.busy = "", True
        elif cmd[-1] == "C-c":
            self.draft = ""
            self.clears += 1
        return CompletedProcess(cmd, 0, stdout=b"")


@pytest.fixture
def nudge_terminal():
    return NudgeTerminal
