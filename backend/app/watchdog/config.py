"""Per-project watchdog config.

Persisted as a single Setting row ``watchdog.config`` whose value is::

    {
        "<script_module_name>": {
            "enabled": true | false,
            "params": {<param>: <value>, ...},
            "source": "default" | "operator" | "agent_authored"
        },
        ...
    }

The agent (or operator) can override per-script params at any time —
PR 5 adds the onboarding step where the council asks the agent if the
default thresholds make sense for the research agenda. Until that
runs, every project uses ``DEFAULT_CONFIG``.
"""
from __future__ import annotations

import importlib
from typing import Any

from ..db import SessionLocal
from ..models import Setting


_SCRIPT_NAMES = (
    "no_metric_flow",
    "nan_loss",
    "diverging",
    "gpu_oom",
    "crashed_silently",
    "done_signal",
)


def _load_default_params(name: str) -> dict:
    mod = importlib.import_module(f"backend.app.watchdog.scripts.{name}")
    return dict(getattr(mod, "DEFAULT_PARAMS", {}))


def list_scripts() -> list[dict]:
    """Return one dict per ship-default watchdog script. Used by the
    onboarding UI + the council prompt that asks the agent to review
    these defaults for the research agenda."""
    out = []
    for name in _SCRIPT_NAMES:
        mod = importlib.import_module(f"backend.app.watchdog.scripts.{name}")
        out.append({
            "name": name,
            "describe": getattr(mod, "describe", lambda: name)(),
            "default_params": dict(getattr(mod, "DEFAULT_PARAMS", {})),
            "default_enabled": getattr(mod, "DEFAULT_ENABLED", True),
            "kills_run": getattr(mod, "KILLS_RUN", False),
        })
    return out


DEFAULT_CONFIG: dict = {
    name: {
        "enabled": True,
        "params": _load_default_params(name),
        "source": "default",
    }
    for name in _SCRIPT_NAMES
}


def get_config() -> dict:
    """Read the current watchdog.config Setting row, merging missing
    entries with the package defaults. Always returns a dict with
    EVERY script populated so callers don't have to guard each lookup."""
    db = SessionLocal()
    try:
        row = (db.query(Setting)
               .filter(Setting.key == "watchdog.config").first())
        stored = (row.value if row and isinstance(row.value, dict) else {})
    finally:
        db.close()
    out: dict = {}
    for name in _SCRIPT_NAMES:
        entry = dict(DEFAULT_CONFIG[name])
        if isinstance(stored.get(name), dict):
            cust = stored[name]
            entry["enabled"] = bool(cust.get("enabled", entry["enabled"]))
            params = dict(entry["params"])
            if isinstance(cust.get("params"), dict):
                params.update(cust["params"])
            entry["params"] = params
            entry["source"] = cust.get("source", entry["source"])
        out[name] = entry
    # Surface any agent_authored extras (custom scripts the agent added
    # outside the default ship list) so they survive round trips.
    for name, cust in (stored or {}).items():
        if name not in out and isinstance(cust, dict):
            out[name] = cust
    return out


def set_config(new: dict, *, source: str = "operator") -> dict:
    """Replace the watchdog.config Setting. Validates types but accepts
    unknown keys (forward-compat). Tags each touched entry with the
    given source for audit."""
    cfg = get_config()
    for name, cust in (new or {}).items():
        if not isinstance(cust, dict):
            continue
        entry = cfg.get(name) or {"enabled": True, "params": {},
                                    "source": source}
        if "enabled" in cust:
            entry["enabled"] = bool(cust["enabled"])
        if isinstance(cust.get("params"), dict):
            merged = dict(entry.get("params") or {})
            merged.update(cust["params"])
            entry["params"] = merged
        entry["source"] = cust.get("source", source)
        cfg[name] = entry
    db = SessionLocal()
    try:
        row = (db.query(Setting)
               .filter(Setting.key == "watchdog.config").first())
        if row is None:
            db.add(Setting(key="watchdog.config", value=cfg))
        else:
            row.value = cfg
        db.commit()
    finally:
        db.close()
    return cfg


def get_script_params(name: str) -> dict:
    """Convenience: return ONLY the params dict for ``name`` (merged
    with defaults). Used by scripts when they need a parameter."""
    return dict(get_config().get(name, {}).get("params") or {})
