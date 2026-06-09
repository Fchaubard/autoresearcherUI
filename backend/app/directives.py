"""Directives queue — JSONL command queue that replaces ideas.md as the
authoritative work source for the research agent (RESEARCH_IMPROVEMENT_PLAN #1).

Why this exists
---------------
``ideas.md`` is a markdown suggestion box: the council writes rows, the agent
*may* read them, but nothing makes the agent stop and act. Production logs
show the council issued the same blocker 40 times in a row and the agent
ignored each one while burning GPU on duplicate ensembles.

``directives.jsonl`` is a *command queue*. Each line is a structured
directive with an ``id``, ``type`` and ``status``. Types carry hard
semantics that the API gate (``/api/track/run``) enforces:

  - ``BLOCKER_INFRA``  — implementation work that MUST land before more
    science is allowed. Blocks every non-``_probe``/``_smoke`` run while
    open.
  - ``BLOCKER_EVAL``   — same hard block, scoped at the evaluation
    pipeline (e.g. "land ``metric_backend='trusted_eval'``").
  - ``SCIENCE``        — a research experiment. Only allowed to run if it
    is the top open directive AND its ``blocked_by`` list is empty.
  - ``HALT``           — stop everything. Blocks ALL runs including
    probes/smokes; the human PI must clear it.
  - ``SEED_REPLICATE`` — explicit replicate request that bypasses the
    novelty-hash duplicate killer.

The file lives at ``$workspace/directives.jsonl`` alongside ``ideas.md``;
``ideas.md`` is preserved as a read-only render surface so the existing
dashboard widgets keep working during the migration.

Each line in directives.jsonl is a JSON object with this schema::

    {"id": "d-001",                # auto-generated d-<8 hex>
     "type": "BLOCKER_INFRA",      # one of TYPES below
     "priority": 1000,             # int; higher = run sooner
     "what": "...",                # one-line description
     "acceptance": "...",          # how we know it's done
     "status": "open",             # open | done | vetoed
     "blocked_by": ["d-002"],      # optional list of ids
     "idea_class": "INFRA",        # INCREMENTAL | ORTHOGONAL | REPRODUCE
                                   #   | INFRA | ABLATION  (item #5)
     "why": "...",                 # optional hypothesis
     "author": "strategic:...",    # optional provenance
     "created_at": "<iso>",        # auto
     "closed_at": "<iso>",         # set when done/vetoed
     "evidence": "...",            # set when done — proof acceptance met
    }

The module is small, dependency-light (stdlib + DATA_DIR + Setting), and
testable in isolation. The hot side-effect — gating ``/api/track/run`` —
is exposed via :func:`open_blocker_kind` and :func:`open_halt` so the API
layer can return clean ``HTTP 423`` payloads without importing JSON itself.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import threading
from pathlib import Path
from typing import Iterable

from .config import DATA_DIR, WORKSPACE_DIR
from .db import SessionLocal
from .models import Setting


# ── schema ────────────────────────────────────────────────────────────────

# All directive types the council may emit. Anything else is rejected by
# :func:`validate_directive`. These names match RESEARCH_IMPROVEMENT_PLAN.md
# verbatim so the prompt and the validator agree.
TYPES = ("BLOCKER_INFRA", "BLOCKER_EVAL", "SCIENCE", "HALT", "SEED_REPLICATE")

# The two types that block real science work. The track_run gate refuses
# to register any non-_probe/_smoke run while one of these is open.
BLOCKER_TYPES = ("BLOCKER_INFRA", "BLOCKER_EVAL")

# Required idea_class values (RESEARCH_IMPROVEMENT_PLAN #5). The orthogonal
# quota validator depends on this enum; new entries SHOULD be reflected in
# council.STRATEGIC_SYSTEM verbatim or the LLM will emit something the
# validator rejects.
IDEA_CLASSES = ("INCREMENTAL", "ORTHOGONAL", "REPRODUCE", "INFRA", "ABLATION")

# Default ``status`` for newly created entries.
STATUS_OPEN = "open"
STATUS_DONE = "done"
STATUS_VETOED = "vetoed"

# Setting key holding the on-disk path override (mostly for tests). When
# absent we derive the path from the onboarding repo_name.
_PATH_OVERRIDE_KEY = "directives_path_override"

# Lock guarding all file mutations. The file is small (sub-megabyte even
# after months of research) so a coarse lock is fine.
_FILE_LOCK = threading.Lock()


def _iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


# ── path resolution ──────────────────────────────────────────────────────

def _onboarding_repo_name() -> str:
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(Setting.key == "onboarding").first()
        if not row or not isinstance(row.value, dict):
            return ""
        return (row.value.get("repo_name") or "").strip()
    finally:
        db.close()


def directives_path() -> Path | None:
    """Resolve the on-disk path for the current project's directives file.

    Search order:
      1. ``Setting('directives_path_override')`` — explicit override used by
         the unit tests so they don't have to create a workspace dir.
      2. ``$workspace/directives.jsonl`` derived from the onboarding repo
         name. Returns ``None`` if no workspace is set up yet.
    """
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(
            Setting.key == _PATH_OVERRIDE_KEY).first()
        if row and isinstance(row.value, dict):
            override = (row.value.get("path") or "").strip()
            if override:
                p = Path(override)
                p.parent.mkdir(parents=True, exist_ok=True)
                return p
    finally:
        db.close()
    name = _onboarding_repo_name()
    if not name:
        return None
    p = WORKSPACE_DIR / name / "directives.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def set_path_override(path: str | os.PathLike) -> None:
    """Persist a path override (test helper)."""
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(
            Setting.key == _PATH_OVERRIDE_KEY).first()
        if row:
            row.value = {"path": str(path)}
        else:
            db.add(Setting(key=_PATH_OVERRIDE_KEY,
                            value={"path": str(path)}))
        db.commit()
    finally:
        db.close()


# ── validation ────────────────────────────────────────────────────────────

def validate_directive(d: dict) -> tuple[bool, str]:
    """Return ``(ok, error_msg)`` — strict per-field validation.

    Required fields: ``type`` (must be in :data:`TYPES`), ``what`` (non-
    empty). ``priority`` defaults to 500 for SCIENCE, 900 for BLOCKER_EVAL,
    1000 for BLOCKER_INFRA, 9999 for HALT. ``idea_class`` is required and
    must be in :data:`IDEA_CLASSES`.

    The validator is permissive about extra keys (the council may evolve
    the schema) but rejects malformed required fields.
    """
    if not isinstance(d, dict):
        return False, "directive must be a dict"
    if d.get("type") not in TYPES:
        return False, f"type must be one of {TYPES}, got {d.get('type')!r}"
    what = (d.get("what") or "").strip()
    if not what:
        return False, "what is required"
    ic = d.get("idea_class")
    if ic is not None and ic not in IDEA_CLASSES:
        return False, (f"idea_class must be one of {IDEA_CLASSES}, "
                       f"got {ic!r}")
    bb = d.get("blocked_by")
    if bb is not None and not isinstance(bb, list):
        return False, "blocked_by must be a list of ids"
    return True, ""


def _default_priority(d_type: str) -> int:
    """Per-type priority hint when the council didn't specify one."""
    if d_type == "HALT":
        return 9999
    if d_type == "BLOCKER_INFRA":
        return 1000
    if d_type == "BLOCKER_EVAL":
        return 900
    if d_type == "SCIENCE":
        return 500
    return 100


def _default_idea_class(d_type: str) -> str:
    if d_type in ("BLOCKER_INFRA", "BLOCKER_EVAL"):
        return "INFRA"
    if d_type == "SEED_REPLICATE":
        return "REPRODUCE"
    return "INCREMENTAL"


def _normalise(d: dict) -> dict:
    """Fill in defaults + auto-id without mutating the caller's dict."""
    out = dict(d)
    out.setdefault("id", "d-" + os.urandom(4).hex())
    out["type"] = str(out["type"])
    out["what"] = str(out.get("what") or "").strip()
    out.setdefault("acceptance", "")
    out.setdefault("status", STATUS_OPEN)
    if not out.get("priority"):
        out["priority"] = _default_priority(out["type"])
    if not out.get("idea_class"):
        out["idea_class"] = _default_idea_class(out["type"])
    out.setdefault("created_at", _iso())
    return out


# ── read / write ──────────────────────────────────────────────────────────

def read_all() -> list[dict]:
    """Return every directive in the file, in file order (oldest first).

    Returns ``[]`` if the file doesn't exist or the workspace isn't set up
    yet. Malformed lines are skipped with a stderr warning — we never
    raise into the caller (the API layer relies on this).
    """
    p = directives_path()
    if not p or not p.exists():
        return []
    out: list[dict] = []
    try:
        text = p.read_text(errors="ignore")
    except OSError:
        return []
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            d = json.loads(ln)
        except Exception as e:                              # noqa: BLE001
            print(f"[directives] skipping malformed line: {e}", flush=True)
            continue
        if isinstance(d, dict):
            out.append(d)
    return out


def _write_all(directives: Iterable[dict]) -> None:
    """Atomically rewrite the file with the given directives."""
    p = directives_path()
    if not p:
        return
    tmp = p.with_suffix(p.suffix + ".tmp")
    with open(tmp, "w") as f:
        for d in directives:
            f.write(json.dumps(d, default=str) + "\n")
    os.replace(tmp, p)


def get(directive_id: str) -> dict | None:
    """Return a single directive by id, or ``None``."""
    for d in read_all():
        if d.get("id") == directive_id:
            return d
    return None


def upsert(directive: dict) -> tuple[dict, bool]:
    """Insert or update a directive. Returns ``(stored, created)`` where
    ``created`` is True if this was a brand-new id.

    Validation runs FIRST — if the directive is malformed we raise
    ``ValueError`` so the caller can return a clean 400.
    """
    ok, err = validate_directive(directive)
    if not ok:
        raise ValueError(err)
    d = _normalise(directive)
    with _FILE_LOCK:
        existing = read_all()
        idx = next((i for i, x in enumerate(existing)
                    if x.get("id") == d["id"]), None)
        created = idx is None
        if created:
            existing.append(d)
        else:
            # Preserve created_at and explicitly-set evidence on update.
            prev = existing[idx]
            prev_created = prev.get("created_at")
            if prev_created:
                d["created_at"] = prev_created
            existing[idx] = d
        _write_all(existing)
    return d, created


def close(directive_id: str, evidence: str = "",
          status: str = STATUS_DONE) -> dict | None:
    """Mark a directive ``done`` (or ``vetoed``).

    Returns the updated directive or ``None`` if no such id exists. The
    ``evidence`` string is preserved on the row for audit (the agent
    posts it when calling /api/directives/<id>/done).
    """
    if status not in (STATUS_DONE, STATUS_VETOED):
        raise ValueError(f"status must be done|vetoed, got {status!r}")
    with _FILE_LOCK:
        existing = read_all()
        for i, d in enumerate(existing):
            if d.get("id") == directive_id:
                d = dict(d)
                d["status"] = status
                d["closed_at"] = _iso()
                if evidence:
                    d["evidence"] = str(evidence)[:1000]
                existing[i] = d
                _write_all(existing)
                return d
    return None


# ── queries used by the API gate ──────────────────────────────────────────

def open_directives() -> list[dict]:
    return [d for d in read_all() if d.get("status") == STATUS_OPEN]


def open_blocker_kind() -> str | None:
    """If any ``BLOCKER_INFRA`` / ``BLOCKER_EVAL`` is open, return its
    type. Used by /api/track/run to 423 non-probe/smoke runs."""
    for d in open_directives():
        if d.get("type") in BLOCKER_TYPES:
            return d.get("type")
    return None


def open_halt() -> dict | None:
    """First open ``HALT`` directive, if any. Used to block ALL runs."""
    for d in open_directives():
        if d.get("type") == "HALT":
            return d
    return None


def top_open() -> dict | None:
    """Top of the open queue by priority (descending). Used by the stuck
    detector + the strategic council so they can compare across reviews."""
    opens = [d for d in open_directives()]
    if not opens:
        return None
    opens.sort(key=lambda d: int(d.get("priority") or 0), reverse=True)
    return opens[0]


def counts_by_idea_class() -> dict[str, int]:
    """Tally OPEN directives by idea_class (used by the 3:1 ratio
    validator in council._validate_directives_upsert)."""
    out: dict[str, int] = {}
    for d in open_directives():
        ic = d.get("idea_class") or "INCREMENTAL"
        out[ic] = out.get(ic, 0) + 1
    return out
