"""Namespaced Settings — typed helpers for the Setting key/value store.

PR 7 of the state-control rewrite (2026-06-05). The legacy convention
was to stuff every onboarding field into a single ``onboarding``
Setting row (one mega-dict per project). New code uses namespaced
single-purpose keys instead — easier to evolve, easier to migrate,
easier to inspect.

Conventions:
  * Keys are dot-separated: ``orchestrator.phase``, ``health.idle_since``,
    ``watchdog.config``, ``pi.cadence_minutes``, ``council.consult_costs``.
  * Values are JSON dicts (or sometimes JSON-friendly primitives).
  * This module is a thin wrapper around ``Setting`` rows — it just
    gives us a typed surface so call sites don't all repeat the same
    ``db.query(Setting).filter(...)`` boilerplate.

NB: This is additive. Existing ``Setting.key == "onboarding"`` paths
continue to work; we don't migrate them en masse in PR 7 to avoid a
risky cross-cutting change.
"""
from __future__ import annotations

from typing import Any

from .db import SessionLocal
from .models import Setting


def get(key: str, default: Any = None) -> Any:
    """Read a namespaced setting. Returns ``default`` if the row is
    missing or stores something other than a dict/primitive."""
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(Setting.key == key).first()
        if row is None:
            return default
        return row.value if row.value is not None else default
    finally:
        db.close()


def set(key: str, value: Any) -> None:
    """Write a namespaced setting. Overwrites whatever was there."""
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(Setting.key == key).first()
        if row is None:
            db.add(Setting(key=key, value=value))
        else:
            row.value = value
        db.commit()
    finally:
        db.close()


def delete(key: str) -> bool:
    """Drop a namespaced setting. Returns True if anything was deleted."""
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(Setting.key == key).first()
        if row is None:
            return False
        db.delete(row)
        db.commit()
        return True
    finally:
        db.close()


def list_keys(prefix: str = "") -> list[str]:
    """List every Setting key (optionally filtered by prefix). Used by
    the Settings introspection endpoint + tests."""
    db = SessionLocal()
    try:
        rows = db.query(Setting).all()
        out = [r.key for r in rows]
        if prefix:
            out = [k for k in out if k.startswith(prefix)]
        return sorted(out)
    finally:
        db.close()
