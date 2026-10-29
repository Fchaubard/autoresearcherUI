"""Bounded, persistent reuse of identical completed advisory work.

Keys include the actual evidence, exact selected models/efforts and prompts.
Failures are never reusable. This module performs no inference or fallback.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
import threading
import time
from pathlib import Path

_LOCK = threading.RLock()
_FLIGHTS: dict[str, list] = {}
_MAX_ENTRIES = 128
_MAX_AGE = 24 * 3600
REVIEW_METADATA = frozenset({"review", "reviews", "strategic_review", "strategic_review_id"})
SOURCE_EXTENSIONS = frozenset({".py", ".md", ".yaml", ".yml", ".json", ".toml", ".cfg", ".sh"})
SKIP_DIRS = frozenset({"__pycache__", ".git", ".venv", "venv", "data", "ckpts",
                       "checkpoints", "runs", "wandb", "node_modules"})
# Root-level progress records written by the app's research loop. These do
# not change source correctness. Arbitrary *_STATUS files, literature reviews,
# program.md, ideas.md and experiment configuration remain review evidence.
PROGRESS_FILES = frozenset({"AGENT_STATE.md", "ARUI_HANDOFF.md", "lessons.md",
                            "results.md", "RESULTS.md", "STATUS.md",
                            "RESUME_STATUS.md", "FOCUS_STATUS.md", "CORE_SPSA_STATUS.md"})


def experiment_config(config) -> dict:
    """Remove application-owned review fields, never experimental subkeys."""
    return {k: v for k, v in config.items() if k not in REVIEW_METADATA} if isinstance(config, dict) else {}


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     default=str).encode()).hexdigest()


def source_files(workspace: str):
    """Shared prompt/hash scope; never descend into data or checkpoint trees."""
    ws = Path(workspace)
    eligible = []
    def unreadable(exc):
        raise exc
    if not ws.exists():
        return
    for root, directories, names in os.walk(ws, followlinks=False, onerror=unreadable):
        directories[:] = sorted(d for d in directories if d not in SKIP_DIRS)
        for name in names:
            path = Path(root) / name
            if path.parent == ws and name in PROGRESS_FILES:
                continue
            if path.is_file() and (path.suffix.lower() in SOURCE_EXTENSIONS or name in {"Dockerfile", "Makefile"}):
                eligible.append(path)
    yield from sorted(eligible)


def source_fingerprint(workspace: str) -> str:
    """Hash every eligible source byte, including tails omitted from prompts."""
    ws = Path(workspace)
    hasher = hashlib.sha256()
    for path in source_files(workspace):
        rel = path.relative_to(ws)
        hasher.update(rel.as_posix().encode() + b"\0")
        # A read failure cannot grant a cached approval for unseen source.
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                hasher.update(block)
        hasher.update(b"\0")
    return hasher.hexdigest()


def get(namespace: str, key: str) -> dict | None:
    from .db import SessionLocal
    from .models import Setting
    with _LOCK, SessionLocal() as db:
        row = db.query(Setting).filter(Setting.key == "review_cache:" + namespace).first()
        entry = (row.value or {}).get(key) if row and isinstance(row.value, dict) else None
        if not isinstance(entry, dict) or time.time() - entry.get("stored_at", 0) > _MAX_AGE:
            return None
        return json.loads(json.dumps(entry["value"]))


def put(namespace: str, key: str, value: dict) -> None:
    from .db import SessionLocal
    from .models import Setting
    with _LOCK, SessionLocal() as db:
        row = db.query(Setting).filter(Setting.key == "review_cache:" + namespace).first()
        now = time.time()
        entries = {k: v for k, v in (row.value or {}).items()
                   if isinstance(v, dict) and now - v.get("stored_at", 0) <= _MAX_AGE} if row else {}
        entries[key] = {"stored_at": now, "value": value}
        entries = dict(sorted(entries.items(), key=lambda item: item[1]["stored_at"])[-_MAX_ENTRIES:])
        if row:
            row.value = entries
        else:
            db.add(Setting(key="review_cache:" + namespace, value=entries))
        db.commit()


@contextmanager
def single_flight(key: str):
    """Concurrent matching crashes share the first completed diagnosis."""
    with _LOCK:
        entry = _FLIGHTS.setdefault(key, [threading.Lock(), 0])
        entry[1] += 1
    try:
        with entry[0]:
            yield
    finally:
        with _LOCK:
            entry[1] -= 1
            if entry[1] == 0:
                _FLIGHTS.pop(key, None)
