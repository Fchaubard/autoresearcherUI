"""Backups must include committed WAL rows and survive failed replacements."""
import importlib.util
from pathlib import Path
import sqlite3

import pytest


spec = importlib.util.spec_from_file_location(
    "arui_backup_db", Path(__file__).resolve().parents[2] / "bin/arui_backup_db.py")
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


def test_backup_includes_live_wal_and_keeps_database_writable(tmp_path):
    source, destination = tmp_path / "live.db", tmp_path / "backups/latest.sqlite"
    with sqlite3.connect(source) as live:
        live.execute("PRAGMA journal_mode=WAL")
        live.execute("CREATE TABLE item (value TEXT)")
        live.execute("INSERT INTO item VALUES ('committed in WAL')")
        live.commit()
        backup.backup_database(source, destination)
        with sqlite3.connect(destination) as restored:
            assert restored.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
            assert restored.execute("SELECT value FROM item").fetchall() == [
                ("committed in WAL",)]
        live.execute("INSERT INTO item VALUES ('still writable')")
        live.commit()
    assert destination.stat().st_mode & 0o777 == 0o600


def test_bad_source_preserves_previous_backup(tmp_path):
    source, destination = tmp_path / "broken.db", tmp_path / "backup.sqlite"
    source.write_bytes(b"not a sqlite database")
    destination.write_bytes(b"previous snapshot")
    with pytest.raises(sqlite3.DatabaseError):
        backup.backup_database(source, destination)
    assert destination.read_bytes() == b"previous snapshot"


def test_backup_cannot_replace_live_database(tmp_path):
    source = tmp_path / "live.db"
    with pytest.raises(ValueError):
        backup.backup_database(source, source)
