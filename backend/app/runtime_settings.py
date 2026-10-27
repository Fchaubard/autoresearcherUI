"""Apply saved configuration to live agent sessions without stopping experiments."""
from __future__ import annotations

import threading

from .db import SessionLocal
from .models import Setting

_LOCK = threading.RLock()
_PENDING_KEY = "settings.pending_agents"
ROLE_FIELDS = {
    "agent": {"research_agent_model", "research_agent_effort", "agent_instructions",
              "purpose", "seed_ideas", "eval", "metric", "baseline", "kill_criteria",
              "extra_gpu_nodes", "github_username", "github_email", "skip_perms"},
    "author": {"author_agent_model", "author_agent_effort"},
}
_CONNECTION_FIELDS = {"claude_token", "openai_token", "gemini_token",
                      "opencode_token", "flint_base_url", "flint_model",
                      "flint_token", "passcode", "github_token"}


def changed_roles(before: dict, after: dict) -> list[str]:
    changed = {k for k in before.keys() | after.keys() if before.get(k) != after.get(k)}
    return [role for role, fields in ROLE_FIELDS.items()
            if changed & (fields | _CONNECTION_FIELDS)]


def validate_models(before: dict, after: dict) -> None:
    """Check selections before committing, without contacting a paid service."""
    from .model_registry import provider_for
    from .provider_config import requirement_error
    from .agent_cli import command, AgentCLIUnavailable
    import os

    for key in ("research_agent_model", "author_agent_model", "scoping_model",
                "council_gemini_model", "council_openai_model",
                "council_claude_model", "pi_agent_model"):
        model = after.get(key)
        if model == before.get(key):
            continue
        if not isinstance(model, str) or not provider_for(model):
            raise ValueError(f"Choose a supported model for {key}.")
        if key in {"research_agent_model", "author_agent_model"}:
            error = requirement_error(model, after, agent=True)
            if error and not os.environ.get("ARUI_CLAUDE_BIN"):
                raise ValueError(error)
            if not os.environ.get("ARUI_CLAUDE_BIN"):
                try:
                    command(model, after.get(key.replace("_model", "_effort"), ""))
                except AgentCLIUnavailable as exc:
                    raise ValueError(str(exc)) from exc


def _get_pending() -> list[str]:
    with SessionLocal() as db:
        row = db.query(Setting).filter(Setting.key == _PENDING_KEY).first()
        return [role for role in (row.value or {}).get("roles", [])
                if role in ROLE_FIELDS] if row else []


def _set_pending(roles: list[str]) -> None:
    with SessionLocal() as db:
        row = db.query(Setting).filter(Setting.key == _PENDING_KEY).first()
        value = {"roles": sorted(set(roles))}
        if row:
            row.value = value
        else:
            db.add(Setting(key=_PENDING_KEY, value=value))
        db.commit()


def queue_changes(db, before: dict, after: dict) -> None:
    """Persist restart intent in the same transaction as the saved settings."""
    row = db.query(Setting).filter(Setting.key == _PENDING_KEY).first()
    pending = (row.value or {}).get("roles", []) if row else []
    value = {"roles": sorted(set(pending) | set(changed_roles(before, after)))}
    if row:
        row.value = value
    else:
        db.add(Setting(key=_PENDING_KEY, value=value))


def apply_pending() -> dict:
    """Apply saved models at a safe time without terminating child training."""
    from . import notify, tmux_safe, agent_watcher, research_processes

    with _LOCK:
        result = {"applied": [], "pending": _get_pending(), "pending_reasons": {}, "warnings": []}
        if not result["pending"]:
            return result
        if notify.research_paused() or notify.research_halted()[0]:
            result["pending_reasons"] = {role: "research_paused" for role in result["pending"]}
            return result
        remaining = []
        for role in result["pending"]:
            if not tmux_safe.pane_alive(role):
                # A future launch already reads current settings.
                continue
            try:
                busy = research_processes.active_session_workloads(role)
            except Exception:
                remaining.append(role)
                result["pending_reasons"][role] = "inspection_failed"
                result["warnings"].append(
                    f"Settings saved, but active experiments in the {role} session "
                    "could not be checked. The model change will retry automatically.")
                continue
            if busy:
                remaining.append(role)
                result["pending_reasons"][role] = "active_experiments"
                continue
            if agent_watcher._restart_session(role, resume=True):
                result["applied"].append(role)
            else:
                remaining.append(role)
                # A child can start, or the operator can pause, between the
                # first inspection and the restart lock's final check.
                if notify.research_paused() or notify.research_halted()[0]:
                    result["pending_reasons"][role] = "research_paused"
                    continue
                try:
                    if research_processes.active_session_workloads(role):
                        result["pending_reasons"][role] = "active_experiments"
                        continue
                except Exception:
                    result["pending_reasons"][role] = "inspection_failed"
                    result["warnings"].append(
                        f"Settings saved, but the {role} session could not be inspected. "
                        "The model change will retry automatically.")
                    continue
                result["pending_reasons"][role] = "restart_failed"
                result["warnings"].append(
                    f"Settings saved, but the {role} session could not restart. "
                    "Use its Restart button to retry.")
        if remaining != result["pending"]:
            _set_pending(remaining)
        result["pending"] = remaining
        return result


def apply_changes(before: dict, after: dict) -> dict:
    with _LOCK:
        roles = changed_roles(before, after)
        _set_pending(_get_pending() + roles)
        return apply_pending()
