"""Paths and runtime configuration for the autoresearcherUI backend."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]        # repo root
# DATA_DIR defaults to <repo>/data; override with ARUI_DATA_DIR if the repo
# lives on a filesystem that does not support SQLite well (network/overlay FS).
DATA_DIR = Path(os.environ["ARUI_DATA_DIR"]) if os.environ.get("ARUI_DATA_DIR") \
    else ROOT / "data"
STATIC_DIR = Path(__file__).resolve().parent / "static"
DOCS_DIR = ROOT / "docs"

DB_PATH = DATA_DIR / "autoresearch.db"            # SQLite: relational metadata
METRICS_DB = DATA_DIR / "metrics.duckdb"          # DuckDB: metric analytics
ARTIFACTS_DIR = DATA_DIR / "artifacts"

HOST = os.environ.get("ARUI_HOST", "0.0.0.0")
PORT = int(os.environ.get("ARUI_PORT", "8000"))

# Auto-run the bundled example project on startup. Default OFF — a fresh
# instance shows the onboarding screen until the user completes it. The e2e
# integration test sets ARUI_AUTORUN=1 to run headlessly.
AUTORUN = os.environ.get("ARUI_AUTORUN", "0") == "1"

DATA_DIR.mkdir(parents=True, exist_ok=True)
ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
