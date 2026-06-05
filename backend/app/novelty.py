"""Novelty hashing + duplicate-killer registry (RESEARCH_IMPROVEMENT_PLAN #3).

Why this exists
---------------
The autoresearch loop has a bias toward "didn't crash = success". In
practice the agent was launching the same 5-way ensemble config five
times in a single batch, the council noticed, the agent ignored it, and
the dashboard happily logged all five duplicates.

This module is the cheap, high-ROI duplicate killer:

  1. ``canonicalise()`` strips bookkeeping/log-only keys from a config
     dict and recursively normalises nested dicts/lists so that two
     "modelling-equivalent" configs hash to the same value, regardless
     of whether one of them happened to carry a different ``run_name``
     or timestamp.

  2. ``novelty_hash()`` returns a stable 16-char SHA256 prefix of the
     canonicalised JSON. Short enough to log/display, long enough to
     make accidental collisions vanishingly rare.

  3. A module-level ``run_registry`` maps each seen hash to the run_id
     that first registered it. ``/api/track/run`` consults this on every
     POST: if the hash is known AND the request is not an explicit seed
     replicate, the registration is rejected with HTTP 409 +
     ``{"error":"duplicate","existing_run_id":...}``.

  4. ``is_seed_replicate()`` is the escape hatch — explicit replicate
     requests bypass dedup so the strategic council can still ask for
     "rerun config X with seeds 0..4" without being told it's a dup.

  5. ``populate_registry_from_db()`` walks the existing kept runs at
     startup so a restart doesn't lose dedup state (we don't persist
     the registry; it's cheap to rebuild and the source-of-truth is the
     DB).

This module deliberately keeps zero deps on FastAPI / SQLAlchemy so it
is unit-testable in isolation.
"""
from __future__ import annotations

import hashlib
import json
import threading
from typing import Any


# ─────────────────────── canonical config form ────────────────────────
#
# Keys we DROP before hashing — these are bookkeeping / human-facing /
# timing fields that have no influence on the experiment being run. Two
# configs that differ only in these keys should be considered duplicates.
LOG_ONLY_KEYS = frozenset({
    # naming/labels
    "run_name", "name", "label", "tag", "tags", "notes", "comment",
    "description", "what", "why", "title",
    # provenance/authorship
    "author", "actor", "agent", "owner", "user", "members",
    # timestamps
    "created_at", "started_at", "ended_at", "finished_at", "updated_at",
    "timestamp", "ts", "time", "datetime", "date",
    # run-id-ish / artifact paths the agent may stamp in
    "run_id", "uuid", "uid", "id", "log_dir", "out_dir", "output_dir",
    "save_dir", "checkpoint_dir", "artifact_path",
    # explicit replicate markers — used to bypass dedup, never to
    # influence the hash. (Otherwise an explicit-replicate request
    # would itself be a "new" config that pollutes the registry.)
    "seed_replicate", "idea_class",
})

# ────── replicate markers (RESEARCH_IMPROVEMENT_PLAN #3, #4) ─────
#
# Replicates are a feature, not a bug: the strategic council should be
# able to ask "run config X with 5 seeds" and have all 5 land. We
# recognise three explicit signals:
#
#   1. config["idea_class"] == "REPRODUCE"  (Gemini's recommended schema)
#   2. config["seed_replicate"] == True     (lightweight inline marker)
#   3. run_id starts with "seed_"            (path-of-least-resistance
#                                              opt-in for the agent)
REPLICATE_IDEA_CLASSES = frozenset({"REPRODUCE", "seed_replicate"})


def canonicalise(value: Any) -> Any:
    """Recursively normalise a config value into a JSON-canonicalisable
    form, dropping LOG_ONLY_KEYS along the way.

    Rules:
      - dicts: drop LOG_ONLY_KEYS, recurse on remaining values, return
        a NEW dict with keys sorted so json.dumps(sort_keys=True) is a
        no-op for stability.
      - lists/tuples: recurse on each element, keep order (order is
        meaningful for ensemble members / sweep grids).
      - sets/frozensets: convert to sorted list so element order doesn't
        affect the hash (a set is by definition unordered).
      - scalars: returned as-is. Floats are NOT rounded — the agent's
        SDK already normalises to a sensible precision, and rounding
        here would silently merge configs that the experimenter actually
        meant to distinguish.
    """
    if isinstance(value, dict):
        out: dict = {}
        for k, v in value.items():
            if str(k) in LOG_ONLY_KEYS:
                continue
            out[str(k)] = canonicalise(v)
        return dict(sorted(out.items()))
    if isinstance(value, (list, tuple)):
        return [canonicalise(v) for v in value]
    if isinstance(value, (set, frozenset)):
        # Sort by stringified form so mixed-type sets don't blow up.
        return sorted((canonicalise(v) for v in value), key=lambda x: json.dumps(x, sort_keys=True, default=str))
    # JSON-incompatible scalars (e.g. numpy types the SDK may slip
    # through) become their str form so json.dumps doesn't explode.
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def novelty_hash(config: dict | None) -> str:
    """16-char SHA256 prefix over the canonicalised config.

    ``None`` and ``{}`` both hash to the empty-config hash — two runs
    that send no config at all are duplicates of each other. (This is
    intentional: an agent that "configures nothing" is launching the
    same run twice, and the duplicate killer should catch that.)"""
    canon = canonicalise(config or {})
    blob = json.dumps(canon, sort_keys=True, separators=(",", ":"),
                      default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def is_seed_replicate(config: dict | None, run_id: str = "") -> bool:
    """True iff this run is an explicit seed-replicate request and
    therefore should bypass the duplicate killer.

    Recognised signals (any one is enough):
      - ``run_id`` startswith ``"seed_"``
      - ``config["idea_class"]`` in {"REPRODUCE", "seed_replicate"}
      - ``config["seed_replicate"]`` truthy
    """
    if str(run_id or "").startswith("seed_"):
        return True
    cfg = config or {}
    if not isinstance(cfg, dict):
        return False
    if str(cfg.get("idea_class") or "") in REPLICATE_IDEA_CLASSES:
        return True
    if cfg.get("seed_replicate"):
        return True
    return False


def is_probe_or_smoke(run_id: str) -> bool:
    """Pre-flight smoke runs are not real experiments; they always
    bypass the duplicate killer (the agent may legitimately rerun the
    same smoke probe to check the toolchain)."""
    rid = str(run_id or "")
    return rid.startswith("_probe") or rid.startswith("_smoke")


# ──────────────────── shared in-process registry ──────────────────────

_REGISTRY_LOCK = threading.Lock()
# hash -> run_id of the run that first claimed it.
run_registry: dict[str, str] = {}


def register(config: dict | None, run_id: str) -> tuple[bool, str | None, str]:
    """Atomic check-and-insert.

    Returns ``(accepted, existing_run_id, hash)``:
      - accepted=True  → hash was novel (or bypassed); registered.
      - accepted=False → duplicate found; existing_run_id is the prior
                         claimant. Caller should HTTP-409.
    """
    h = novelty_hash(config)
    # Bypass: explicit replicates and smoke probes never compete with
    # the registry. We DO still record the first one, but a collision
    # for a bypassed request is not an error.
    bypass = is_probe_or_smoke(run_id) or is_seed_replicate(config, run_id)
    with _REGISTRY_LOCK:
        prior = run_registry.get(h)
        if prior is not None and not bypass:
            return False, prior, h
        # First writer wins; bypassed runs don't overwrite an existing
        # claim (the original run_id stays the canonical one).
        if prior is None:
            run_registry[h] = run_id
        return True, None, h


def _clear_for_tests() -> None:
    """Reset the in-process registry. Used by the test suite's
    isolation fixture — production code should never call this."""
    with _REGISTRY_LOCK:
        run_registry.clear()


# ─────────────────────── startup migration ────────────────────────────

def populate_registry_from_db() -> int:
    """Walk every persisted Run and seed the registry from its config.

    Called once at backend startup. We don't persist the registry to
    disk — it's cheap to rebuild and the DB is the source of truth.

    Only kept_* runs (kept, kept_novel, kept_replicate, success_smoke)
    seed the registry; crashed/discarded runs would just pollute it
    with hashes the system has explicitly rejected.

    Returns the number of hashes loaded (for logging)."""
    try:
        from .db import SessionLocal
        from .models import Run
    except Exception:
        # If the DB isn't importable yet (e.g. in a unit test that
        # exercises this module in isolation), no-op cleanly.
        return 0

    db = SessionLocal()
    try:
        # Status taxonomy migration: old DBs may only have 'kept'; new
        # ones add kept_novel + kept_replicate + success_smoke. All of
        # these should seed the registry so the duplicate killer keeps
        # working across the migration.
        kept_statuses = ("kept", "kept_novel", "kept_replicate",
                         "success_smoke")
        runs = db.query(Run).filter(Run.status.in_(kept_statuses)).all()
        loaded = 0
        with _REGISTRY_LOCK:
            for r in runs:
                cfg = r.config if isinstance(r.config, dict) else {}
                h = novelty_hash(cfg)
                # First writer wins — if two old runs happen to share a
                # hash, the older one keeps the slot (stable across
                # restarts).
                run_registry.setdefault(h, r.id)
                loaded += 1
        return loaded
    finally:
        db.close()
