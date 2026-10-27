"""Research interrupt + resume flow.

The operator hits a big red "Halt Research" button when the autoresearcher goes
off the rails. This module implements the durable interrupt:

  hard_interrupt()            - YES on the "Are you sure?" modal: stop ALL runs
                                and queued work RIGHT NOW, pause the loop, kill
                                every killable run session (never the main
                                agent), append the focus to lessons.md, and
                                raise a high-priority `d-interrupt-focus`
                                directive.
  restart_with_feedback()     - re-evaluate against an updated purpose + seeds:
                                update onboarding, append the feedback, refresh
                                the focus directive, and re-enter scoping (which
                                re-requires PI + council approval) with the code
                                pruned to the new direction by the agent.
  resume_from_interrupt()     - "Continue and Ignore Feedback" / resume: refuse
                                if halted or an open HALT directive exists,
                                unpause, restart the main agent if its tmux
                                session vanished, else send a checkpoint-aware
                                resume message, and publish the resumed state.

Everything is defensive: the MAIN research agent session is protected via
tmux_safe, and each step is best-effort so a single failure never wedges the
whole interrupt.
"""
from __future__ import annotations

import datetime as dt

from .safe_errors import describe as _safe_error

INTERRUPT_DIRECTIVE_ID = "d-interrupt-focus"


def _iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _onboarding() -> dict:
    from .db import SessionLocal
    from .models import Setting
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(Setting.key == "onboarding").first()
        return dict(row.value) if row and isinstance(row.value, dict) else {}
    finally:
        db.close()


def _save_onboarding(cfg: dict) -> None:
    from .db import SessionLocal
    from .models import Setting
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(Setting.key == "onboarding").first()
        if row:
            row.value = cfg
        else:
            db.add(Setting(key="onboarding", value=cfg))
        db.commit()
    finally:
        db.close()


def lessons_append(text: str) -> bool:
    """Append an operator focus/feedback note to the project's lessons.md so
    the agent, PI, and council all re-read the new direction. Best-effort."""
    text = (text or "").strip()
    if not text:
        return False
    try:
        from . import council
        p = council._lessons_path()
        if not p:
            return False
        block = (f"\n\n## OPERATOR INTERRUPT — {_iso()}\n"
                 "The researcher HALTED the run and set this focus. Treat it as "
                 "the highest-priority instruction; drop anything that "
                 "conflicts with it.\n\n" + text + "\n")
        with open(p, "a", encoding="utf-8") as f:
            f.write(block)
        return True
    except Exception:                                      # noqa: BLE001
        return False


def set_interrupt_focus_directive(purpose: str, seeds: str,
                                  feedback: str) -> dict | None:
    """Create/refresh the high-priority `d-interrupt-focus` SCIENCE directive
    that re-anchors the agent on the (possibly updated) purpose + seeds."""
    try:
        from . import directives
        what = ("OPERATOR INTERRUPT FOCUS. Re-anchor ALL work on this purpose "
                "and these seed ideas; abandon anything that does not serve "
                "them.\n\nPURPOSE:\n" + (purpose or "").strip() +
                "\n\nSEED IDEAS:\n" + (seeds or "").strip() +
                "\n\nOPERATOR FEEDBACK:\n" + (feedback or "").strip())
        d = {"id": INTERRUPT_DIRECTIVE_ID, "type": "SCIENCE",
             "what": what[:4000], "priority": 10000,   # above everything
             "idea_class": "INCREMENTAL", "acceptance":
             "The agent's next actions visibly serve the updated purpose."}
        stored, _ = directives.upsert(d)
        return stored
    except Exception:                                      # noqa: BLE001
        return None


def kill_killable_runs() -> dict:
    """Stop scoped OS workloads even if Run tracking is empty or unavailable.

    Queue launchers are stopped before workers. Metadata only changes after
    verification; failed stops remain running and are returned to the caller.
    """
    from .db import SessionLocal
    from .models import Run
    from . import tmux_safe, research_control, research_processes, orchestrator
    errors: list[str] = []
    cfg = {}
    try:
        cfg = _onboarding()
        research_control.remember_workspace(cfg)
    except Exception as exc:
        errors.append(f"Cannot read project settings: {_safe_error(exc)}")
    try:
        root = research_control.workspace(cfg)
    except ValueError as exc:
        root = None
        errors.append(str(exc))
    runs = []
    try:
        db = SessionLocal()
        try:
            runs = [(r.id, r.tmux_session or r.id, r.status)
                    for r in db.query(Run).filter(Run.status.in_(["running", "queued"])).all()]
        finally:
            db.close()
    except Exception as exc:
        errors.append(f"Cannot read tracked runs: {_safe_error(exc)}")
    registered = {session for _, session, status in runs if status == "running"}
    if registered and root is None:
        errors.append("Cannot verify run ownership: project workspace is not configured")
    # Cancel the in-process scheduler too; otherwise it could restart a
    # discarded queued task as soon as pause is lifted.
    try:
        orchestrator.stop()
    except Exception as exc:
        errors.append(f"Cannot stop research scheduler: {_safe_error(exc)}")
    stats = research_processes.stop_project(root, registered)
    errors.extend(stats["errors"])
    discarded = 0
    try:
        db = SessionLocal()
        try:
            for rid, session, status in runs:
                row = db.query(Run).filter(Run.id == rid).first()
                if not row:
                    continue
                if status == "queued":
                    row.status = "discarded"
                    discarded += 1
                elif session in stats["killed"] or (
                        root is not None and not tmux_safe.is_protected(session)
                        and not tmux_safe.is_alive(session) and not stats["remaining"]
                        and not stats["errors"]):
                    row.status = "crashed"
                    row.ended_at = _iso()
                else:
                    if session not in stats["remaining"]:
                        stats["remaining"].append(session)
            db.commit()
        finally:
            db.close()
    except Exception as exc:
        discarded = 0  # no committed metadata update is counted as success
        errors.append(f"Processes stopped, but run metadata could not be updated: {_safe_error(exc)}")
    stats.update(discarded=discarded, errors=list(dict.fromkeys(errors)), warnings=[])
    stats["ok"] = not stats["errors"] and not stats["remaining"]
    _publish("runs", "runs_changed", {})
    return stats


def _publish(topic: str, event: str, payload: dict | None = None) -> None:
    try:
        from .bus import bus
        bus.publish(topic, event, payload or {})
    except Exception:                                      # noqa: BLE001
        pass


def hard_interrupt(feedback: str = "") -> dict:
    """Pause, stop project queues/workers, and report verified outcomes."""
    from . import notify, research_control
    gate_errors = []
    try:
        research_control.write(paused=True, stop_all=True)
    except Exception as exc:
        gate_errors.append(f"Cannot persist interrupt gate: {_safe_error(exc)}")
    paused = notify.set_research_paused(True)
    stats = kill_killable_runs()
    cfg = {}
    try:
        cfg = _onboarding()
    except Exception as exc:
        gate_errors.append(f"Cannot read research focus: {_safe_error(exc)}")
    if feedback:
        lessons_append(feedback)
    set_interrupt_focus_directive(cfg.get("purpose", ""),
                                  cfg.get("seed_ideas", ""), feedback)
    stats["errors"].extend(gate_errors + paused.get("errors", []))
    stats["ok"] = stats["ok"] and not stats["errors"]
    stats.update(paused=paused.get("paused", notify.research_paused()),
                 sessions_interrupted=paused.get("sessions_interrupted", []),
                 purpose=cfg.get("purpose", ""), seed_ideas=cfg.get("seed_ideas", ""))
    _publish("events", "research_interrupted", stats)
    return stats


def restart_with_feedback(purpose: str, seed_ideas: str,
                          feedback: str = "") -> dict:
    """Re-scope against the updated purpose + seeds. Persists the new purpose/
    seeds, appends the feedback to lessons.md, refreshes the focus directive,
    kills the main agent so it re-spawns clean under the new scope, and
    re-enters the scoping gate (which re-requires PI + council approval). The
    agent prunes off-purpose code + commits/pushes per the directive."""
    from . import tmux_safe, notify, orchestrator, realrun, metrics
    from .db import engine
    from .models import Base

    # 1) Snapshot the onboarding and fold in the updated purpose + seeds.
    cfg = _onboarding()
    cfg["purpose"] = (purpose or cfg.get("purpose", "")).strip()
    cfg["seed_ideas"] = (seed_ideas or cfg.get("seed_ideas", "")).strip()

    # 2) Stop the world: cancel loops, kill every killable run, kill the main
    #    agent (allowed only on this deliberate restart path).
    for fn in (orchestrator.stop, realrun.stop):
        try:
            fn()
        except Exception:                                  # noqa: BLE001
            pass
    stats = hard_interrupt(feedback)
    if not stats["ok"]:
        return {**stats, "reset": False, "status": "stop_incomplete"}
    tmux_safe.kill_session("agent", allow_protected=True)
    tmux_safe.kill_session("author", allow_protected=True)

    # 3) TRUE RESET - wipe ALL research results (runs, ideas, events, metrics,
    #    lifecycle/phase, bless, halt/pause, conclusion, scope state) so the
    #    dashboard genuinely starts over, then re-insert the UPDATED onboarding
    #    row so the project config (with the new purpose/seeds) survives.
    try:
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        metrics.reset()
    except Exception as e:                                 # noqa: BLE001
        print(f"[interrupt] reset wipe error: {e}", flush=True)
    _save_onboarding(cfg)

    # 4) Clear the stale directive queue, then re-anchor on the operator's
    #    feedback (lessons.md is a workspace file, survives the DB wipe).
    try:
        from . import directives
        directives._write_all([])
    except Exception:                                      # noqa: BLE001
        pass
    lessons_append("RESTART WITH FEEDBACK\n" + (feedback or ""))
    set_interrupt_focus_directive(cfg["purpose"], cfg["seed_ideas"], feedback)

    # 5) Fresh state + re-enter the scoping gate (PI + council must re-approve).
    notify.set_research_halted(False)
    notify.set_research_paused(False)
    from . import scoping
    try:
        scoping.start(cfg)
        status = "scoping"
    except Exception as e:                                 # noqa: BLE001
        status = f"scoping_error: {e}"
    _publish("events", "research_restart_with_feedback",
             {"purpose": cfg["purpose"]})
    _publish("runs", "runs_changed", {})
    return {"ok": True, "status": status, "purpose": cfg["purpose"],
            "reset": True}


def resume_from_interrupt() -> dict:
    """Resume research after an interrupt.

    Refuses if research is hard-halted or an open HALT directive exists.
    Otherwise unpauses, restarts the main agent if its tmux session is gone,
    else sends a checkpoint-aware resume message into the agent session, and
    publishes the resumed state."""
    from . import notify, directives, tmux_safe, monitor
    halted, reason = notify.research_halted()
    if halted:
        return {"ok": False, "blocked": True, "reason": "research_halted",
                "detail": reason or "research is hard-halted"}
    halt_d = directives.open_halt()
    if halt_d:
        return {"ok": False, "blocked": True, "reason": "halt_directive",
                "detail": f"resolve open HALT directive {halt_d.get('id')}"}

    result = notify.set_research_paused(False)
    if result.get("ok") is False or result.get("paused"):
        return {**result, "ok": False, "resumed": False}
    restarted = False
    if not tmux_safe.pane_alive("agent"):
        # The agent session vanished (crash / server move) - relaunch it.
        try:
            from . import realrun
            realrun.start_real(_onboarding())
            restarted = True
        except Exception as exc:                           # noqa: BLE001
            notify.set_research_paused(True)
            return {"ok": False, "resumed": False, "error": f"Agent restart failed: {_safe_error(exc)}"}
    else:
        # Session alive - nudge it to pick up from its checkpoints.
        msg = ("RESUME: the operator lifted the interrupt. Re-read lessons.md "
               "(note the OPERATOR INTERRUPT focus) and directives.jsonl "
               "(d-interrupt-focus). Continue from your latest checkpoints - "
               "do not restart from scratch.")
        try:
            monitor.message_agent(msg)
        except Exception:                                  # noqa: BLE001
            pass
    _publish("events", "research_resumed", {"agent_restarted": restarted})
    return {"ok": True, "resumed": True, "agent_restarted": restarted,
            "warnings": result.get("warnings", [])}
