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
    "backend.app.seed",
    "backend.app.orchestrator",
    "backend.app.agent",
    "backend.app.pane_stream",
    "backend.app.repo",
    "backend.app.authkeys",
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
    # Don't accidentally let any test contact real services.
    # NOTE: backend.app.council loads .deploy/keys.env at import time and
    # may set these — we delete AFTER the import below.
    _purge_backend_modules()
    # Force re-import so config.DATA_DIR / db.engine pick up the env var.
    from backend.app import db, models, council  # noqa: F401
    db.init_db()
    # Now scrub any keys council may have just loaded from keys.env so
    # unit tests never accidentally hit a real LLM.
    for k in ("GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    yield data_dir
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
