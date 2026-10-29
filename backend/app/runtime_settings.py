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
_PROVIDER_CONNECTION_FIELDS = {
    "claude": {"claude_token"}, "openai": {"openai_token"},
    "gemini": {"gemini_token"}, "opencode": {"opencode_token"},
    "flint": {"flint_base_url", "flint_model", "flint_token", "flint_mode",
              "flint_device", "flint_timeout_seconds"},
}
_CONNECTION_FIELDS = {"passcode", "github_token"}


def _role_model(role: str, cfg: dict) -> str:
    prefix = "research_agent" if role == "agent" else "author_agent"
    return (cfg.get(prefix + "_model") or
            (cfg.get("research_agent_model") if role == "author" else None)
            or "claude-opus-5")


def _role_effort(role: str, cfg: dict) -> str:
    if role == "author":
        return cfg.get("author_agent_effort") or cfg.get("research_agent_effort") or "high"
    return cfg.get("research_agent_effort", "high") or ""


def _relevant_fields(role: str, before: dict, after: dict) -> set[str]:
    from .model_registry import provider_for
    fields = ROLE_FIELDS[role] | _CONNECTION_FIELDS
    for cfg in (before, after):
        fields |= _PROVIDER_CONNECTION_FIELDS.get(provider_for(_role_model(role, cfg)), set())
    # An author with no explicit selection inherits the research model.
    if role == "author" and not before.get("author_agent_model") and not after.get("author_agent_model"):
        fields |= {"research_agent_model"}
    if role == "author" and not before.get("author_agent_effort") and not after.get("author_agent_effort"):
        fields |= {"research_agent_effort"}
    return fields


def changed_roles(before: dict, after: dict) -> list[str]:
    changed = {k for k in before.keys() | after.keys() if before.get(k) != after.get(k)}
    return [role for role in ROLE_FIELDS
            if changed & _relevant_fields(role, before, after)]


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
        role = {"research_agent_model": "agent", "author_agent_model": "author"}.get(key)
        if role:
            if model != before.get(key) and (not isinstance(model, str) or not model.strip()):
                raise ValueError(f"Choose a supported model for {key}.")
            model, effort = _role_model(role, after), _role_effort(role, after)
            if model == _role_model(role, before) and effort == _role_effort(role, before):
                continue
        elif model == before.get(key):
            continue
        if not isinstance(model, str) or not provider_for(model):
            raise ValueError(f"Choose a supported model for {key}.")
        if key in {"research_agent_model", "author_agent_model"}:
            from .model_registry import efforts_for
            if provider_for(model) == "openai" and effort and effort not in efforts_for(model):
                raise ValueError(f"Reasoning effort {effort!r} is not supported by the selected model {model!r}.")
            error = requirement_error(model, after, agent=True, check_ready=False)
            if error and not os.environ.get("ARUI_CLAUDE_BIN"):
                raise ValueError(error)
            if not os.environ.get("ARUI_CLAUDE_BIN"):
                try:
                    command(model, effort)
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
        continuations = (row.value or {}).get("continuations", {}) if row else {}
        continuations = {role: item for role, item in continuations.items() if role in roles}
        if continuations:
            value["continuations"] = continuations
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
    continuations = dict((row.value or {}).get("continuations", {})) if row else {}
    changed = {k for k in before.keys() | after.keys() if before.get(k) != after.get(k)}
    for role in changed_roles(before, after):
        prefix = "research_agent" if role == "agent" else "author_agent"
        previous = continuations.get(role, {})
        source_model = previous.get("source_model") or _role_model(role, before)
        target_model = _role_model(role, after)
        fields = sorted(set(previous.get("changed_fields", [])) |
                        (changed & _relevant_fields(role, before, after)))
        from .model_registry import provider_for
        native_fields = {prefix + "_model", prefix + "_effort"}
        if role == "author" and not after.get("author_agent_model"):
            native_fields.add("research_agent_model")
        if role == "author" and not after.get("author_agent_effort"):
            native_fields.add("research_agent_effort")
        native = (provider_for(source_model) == provider_for(target_model) == "openai"
                  and set(fields) <= native_fields)
        continuations[role] = {"source_model": source_model,
                               "target_model": target_model,
                               "changed_fields": fields,
                               "mode": "native_conversation" if native else "workspace_handoff"}
    if continuations:
        value["continuations"] = continuations
    if row:
        row.value = value
    else:
        db.add(Setting(key=_PENDING_KEY, value=value))


def apply_pending() -> dict:
    """Apply saved models at a safe time without terminating child training."""
    from . import notify, tmux_safe, agent_watcher, research_processes

    with _LOCK:
        result = {"applied": [], "pending": _get_pending(), "pending_reasons": {},
                  "context_preservation": {}, "warnings": []}
        if not result["pending"]:
            return result
        if notify.research_paused() or notify.research_halted()[0]:
            result["pending_reasons"] = {role: "research_paused" for role in result["pending"]}
            return result
        remaining = []
        with SessionLocal() as db:
            row = db.query(Setting).filter(Setting.key == _PENDING_KEY).first()
            continuations = (row.value or {}).get("continuations", {}) if row else {}
            row = db.query(Setting).filter_by(key="onboarding").first()
            cfg = dict(row.value) if row and isinstance(row.value, dict) else {}
        for role in result["pending"]:
            from .agent_sessions import resume_intent
            if not tmux_safe.pane_alive(role) and not resume_intent(role):
                # A future launch already reads current settings.
                continue
            from .model_registry import provider_for
            from .provider_config import flint_managed
            if provider_for(_role_model(role, cfg)) == "flint" and flint_managed(cfg):
                from .flint_manager import manager
                local = manager()
                if not local.is_ready():
                    state = local.status()
                    # Failed setup needs an explicit retry, not a watcher storm.
                    if state["status"] not in {"error", "disabled"} and not state.get("manually_stopped"):
                        state = local.prepare(str(cfg.get("flint_device") or "auto"))
                    remaining.append(role)
                    result["pending_reasons"][role] = (
                        "model_unavailable" if state["status"] in {"error", "disabled"} or state.get("manually_stopped")
                        else "model_starting")
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
            spec = continuations.get(role, {})
            mode = spec.get("mode", "workspace_handoff")
            conversation = None
            if mode == "native_conversation":
                try:
                    from .agent_sessions import codex_conversation, recovery_conversation
                    from .models import Project
                    from .config import WORKSPACE_DIR
                    from . import paper
                    with SessionLocal() as db:
                        project = db.query(Project).first()
                        workspace = ((project.repo_path or str(WORKSPACE_DIR / project.name))
                                     if project else None)
                    if role == "author":
                        workspace = paper.paper_folder()
                    if not workspace:
                        raise ValueError("No project workspace")
                    conversation = recovery_conversation(role)
                    if conversation is None:
                        conversation = codex_conversation(
                            role, workspace, continuations[role]["source_model"])
                    conversation = {**conversation, "target_model": spec["target_model"]}
                except Exception:
                    remaining.append(role)
                    result["pending_reasons"][role] = "conversation_unavailable"
                    result["warnings"].append(
                        f"Settings saved, but the {role} conversation could not be verified. "
                        "The current agent is still running; its model/effort change remains pending.")
                    continue
            else:
                from .agent_sessions import save_workspace_handoff, forget_resume
                mode = save_workspace_handoff(role, spec)
                # An explicit incompatible settings change selects a new
                # conversation after saving the available project handoff.
                forget_resume(role)
            kwargs = {"resume": True}
            if conversation is not None:
                kwargs["continuation"] = conversation
            if agent_watcher._restart_session(role, **kwargs):
                result["applied"].append(role)
                result["context_preservation"][role] = mode
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
        with SessionLocal() as db:
            queue_changes(db, before, after)
            db.commit()
        return apply_pending()
