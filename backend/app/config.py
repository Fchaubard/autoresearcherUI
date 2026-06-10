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

# Per-project agent workspaces. A project's code (program.md, train.py,
# ideas.md, lessons.md, run logs, …) LIVES at <WORKSPACE_DIR>/<project>.
# Default to the repo ROOT so the agent actually works in ./<project>/ — easy
# to find and edit, instead of the buried ./data/workspace/<project>. The
# archive/backup is taught about this location (see archive.py). Tests set
# ARUI_WORKSPACE_DIR to a tmp dir so they never write into the real repo.
WORKSPACE_DIR = Path(os.environ["ARUI_WORKSPACE_DIR"]) \
    if os.environ.get("ARUI_WORKSPACE_DIR") else ROOT


def _ensure_root_ignored(name: str) -> None:
    """Locally ignore the repo-root project dir via .git/info/exclude so it
    never shows as untracked.

    We deliberately do NOT touch the tracked .gitignore: a dirty .gitignore
    would block `git pull --ff-only` on every deploy. .git/info/exclude is
    the per-clone, never-committed ignore list — perfect for this."""
    try:
        exclude = ROOT / ".git" / "info" / "exclude"
        if not exclude.parent.exists():       # not a git checkout — skip
            return
        line = f"/{name}"
        existing = exclude.read_text().splitlines() if exclude.exists() else []
        if line not in existing:
            with open(exclude, "a") as f:
                f.write(f"\n# autoresearcher project workspace\n{line}\n")
    except Exception:
        pass


def workspace_dir(name: str):
    """Absolute path to project <name>'s workspace (./<name>/ at the repo root
    in a real deploy), created on demand. When it lives at ROOT, register it
    in .git/info/exclude so the project tree never pollutes git status.
    Read-only callers can use ``WORKSPACE_DIR / name`` directly."""
    p = WORKSPACE_DIR / name
    try:
        p.mkdir(parents=True, exist_ok=True)
        if WORKSPACE_DIR.resolve() == ROOT.resolve():
            _ensure_root_ignored(name)
    except Exception:
        pass
    return p

HOST = os.environ.get("ARUI_HOST", "0.0.0.0")
PORT = int(os.environ.get("ARUI_PORT", "8000"))

# Auto-run the bundled example project on startup. Default OFF — a fresh
# instance shows the onboarding screen until the user completes it. The e2e
# integration test sets ARUI_AUTORUN=1 to run headlessly.
AUTORUN = os.environ.get("ARUI_AUTORUN", "0") == "1"

DATA_DIR.mkdir(parents=True, exist_ok=True)
ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
