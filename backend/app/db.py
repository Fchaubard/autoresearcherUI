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
