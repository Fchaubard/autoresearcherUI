"""SQLite + SQLAlchemy: the relational metadata store.

Per doc 11 (D2) metrics do NOT live here - they live in DuckDB/Parquet
(see metrics.py). SQLite holds only small relational metadata.
"""
from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import DB_PATH

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
    future=True,
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(conn, _record):
    """WAL + a busy timeout so the simulator's writes and the API's reads
    never collide with 'database is locked'. Best-effort: some filesystems
    (network/overlay mounts) do not support WAL - fall back silently."""
    cur = conn.cursor()
    try:
        cur.execute("PRAGMA journal_mode=WAL")
    except Exception:
        pass
    try:
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        pass
    cur.close()
SessionLocal = sessionmaker(bind=engine, autoflush=False, future=True)


class Base(DeclarativeBase):
    pass


def get_session():
    """FastAPI dependency: yields a session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from . import models  # noqa: F401  (register mappers)
    Base.metadata.create_all(engine)
    # ── Additive migrations for paper-mode columns on `run` ─────────────
    # SQLAlchemy's create_all does NOT add new columns to existing tables,
    # so we ALTER manually here. SQLite supports ADD COLUMN with a default.
    # This is idempotent — we check pragma first.
    _migrate_run_paper_columns()


def _migrate_run_paper_columns() -> None:
    """Add paper-mode columns to the run table if they don't exist yet.
    Safe to run on every boot. Tolerates the table not existing yet
    (which happens on a fresh install before create_all)."""
    with engine.connect() as conn:
        # Check existing columns
        try:
            cols = {row[1] for row in conn.exec_driver_sql(
                "PRAGMA table_info(run)").fetchall()}
        except Exception:
            return
        if not cols:
            return  # table doesn't exist; create_all will handle it
        new_columns = [
            ("context",            "VARCHAR DEFAULT 'research'"),
            ("paper_claim_id",     "VARCHAR DEFAULT ''"),
            ("paper_figure_id",    "VARCHAR DEFAULT ''"),
            ("paper_role",         "VARCHAR DEFAULT ''"),
            ("task_type",          "VARCHAR DEFAULT 'compute'"),
            ("integration_status", "VARCHAR DEFAULT 'pending'"),
            ("n_seeds",            "INTEGER DEFAULT 1"),
            ("depends_on",         "JSON DEFAULT '[]'"),
            ("compare_to_run_id",  "VARCHAR DEFAULT ''"),
            ("compare_to_baseline_id", "VARCHAR DEFAULT ''"),
            ("gpus_required",      "INTEGER DEFAULT 1"),
            ("est_time_sec",       "INTEGER DEFAULT 0"),
            ("paper_seed_group",   "VARCHAR DEFAULT ''"),
        ]
        for name, decl in new_columns:
            if name not in cols:
                try:
                    conn.exec_driver_sql(
                        f"ALTER TABLE run ADD COLUMN {name} {decl}")
                except Exception as e:
                    print(f"[db] migrate run.{name} failed: {e}",
                          flush=True)
        conn.commit()
