"""Archive & restore — save the whole research state, move it to a new box.

Design (consensus of the Gemini 3 Pro / GPT-5.5 consultation):

  * the archive is a .tar.gz of DATA_DIR; it is NEVER emailed (multi-GB) — it
    is a streaming browser download, with an rsync one-liner as the escape
    hatch for server-to-server transfer;
  * SQLite and DuckDB are snapshotted to consistent copies before tarring, so
    a live write cannot corrupt the archive;
  * "full" includes checkpoints + datasets (everything needed to resume warm);
    "slim" omits them (small, fast, e-mailable);
  * restore extracts the tar straight into DATA_DIR; runs still marked
    "running" become crashed (their processes died with the old server); a
    fresh agent then reads the restored files and continues the research.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import sqlite3
import subprocess
import tarfile

from . import metrics
from .config import ARTIFACTS_DIR, DATA_DIR, DB_PATH, ROOT, WORKSPACE_DIR

_STAGE = DATA_DIR / ".archive_tmp"
_RESTORE = DATA_DIR / ".restore_tmp"
# slim archive keeps only files below this size — robustly separates code,
# logs, configs and DBs from checkpoints / dataset shards whatever they're
# named (checkpoints turn up as ckpts/*.pt, runs/<x>/step_N, *.bin, ...).
_SLIM_CAP = 25 * 1024 * 1024


def _iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _git_commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(ROOT),
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _tree_bytes(path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


# ──────────────────────────────── info ─────────────────────────────────────

def _project_dirs():
    """The per-project workspace dirs to back up. Projects now live at
    WORKSPACE_DIR/<name> (the repo root in a real deploy), so we identify
    them as immediate subdirs that carry a workspace marker (agent.log /
    program.md / _setup_prompt.txt) — this skips the repo's own code dirs
    (backend/, arui/, tests/, …) when WORKSPACE_DIR == ROOT."""
    out = []
    try:
        for child in sorted(WORKSPACE_DIR.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            if any((child / m).exists()
                   for m in ("agent.log", "program.md", "_setup_prompt.txt")):
                out.append(child)
    except Exception:
        pass
    return out


def _walk_files():
    """Yield (relative_path, size) for every file we archive: everything under
    DATA_DIR (dbs, artifacts, datasets) plus each project workspace, the
    latter keyed under a stable ``workspace/<name>/…`` prefix regardless of
    where projects physically live. Skips the archive's own scratch dirs."""
    for root, _dirs, files in os.walk(DATA_DIR):
        if ".archive_tmp" in root or ".restore_tmp" in root:
            continue
        for f in files:
            fp = os.path.join(root, f)
            try:
                sz = os.path.getsize(fp)
            except OSError:
                continue
            yield os.path.relpath(fp, DATA_DIR).replace(os.sep, "/"), sz
    for proj in _project_dirs():
        for root, _dirs, files in os.walk(proj):
            if "__pycache__" in root:
                continue
            for f in files:
                fp = os.path.join(root, f)
                try:
                    sz = os.path.getsize(fp)
                except OSError:
                    continue
                rel = "workspace/" + proj.name + "/" \
                    + os.path.relpath(fp, proj).replace(os.sep, "/")
                yield rel, sz


def info() -> dict:
    """Size of the research state, bucketed — drives the Archive modal. The
    slim size is everything below the per-file cap (so it never misses a
    differently-named checkpoint)."""
    cats = {"databases": 0, "checkpoints": 0, "datasets": 0,
            "code & logs": 0, "artifacts": 0}
    slim = 0
    for rel, sz in _walk_files():
        if rel.endswith((".db", ".duckdb", ".db-wal", ".duckdb.wal",
                         ".db-shm")):
            cats["databases"] += sz
        elif "/ckpts/" in rel or rel.endswith(
                (".pt", ".pth", ".ckpt", ".safetensors", ".bin")) \
                or "/runs/" in rel:
            cats["checkpoints"] += sz
        elif "/data/" in rel:
            cats["datasets"] += sz
        elif rel.startswith("artifacts/"):
            cats["artifacts"] += sz
        else:
            cats["code & logs"] += sz
        if sz < _SLIM_CAP:
            slim += sz
    host = os.environ.get("ARUI_PUBLIC_HOST", "<this-server>")
    return {
        "categories": cats,
        "full_bytes": sum(cats.values()),
        "slim_bytes": slim,
        "rsync": f"rsync -avzP -e ssh root@{host}:{DATA_DIR}/ ./data/",
    }


# ──────────────────────────────── archive ──────────────────────────────────

def _stage_dbs(profile: str) -> None:
    """Snapshot the two databases + a manifest into the staging dir."""
    if _STAGE.exists():
        shutil.rmtree(_STAGE, ignore_errors=True)
    _STAGE.mkdir(parents=True, exist_ok=True)
    # SQLite — the backup API gives a consistent copy even under WAL writes
    src = sqlite3.connect(str(DB_PATH))
    dst = sqlite3.connect(str(_STAGE / "autoresearch.db"))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    # DuckDB — checkpoint + copy
    try:
        metrics.snapshot(str(_STAGE / "metrics.duckdb"))
    except Exception as e:                           # noqa: BLE001
        print(f"[archive] duckdb snapshot warning: {e}", flush=True)
    manifest = {
        "archive_schema_version": 1,
        "profile": profile,
        "created_at": _iso(),
        "autoresearcher_git_commit": _git_commit(),
        "note": "Extract this tarball into the new install's data/ directory.",
    }
    (_STAGE / "manifest.json").write_text(json.dumps(manifest, indent=2))


def _stage_trees() -> None:
    """Symlink the project workspaces + artifacts into the staging dir under a
    stable ``workspace/<name>`` + ``artifacts`` layout. The tar runs with -h
    (dereference) so these are archived as real files regardless of where the
    projects physically live — keeping the on-disk archive format stable."""
    ws = _STAGE / "workspace"
    ws.mkdir(exist_ok=True)
    for proj in _project_dirs():
        link = ws / proj.name
        try:
            if not link.exists():
                link.symlink_to(proj.resolve(), target_is_directory=True)
        except Exception:
            pass
    art = DATA_DIR / "artifacts"
    if art.exists():
        try:
            la = _STAGE / "artifacts"
            if not la.exists():
                la.symlink_to(art.resolve(), target_is_directory=True)
        except Exception:
            pass


def _staged_walk():
    """(rel_to_STAGE, size) for every file reachable from the staging dir,
    following the workspace/artifacts symlinks."""
    for root, _dirs, files in os.walk(_STAGE, followlinks=True):
        for f in files:
            fp = os.path.join(root, f)
            try:
                sz = os.path.getsize(fp)
            except OSError:
                continue
            yield os.path.relpath(fp, _STAGE).replace(os.sep, "/"), sz


def stream(profile: str = "full"):
    """Generator yielding a .tar.gz of the whole research state. 'slim' keeps
    only files below the per-file cap — no checkpoints or dataset shards,
    whatever they are named. Cleans up the staging dir when finished."""
    profile = "slim" if profile == "slim" else "full"
    _stage_dbs(profile)
    _stage_trees()
    cmd = ["tar", "--use-compress-program=gzip -1", "-cf", "-", "-h",
           "--ignore-failed-read", "--warning=no-file-changed",
           "--exclude=__pycache__", "--exclude=*.pyc",
           "--exclude=.archive_tmp", "--exclude=.restore_tmp"]
    always = ["autoresearch.db", "metrics.duckdb", "manifest.json"]
    if profile == "slim":
        listfile = _STAGE / "slim_files.txt"
        with open(listfile, "w") as lf:
            for rel, sz in _staged_walk():
                if rel in always or rel.startswith("slim_files"):
                    continue
                if "__pycache__" in rel or sz >= _SLIM_CAP:
                    continue
                lf.write(rel + "\n")
        cmd += ["-C", str(_STAGE)] + always + ["--files-from", str(listfile)]
    else:
        members = always + [d for d in ("workspace", "artifacts")
                            if (_STAGE / d).exists()]
        cmd += ["-C", str(_STAGE)] + members
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)
    try:
        while True:
            chunk = proc.stdout.read(256 * 1024)
            if not chunk:
                break
            yield chunk
        proc.wait()
    finally:
        try:
            proc.kill()
        except Exception:
            pass
        shutil.rmtree(_STAGE, ignore_errors=True)


def archive_filename(profile: str) -> str:
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"autoresearcher-{('slim' if profile == 'slim' else 'full')}-{ts}" \
           f".tar.gz"


# ──────────────────────────────── restore ──────────────────────────────────

def restore(tar_path: str) -> dict:
    """Extract an archive into DATA_DIR, replacing the current (empty) state.
    Marks any 'running' run crashed — its process died with the old server.
    Returns a summary dict."""
    from .db import SessionLocal, engine
    from .models import Event, Run

    if _RESTORE.exists():
        shutil.rmtree(_RESTORE, ignore_errors=True)
    _RESTORE.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path) as t:
        t.extractall(_RESTORE)

    # bring the databases live
    db_src = _RESTORE / "autoresearch.db"
    if not db_src.exists():
        shutil.rmtree(_RESTORE, ignore_errors=True)
        raise ValueError("archive has no autoresearch.db — not a valid "
                         "autoresearcherUI archive")
    engine.dispose()
    for side in ("-wal", "-shm"):
        p = DB_PATH.parent / (DB_PATH.name + side)
        try:
            p.unlink()
        except OSError:
            pass
    shutil.copy(str(db_src), str(DB_PATH))
    duck_src = _RESTORE / "metrics.duckdb"
    if duck_src.exists():
        metrics.swap(str(duck_src))

    # restore the artifacts tree (back under DATA_DIR)
    art_src = _RESTORE / "artifacts"
    if art_src.exists():
        dest = DATA_DIR / "artifacts"
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        shutil.move(str(art_src), str(dest))
    # restore each project workspace to WORKSPACE_DIR/<name> (the repo root in
    # a real deploy — was DATA_DIR/workspace pre-2026-06-10; the archive's
    # internal layout stays workspace/<name> for back-compat).
    ws_src = _RESTORE / "workspace"
    if ws_src.exists():
        try:
            WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        for proj in ws_src.iterdir():
            dest = WORKSPACE_DIR / proj.name
            if dest.exists():
                shutil.rmtree(dest, ignore_errors=True)
            try:
                shutil.move(str(proj), str(dest))
            except Exception:
                pass
    shutil.rmtree(_RESTORE, ignore_errors=True)

    # the old server's tmux/training processes are gone — no run is "running"
    db = SessionLocal()
    interrupted = 0
    try:
        for run in db.query(Run).filter(Run.status == "running").all():
            run.status = "crashed"
            run.ended_at = _iso()
            interrupted += 1
        if interrupted:
            db.add(Event(id="ev-" + os.urandom(4).hex(),
                         type="run_finished", severity="warning",
                         actor="system",
                         message=f"{interrupted} run(s) interrupted by a "
                                 f"server move — agent will resume",
                         created_at=_iso()))
        n_runs = db.query(Run).count()
        db.commit()
    finally:
        db.close()
    return {"status": "restored", "runs": n_runs,
            "interrupted": interrupted}
