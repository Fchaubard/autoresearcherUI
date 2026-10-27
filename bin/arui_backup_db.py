#!/usr/bin/env python3
"""Publish a consistent SQLite snapshot to a backup volume, including WAL data."""
from __future__ import annotations

import argparse
from contextlib import closing
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile


def backup_database(source: Path, destination: Path) -> None:
    source, destination = source.resolve(), destination.resolve()
    if source == destination:
        raise ValueError("Backup destination must differ from the live database")
    # SQLite operates on local disk only. Publish the closed snapshot as an
    # ordinary file so network-volume locking cannot corrupt the live store.
    with tempfile.TemporaryDirectory(prefix="arui-db-backup-") as local:
        snapshot = Path(local) / "snapshot.sqlite"
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src:
            with closing(sqlite3.connect(snapshot)) as dst:
                src.backup(dst)
                result = dst.execute("PRAGMA integrity_check").fetchall()
                if result != [("ok",)]:
                    raise RuntimeError("SQLite backup failed integrity_check")
                dst.execute("PRAGMA journal_mode=DELETE")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            # Private permissions: settings contain credentials.
            with tempfile.NamedTemporaryFile(dir=destination.parent,
                                             prefix=".arui-backup-",
                                             delete=False) as out:
                temporary = Path(out.name)
                with snapshot.open("rb") as inp:
                    shutil.copyfileobj(inp, out)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, destination)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    backup_database(args.source, args.destination)
