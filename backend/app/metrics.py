"""The metric store: DuckDB (doc 06 / doc 11 D2).

All metric time-series live here, not in SQLite. DuckDB is the analytical
engine; in production each run additionally shards to its own Parquet file
(data/metrics/<run_id>.parquet) which DuckDB queries directly. For the scaffold
a single DuckDB file is used - the data flow and query API are identical.
"""
from __future__ import annotations

import datetime as dt
import math
import shutil
import threading
import time

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
# Maintain a tiny table of seen-keys per ingest, so /api/metrics/keys is
# O(1)-ish rather than a DISTINCT-scan over the whole metrics table.
_con.execute(
    """CREATE TABLE IF NOT EXISTS metric_keys (
           key          VARCHAR PRIMARY KEY,
           last_seen_at VARCHAR
       )"""
)
# Cache for the bucketed-batch query — keyed by a tuple of all parameters
# that affect the result. We never bound it explicitly (typical project has
# ~hundreds of (run, key) pairs); evicted lazily when a running-run cache
# entry is stale by more than 0.5 s.
_batch_cache: dict[tuple, dict] = {}
_batch_cache_last_recompute: dict[str, float] = {}   # run_id -> wall
_batch_lock = threading.Lock()


def _iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


# Automatic key aliasing: the agent (or someone porting from wandb) often
# logs `loss` / `accuracy` instead of the canonical `train_loss` / `train_acc`
# the dashboard expects for its "default plots" section. Rather than spamming
# the user with "(not logged)" on every kept_novel run, normalize at ingest.
#
# IMPORTANT: aliasing is a RENAME, not a duplicate — the canonical key is
# what gets stored. Users who deliberately log a non-canonical key (e.g.
# `accuracy` to mean something domain-specific) can override by also logging
# `train_acc` directly; the canonical key wins because both paths land at
# the same storage location.
KEY_ALIASES: dict[str, str] = {
    # train loss aliases
    "loss":             "train_loss",
    "training_loss":    "train_loss",
    "train/loss":       "train_loss",
    # train acc aliases
    "acc":              "train_acc",
    "accuracy":         "train_acc",
    "training_acc":     "train_acc",
    "train/acc":        "train_acc",
    "train/accuracy":   "train_acc",
    # val loss aliases
    "validation_loss":  "val_loss",
    "valid_loss":       "val_loss",
    "eval_loss":        "val_loss",
    "val/loss":         "val_loss",
    # val acc aliases
    "validation_acc":   "val_acc",
    "valid_acc":        "val_acc",
    "eval_acc":         "val_acc",
    "val_accuracy":     "val_acc",
    "val/acc":          "val_acc",
    "val/accuracy":     "val_acc",
    # learning rate
    "learning_rate":    "lr",
    "lr_current":       "lr",
    # throughput
    "step_time":        "time_per_step",
    "step_time_s":      "time_per_step",
    "sec_per_step":     "time_per_step",
    "samples/sec":      "samples_per_sec",
    "throughput":       "samples_per_sec",
    "tokens_per_sec":   "samples_per_sec",
}


def canonical_key(key: str) -> str:
    """Map a user-supplied metric key to its canonical (default-plot) name
    if one exists, else return the key unchanged. Lower-cased for match,
    but the canonical form is always lowercase too so callers can compare
    directly to ``REQUIRED_DEFAULT_KEYS``."""
    if not key:
        return key
    return KEY_ALIASES.get(str(key).strip().lower(), str(key))


def append(run_id: str, points: list[dict]) -> None:
    """Append a batch of metric points for a run and update metric_keys.

    Keys are passed through :func:`canonical_key` so common synonyms
    (``loss`` → ``train_loss``, ``accuracy`` → ``train_acc``, etc.) get
    stored under the dashboard's expected default-plot names. See
    ``KEY_ALIASES``.
    """
    rows = [
        (run_id, canonical_key(str(p["key"])), int(p.get("step") or 0),
         float(p["value"]), float(p.get("wall_time") or 0.0))
        for p in points
    ]
    if not rows:
        return
    seen_keys = {r[1] for r in rows}
    now_iso = _iso()
    with _lock:
        _con.executemany(
            "INSERT INTO metrics VALUES (?, ?, ?, ?, ?)", rows)
        # INSERT OR REPLACE doesn't exist in DuckDB; use ON CONFLICT.
        _con.executemany(
            """INSERT INTO metric_keys VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET last_seen_at = EXCLUDED.last_seen_at""",
            [(k, now_iso) for k in seen_keys])
    # invalidate per-run cache entries on new data
    with _batch_lock:
        for k in list(_batch_cache.keys()):
            if k[0] == run_id:
                _batch_cache.pop(k, None)


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
    """Every distinct metric key across all runs (drives the Analysis view).
    Reads the maintained metric_keys table — no full table scan."""
    with _lock:
        rows = _con.execute(
            "SELECT key FROM metric_keys ORDER BY key").fetchall()
    out = [r[0] for r in rows]
    if not out:
        # Backfill the metric_keys table if it's empty (first run after the
        # schema change). Cheap one-time DISTINCT scan, then we're good.
        with _lock:
            scanned = _con.execute(
                "SELECT DISTINCT key FROM metrics ORDER BY key").fetchall()
            if scanned:
                _con.executemany(
                    """INSERT INTO metric_keys VALUES (?, ?)
                       ON CONFLICT(key) DO UPDATE SET last_seen_at = EXCLUDED.last_seen_at""",
                    [(r[0], _iso()) for r in scanned])
                out = [r[0] for r in scanned]
    return out


def run_keys(run_id: str) -> list[str]:
    """Keys this specific run has logged."""
    return keys(run_id)


def batch_bucketed(
        run_ids: list[str], keys_wanted: list[str],
        x_key: str = "step", x_min: float | None = None,
        x_max: float | None = None, bucket_count: int = 500,
        running_set: set[str] | None = None,
) -> dict:
    """Return server-bucketed series for (run_ids × keys_wanted) in a single
    query. Result schema (one entry per (run_id, key) pair that has data):

      {"series": [{"run_id", "key", "x" (bucket x_first, length=bucket_count),
                   "y" (y_last, NULL for empty), "y_min", "y_max"}, ...],
       "x_key": ..., "buckets": bucket_count, "x_min": ..., "x_max": ...}

    Empty buckets are emitted as NULL so client indices align across series
    in the same panel. Cache keyed by (run_id, key, x_key, x_min, x_max,
    bucket_count); cache entries for runs in `running_set` expire after
    500 ms.
    """
    if not run_ids or not keys_wanted:
        return {"series": [], "x_key": x_key, "buckets": bucket_count}
    x_col = "step" if x_key == "step" else (
        "wall_time" if x_key == "wall_time" else "step")
    bc = max(8, min(int(bucket_count), 4000))
    running_set = running_set or set()
    now = time.time()

    # Cache lookup
    out_series: list[dict] = []
    todo: list[tuple[str, str]] = []
    with _batch_lock:
        for rid in run_ids:
            for k in keys_wanted:
                ck = (rid, k, x_col, x_min, x_max, bc)
                cached = _batch_cache.get(ck)
                stale = (rid in running_set
                         and now - _batch_cache_last_recompute.get(rid, 0) > 0.5)
                if cached and not stale:
                    out_series.append(cached)
                else:
                    todo.append((rid, k))

    if todo:
        # Compute the x range if not provided. We do this once per query.
        if x_min is None or x_max is None:
            ph = ",".join(["?"] * len(run_ids))
            ph2 = ",".join(["?"] * len(keys_wanted))
            with _lock:
                row = _con.execute(
                    f"SELECT min({x_col}), max({x_col}) FROM metrics "
                    f"WHERE run_id IN ({ph}) AND key IN ({ph2})",
                    list(run_ids) + list(keys_wanted)).fetchone()
            qmin = row[0] if row and row[0] is not None else 0.0
            qmax = row[1] if row and row[1] is not None else 1.0
        else:
            qmin = float(x_min)
            qmax = float(x_max)
        if qmax <= qmin:
            qmax = qmin + 1.0

        # The single batched query. We compute the bucket integer in a
        # subquery so we can group by it and pull ARG_MAX for the line.
        # NB: arg_max in duckdb is `arg_max(value, ordering_expr)`.
        run_ph = ",".join(["?"] * len(run_ids))
        key_ph = ",".join(["?"] * len(keys_wanted))
        sql = f"""
            WITH ranged AS (
                SELECT run_id, key, {x_col} AS x, value,
                       CAST(LEAST({bc} - 1,
                            FLOOR((CAST({x_col} AS DOUBLE) - {qmin})
                                  / NULLIF({qmax} - {qmin}, 0) * {bc})
                            ) AS INTEGER) AS bucket
                FROM metrics
                WHERE run_id IN ({run_ph})
                  AND key IN ({key_ph})
                  AND {x_col} BETWEEN {qmin} AND {qmax}
            )
            SELECT run_id, key, bucket,
                   MIN(x)              AS x_first,
                   MAX(x)              AS x_last,
                   arg_max(value, x)   AS y_last,
                   MIN(value)          AS y_min,
                   MAX(value)          AS y_max
            FROM ranged
            GROUP BY run_id, key, bucket
            ORDER BY run_id, key, bucket
        """
        params = list(run_ids) + list(keys_wanted)
        with _lock:
            rows = _con.execute(sql, params).fetchall()

        # Reshape rows into dense bucket_count-long arrays per (run, key).
        grouped: dict[tuple[str, str], list[tuple[int, float, float, float, float]]] = {}
        for r in rows:
            rid, k, b, xf, xl, yl, ymin, ymax = r
            grouped.setdefault((rid, k), []).append(
                (int(b), float(xf), float(yl) if yl is not None else float("nan"),
                 float(ymin) if ymin is not None else float("nan"),
                 float(ymax) if ymax is not None else float("nan")))

        with _batch_lock:
            for (rid, k) in todo:
                entries = grouped.get((rid, k)) or []
                # Build dense arrays. Empty buckets get NULL → None in JSON.
                x_arr = [None] * bc
                y_arr = [None] * bc
                ymin_arr = [None] * bc
                ymax_arr = [None] * bc
                for b, xf, yl, ymin, ymax in entries:
                    if 0 <= b < bc:
                        # Fall back to bucket-edge x when MIN(x) NaN'd out
                        x_arr[b] = xf if not math.isnan(xf) else None
                        y_arr[b] = yl if not math.isnan(yl) else None
                        ymin_arr[b] = ymin if not math.isnan(ymin) else None
                        ymax_arr[b] = ymax if not math.isnan(ymax) else None
                cell = {
                    "run_id": rid, "key": k,
                    "x": x_arr, "y": y_arr,
                    "y_min": ymin_arr, "y_max": ymax_arr,
                }
                if entries:
                    out_series.append(cell)
                    _batch_cache[(rid, k, x_col, x_min, x_max, bc)] = cell
                    _batch_cache_last_recompute[rid] = now

    return {
        "series": out_series,
        "x_key": x_key,
        "buckets": bc,
        "x_min": x_min,
        "x_max": x_max,
    }


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
