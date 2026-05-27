"""Paper compile — latexmk wrapper with build-log capture.

Compiles paper/main.tex → paper/build/main.pdf. Caches the last build
status and surfaces the build log so the UI can render errors clearly.

Falls back gracefully when latexmk isn't installed: returns a clear
error in the build log, never raises.
"""
from __future__ import annotations

import datetime as dt
import shutil
import subprocess
import threading
from pathlib import Path

from . import paper
from .bus import bus

_LOCK = threading.Lock()
_LAST_BUILD: dict = {
    "at": "", "ok": False, "stale": True, "log": "", "elapsed_sec": 0.0,
}


def status() -> dict:
    return dict(_LAST_BUILD)


def _have_latexmk() -> bool:
    return shutil.which("latexmk") is not None


def _have_pdflatex() -> bool:
    return shutil.which("pdflatex") is not None


def _pdf_path() -> Path | None:
    folder = paper.paper_folder()
    if not folder:
        return None
    return folder / "build" / "main.pdf"


def _is_stale(folder: Path) -> bool:
    """The PDF is stale if any .tex file is newer."""
    pdf = folder / "build" / "main.pdf"
    if not pdf.exists():
        return True
    pdf_mtime = pdf.stat().st_mtime
    for p in folder.rglob("*.tex"):
        if p.stat().st_mtime > pdf_mtime:
            return True
    return False


def needs_rebuild() -> bool:
    folder = paper.paper_folder()
    if not folder:
        return False
    return _is_stale(folder)


def build(force: bool = False) -> dict:
    """Run latexmk synchronously and return a status dict."""
    global _LAST_BUILD
    folder = paper.paper_folder()
    if not folder:
        _LAST_BUILD = {"at": dt.datetime.now(dt.timezone.utc).isoformat(),
                       "ok": False, "stale": True,
                       "log": "no paper/ folder", "elapsed_sec": 0.0}
        return dict(_LAST_BUILD)
    if not force and not _is_stale(folder):
        _LAST_BUILD["stale"] = False
        return dict(_LAST_BUILD)
    main_tex = folder / "main.tex"
    if not main_tex.exists():
        _LAST_BUILD = {"at": dt.datetime.now(dt.timezone.utc).isoformat(),
                       "ok": False, "stale": True,
                       "log": "no main.tex yet — Author Agent hasn't "
                              "scaffolded it",
                       "elapsed_sec": 0.0}
        return dict(_LAST_BUILD)
    with _LOCK:
        bus.publish("paper", "build_started", {})
        t0 = dt.datetime.now(dt.timezone.utc)
        build_dir = folder / "build"
        build_dir.mkdir(exist_ok=True)
        log = ""
        ok = False
        if _have_latexmk():
            try:
                r = subprocess.run(
                    ["latexmk", "-pdf", "-interaction=nonstopmode",
                     "-output-directory=build", "main.tex"],
                    cwd=str(folder),
                    capture_output=True, text=True, timeout=240)
                log = (r.stdout or "") + "\n" + (r.stderr or "")
                ok = r.returncode == 0
            except Exception as e:                      # noqa: BLE001
                log = f"latexmk failed: {e}"
                ok = False
        elif _have_pdflatex():
            # 2 passes for refs/citations
            try:
                for _ in range(2):
                    r = subprocess.run(
                        ["pdflatex", "-interaction=nonstopmode",
                         "-output-directory=build", "main.tex"],
                        cwd=str(folder),
                        capture_output=True, text=True, timeout=180)
                    log = (r.stdout or "") + "\n" + (r.stderr or "")
                    if r.returncode != 0:
                        ok = False; break
                else:
                    ok = True
            except Exception as e:                      # noqa: BLE001
                log = f"pdflatex failed: {e}"
                ok = False
        else:
            log = ("Neither latexmk nor pdflatex found on this machine. "
                   "Install TeX Live or use the Docker image.")
            ok = False
        elapsed = (dt.datetime.now(dt.timezone.utc) - t0).total_seconds()
        _LAST_BUILD = {
            "at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "ok": bool(ok), "stale": False,
            "log": log[-30000:],
            "elapsed_sec": round(elapsed, 1),
        }
        bus.publish("paper", "build_finished",
                    {"ok": ok, "elapsed_sec": _LAST_BUILD["elapsed_sec"]})
        return dict(_LAST_BUILD)


def pdf_bytes() -> bytes | None:
    """Return the current PDF bytes (after compile)."""
    p = _pdf_path()
    if not p or not p.exists():
        return None
    try:
        return p.read_bytes()
    except OSError:
        return None
