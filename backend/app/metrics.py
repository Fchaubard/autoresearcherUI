"""The metric store: DuckDB (doc 06 / doc 11 D2).

All metric time-series live here, not in SQLite. DuckDB is the analytical
engine; in production each run additionally shards to its own Parquet file
(data/metrics/<run_id>.parquet) which DuckDB queries directly. For the scaffold
a single DuckDB file is used - the data flow and query API are identical.
"""
from __future__ import annotations

import shutil
import threading

import duckdb

from .config import METRICS_DB

_lock = threading.Lock()
_con = duckdb.connect(str(METRICS_DB))
_con.execute(
    """CREATE TABLE IF NOT EXISTS metrics (
           run_id    VARCHAR,
           key       VARCHAR,
           step      INTEGER,
           value     DOUBLE,
           wall_time DOUBLE
       )"""
)


def append(run_id: str, points: list[dict]) -> None:
    """Append a batch of metric points for a run."""
    rows = [
        (run_id, str(p["key"]), int(p.get("step") or 0),
         float(p["value"]), float(p.get("wall_time") or 0.0))
        for p in points
    ]
    if not rows:
        return
    with _lock:
        _con.executemany(
            "INSERT INTO metrics VALUES (?, ?, ?, ?, ?)", rows)


def keys(run_id: str) -> list[str]:
    with _lock:
        rows = _con.execute(
            "SELECT DISTINCT key FROM metrics WHERE run_id = ? ORDER BY key",
            [run_id]).fetchall()
    return [r[0] for r in rows]


def query(run_id: str, wanted: list[str] | None = None,
          max_points: int = 2000) -> dict[str, list[list[float]]]:
    """Return {key: [[step, value], ...]} for a run, decimated to max_points."""
    out: dict[str, list[list[float]]] = {}
    for key in (wanted or keys(run_id)):
        with _lock:
            n = _con.execute(
                "SELECT count(*) FROM metrics WHERE run_id=? AND key=?",
                [run_id, key]).fetchone()[0]
            stride = max(1, n // max_points)
            # decimate with a deterministic stride; cheap and spike-safe enough
            rows = _con.execute(
                """SELECT step, value FROM (
                       SELECT step, value,
                              row_number() OVER (ORDER BY step) AS rn
                       FROM metrics WHERE run_id=? AND key=?
                   ) WHERE rn % ? = 0 ORDER BY step""",
                [run_id, key, stride]).fetchall()
        out[key] = [[float(s), float(v)] for s, v in rows]
    return out


def last_step(run_id: str) -> int:
    """The highest step recorded for a run (-1 if none)."""
    with _lock:
        row = _con.execute(
            "SELECT max(step) FROM metrics WHERE run_id=?", [run_id]).fetchone()
    return int(row[0]) if row and row[0] is not None else -1


def latest(run_id: str, key: str) -> float | None:
    with _lock:
        row = _con.execute(
            """SELECT value FROM metrics WHERE run_id=? AND key=?
               ORDER BY step DESC LIMIT 1""", [run_id, key]).fetchone()
    return float(row[0]) if row else None


def all_keys() -> list[str]:
    """Every distinct metric key across all runs (drives the Analysis view)."""
    with _lock:
        rows = _con.execute(
            "SELECT DISTINCT key FROM metrics ORDER BY key").fetchall()
    return [r[0] for r in rows]


def last_activity(run_id: str) -> float | None:
    """Wall-clock (epoch) time of the most recent metric point for a run, or
    None if it has logged nothing. Used to tell a live run from a dead one."""
    with _lock:
        row = _con.execute(
            "SELECT max(wall_time) FROM metrics WHERE run_id=?",
            [run_id]).fetchone()
    return float(row[0]) if row and row[0] else None


def reset() -> None:
    with _lock:
        _con.execute("DELETE FROM metrics")


def snapshot(dest: str) -> None:
    """Write a consistent copy of the metric DB to dest (used by Archive)."""
    with _lock:
        try:
            _con.execute("CHECKPOINT")
        except Exception:
            pass
        shutil.copy(str(METRICS_DB), dest)


def swap(new_db_path: str) -> None:
    """Replace the live metric DB with new_db_path and reconnect. Used by
    Restore; lock-held so in-flight queries resume on the new connection."""
    global _con
    with _lock:
        try:
            _con.close()
        except Exception:
            pass
        shutil.copy(new_db_path, str(METRICS_DB))
        _con = duckdb.connect(str(METRICS_DB))
