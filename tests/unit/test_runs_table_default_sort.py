"""Regression test pinning the main runs table default sort.

User (Francois, 2026-06-04) wanted the dashboard runs table to default
to DESCENDING by time (newest run on top), instead of the previous
ascending default.

Three contracts pinned here:

1.  S.sortKey defaults to 'time' (sort by started_at).
2.  S.sortAsc defaults to false (i.e., descending).
3.  The paintTable() sort uses `S.sortAsc === true` for the `asc` flag,
    so the false default actually produces descending order (a previous
    `!== false` check would have flipped to ascending on a falsy value).
"""
from __future__ import annotations

import re
from pathlib import Path


APP_JS = (Path(__file__).resolve().parents[2]
          / "backend" / "app" / "static" / "app.js")


def _read():
    return APP_JS.read_text()


# ────────────────────── contract 1+2: default state ─────────────────────────

def test_default_sort_key_is_time():
    """The top-level `S` object's sortKey default must be 'time'."""
    src = _read()
    # Look for the S definition block — first 30 lines is plenty.
    head = "\n".join(src.splitlines()[:30])
    m = re.search(r"sortKey\s*:\s*'(\w+)'", head)
    assert m is not None, "sortKey default not found in S"
    assert m.group(1) == "time", (
        f"S.sortKey default is {m.group(1)!r}; must be 'time' so the "
        "runs table sorts by started_at by default.")


def test_default_sort_is_descending():
    """The top-level `S` object's sortAsc default must be false (DESC).

    User wants newest run on top. If anyone flips this back to true,
    this test fails loudly.
    """
    src = _read()
    head = "\n".join(src.splitlines()[:30])
    m = re.search(r"sortAsc\s*:\s*(true|false)", head)
    assert m is not None, "sortAsc default not found in S"
    assert m.group(1) == "false", (
        f"S.sortAsc default is {m.group(1)!r}; must be false so the "
        "main runs table defaults to DESCENDING by time (newest first).")


# ────────────────── contract 3: asc flag matches the default ────────────────

def test_paint_table_asc_flag_uses_strict_true():
    """Inside paintTable(), the `asc` flag must be derived such that
    the default `sortAsc=false` actually produces descending order.

    The old code did `S.sortAsc !== false`, which treated `undefined`
    AND any non-`false` value as ascending. With the new default of
    `false`, that check would still produce `false` (correct), but
    the matching arrow check `S.sortAsc !== false ? '▲' : '▼'` and
    the conceptual model break down if someone later defaults sortAsc
    to undefined. Pin the strict-true check so default-DESC is the
    only natural interpretation.
    """
    src = _read()
    assert "const asc = S.sortAsc === true;" in src, (
        "Expected paintTable to compute `const asc = S.sortAsc === true;` "
        "so that the default (sortAsc=false) sorts DESC by time. If you "
        "see this fail, the runs table likely sorts ASC again.")


def test_sort_arrow_uses_strict_true():
    """The column-header arrow must match the asc flag — strict true."""
    src = _read()
    assert "S.sortAsc === true ? ' ▲' : ' ▼'" in src, (
        "Header arrow rendering must use `S.sortAsc === true` so the "
        "arrow matches the actual sort direction with sortAsc=false "
        "(default DESC).")
