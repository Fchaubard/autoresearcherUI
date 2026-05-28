"""Unit tests for backend.app.db."""
from __future__ import annotations


def test_init_db_creates_tables(arui_env):
    """init_db() must create all known tables and the data dir."""
    from backend.app import db, models  # noqa: F401
    from sqlalchemy import inspect

    inspector = inspect(db.engine)
    tables = set(inspector.get_table_names())
    # A representative sample of expected tables.
    for expected in (
        "project", "idea", "run", "gpu", "event", "chat_message",
        "journal_entry", "setting", "paper_meta", "paper_proposal",
        "paper_claim", "paper_figure", "paper_baseline",
        "paper_citation", "paper_section", "paper_version",
        "paper_decision", "paper_review_sim", "paper_budget_event",
        "dataset_registry", "mode_history",
    ):
        assert expected in tables, f"missing {expected}"


def test_db_path_under_arui_data_dir(arui_env):
    """Sanity-check that DB_PATH lives inside the temp data dir."""
    from backend.app.config import DB_PATH, DATA_DIR
    assert str(DB_PATH).startswith(str(DATA_DIR))
    assert DB_PATH.exists()


def test_get_session_yields_then_closes(arui_env):
    """get_session is a FastAPI-style generator dependency."""
    from backend.app.db import get_session
    gen = get_session()
    sess = next(gen)
    assert sess is not None
    # close
    try:
        next(gen)
    except StopIteration:
        pass


def test_run_table_has_paper_mode_columns(arui_env):
    """The additive migrations must add paper-mode columns to run."""
    from backend.app import db
    with db.engine.connect() as conn:
        cols = {row[1] for row in conn.exec_driver_sql(
            "PRAGMA table_info(run)").fetchall()}
    for c in ("context", "paper_claim_id", "paper_role", "task_type",
              "integration_status", "n_seeds", "depends_on",
              "gpus_required", "est_time_sec"):
        assert c in cols, f"missing column {c}"


def test_migrate_idempotent(arui_env):
    """init_db can be called twice without error."""
    from backend.app import db
    db.init_db()
    db.init_db()  # should not raise
