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
    """Kill every RUNNING run's tmux session (never a protected core session)
    and mark QUEUED work discarded. Returns {killed:[...], discarded:int}."""
    from .db import SessionLocal
    from .models import Run
    from . import tmux_safe
    killed, discarded = [], 0
    db = SessionLocal()
    try:
        for r in db.query(Run).filter(Run.status == "running").all():
            sess = r.tmux_session or r.id
            ok, _msg = tmux_safe.kill_session(sess)   # refuses agent/author/…
            if ok:
                killed.append(sess)
            r.status = "crashed"
            r.ended_at = _iso()
        for r in db.query(Run).filter(Run.status == "queued").all():
            r.status = "discarded"
            discarded += 1
        db.commit()
    finally:
        db.close()
    return {"killed": killed, "discarded": discarded}


def _publish(topic: str, event: str, payload: dict | None = None) -> None:
    try:
        from .bus import bus
        bus.publish(topic, event, payload or {})
    except Exception:                                      # noqa: BLE001
        pass


def hard_interrupt(feedback: str = "") -> dict:
    """The YES action: stop the world. Pause the loop, kill killable runs,
    discard queued work, append the feedback to lessons.md, and raise the
    focus directive. The main agent session is preserved (paused, not killed)
    so the resume path can pick it back up."""
    from . import notify
    paused = notify.set_research_paused(True)
    stats = kill_killable_runs()
    cfg = _onboarding()
    if feedback:
        lessons_append(feedback)
    set_interrupt_focus_directive(cfg.get("purpose", ""),
                                  cfg.get("seed_ideas", ""), feedback)
    _publish("events", "research_interrupted",
             {"killed": stats["killed"], "discarded": stats["discarded"]})
    return {"ok": True, "paused": True,
            "sessions_interrupted": paused.get("sessions_interrupted", []),
            "killed": stats["killed"], "discarded": stats["discarded"],
            "purpose": cfg.get("purpose", ""),
            "seed_ideas": cfg.get("seed_ideas", "")}


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
    kill_killable_runs()
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

    notify.set_research_paused(False)
    restarted = False
    if not tmux_safe.is_alive("agent"):
        # The agent session vanished (crash / server move) - relaunch it.
        try:
            from . import realrun
            realrun.start_real(_onboarding())
            restarted = True
        except Exception:                                  # noqa: BLE001
            restarted = False
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
    return {"ok": True, "resumed": True, "agent_restarted": restarted}
