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
# ideas.md, lessons.md, run logs, …) is STORED at <WORKSPACE_DIR>/<project>.
# The bytes stay under data/ so the archive/backup + restore (which snapshot
# DATA_DIR) keep working unchanged. For findability — the operator rightly
# complained that data/workspace/<project> is buried — workspace_dir() also
# surfaces each project as a symlink at the repo root: ./<project>/ . Tests
# set ARUI_WORKSPACE_DIR to a tmp dir for isolation (and that also disables
# the repo-root symlink so tests never write into the real repo).
WORKSPACE_DIR = Path(os.environ["ARUI_WORKSPACE_DIR"]) \
    if os.environ.get("ARUI_WORKSPACE_DIR") else DATA_DIR / "workspace"


def _ensure_root_gitignored(name: str) -> None:
    """Idempotently add `/<name>` to the repo .gitignore so the convenience
    symlink at the repo root doesn't show up as untracked."""
    try:
        gi = ROOT / ".gitignore"
        line = f"/{name}"
        existing = gi.read_text().splitlines() if gi.exists() else []
        if line not in existing:
            with open(gi, "a") as f:
                f.write(("" if (not existing or existing[-1] == "") else "\n")
                        + f"# project workspace symlink\n{line}\n")
    except Exception:
        pass


def workspace_dir(name: str):
    """Absolute path to project <name>'s STORED workspace, created on demand.

    In a real deploy (ARUI_WORKSPACE_DIR unset) it also creates a convenience
    symlink at ROOT/<name> -> the stored dir, so the operator finds the
    project at ./<name>/ instead of ./data/workspace/<name>/. Read-only
    callers can use ``WORKSPACE_DIR / name`` directly."""
    p = WORKSPACE_DIR / name
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception:
        return p
    # Repo-root convenience symlink (production only — skipped in tests, which
    # set ARUI_WORKSPACE_DIR, so we never drop symlinks into the real repo).
    if not os.environ.get("ARUI_WORKSPACE_DIR"):
        try:
            link = ROOT / name
            if not link.exists() and not link.is_symlink() \
                    and link.resolve() != p.resolve():
                link.symlink_to(p.resolve(), target_is_directory=True)
                _ensure_root_gitignored(name)
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
