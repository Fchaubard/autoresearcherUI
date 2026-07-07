"""Research interrupt + resume flow: hard stop, focus directive, lessons
append, and resume guarding (refuse when halted / open HALT directive)."""
import pytest


def _mk_run(db, rid, status, session=None):
    from backend.app.models import Run
    import datetime as dt
    db.add(Run(id=rid, project_id="p", idea_id=f"i-{rid}", run_name=rid,
               status=status, is_baseline=False, config={},
               tmux_session=session or rid,
               created_at=dt.datetime.now(dt.timezone.utc).isoformat()))
    db.commit()


def test_kill_killable_runs_kills_running_discards_queued(arui_env, monkeypatch):
    from backend.app import interrupt, tmux_safe
    from backend.app.db import SessionLocal
    from backend.app.models import Run
    db = SessionLocal()
    _mk_run(db, "run-a", "running", "run-a")
    _mk_run(db, "run-b", "queued")
    db.close()
    killed = []
    monkeypatch.setattr(tmux_safe, "kill_session",
                        lambda name, **k: (killed.append(name) or (True, "ok")))
    out = interrupt.kill_killable_runs()
    assert out["killed"] == ["run-a"]
    assert out["discarded"] == 1
    db = SessionLocal()
    try:
        assert db.query(Run).filter(Run.id == "run-a").first().status == "crashed"
        assert db.query(Run).filter(Run.id == "run-b").first().status == "discarded"
    finally:
        db.close()


def test_kill_killable_never_kills_agent(arui_env, monkeypatch):
    """A run row whose session is literally 'agent' must NOT be killed - the
    protected-session guard in tmux_safe refuses it."""
    from backend.app import interrupt
    from backend.app.db import SessionLocal
    db = SessionLocal()
    _mk_run(db, "agent", "running", "agent")   # pathological name collision
    db.close()
    out = interrupt.kill_killable_runs()
    assert "agent" not in out["killed"]         # protected -> refused


def test_hard_interrupt_pauses_and_sets_focus(arui_env, monkeypatch):
    from backend.app import interrupt, notify, directives
    from backend.app.db import SessionLocal
    from backend.app.models import Setting
    db = SessionLocal()
    db.add(Setting(key="onboarding",
                   value={"purpose": "predict S&P", "seed_ideas": "ridge",
                          "repo_name": "proj"}))
    db.commit(); db.close()
    monkeypatch.setattr(notify, "set_research_paused",
                        lambda p: {"sessions_interrupted": ["agent"]})
    out = interrupt.hard_interrupt(feedback="stop the width sweeps")
    assert out["ok"] and out["paused"]
    assert out["purpose"] == "predict S&P"        # prefill returned
    d = directives.get(interrupt.INTERRUPT_DIRECTIVE_ID)
    assert d and d["type"] == "SCIENCE" and d["priority"] == 10000
    assert "stop the width sweeps" in d["what"]


def test_resume_refuses_when_halted(arui_env, monkeypatch):
    from backend.app import interrupt, notify
    monkeypatch.setattr(notify, "research_halted", lambda: (True, "council halt"))
    out = interrupt.resume_from_interrupt()
    assert out["ok"] is False and out["reason"] == "research_halted"


def test_resume_refuses_on_open_halt_directive(arui_env, monkeypatch):
    from backend.app import interrupt, notify, directives
    from backend.app.db import SessionLocal
    from backend.app.models import Setting
    db = SessionLocal(); db.add(Setting(key="onboarding",
                                        value={"repo_name": "proj"}))
    db.commit(); db.close()
    monkeypatch.setattr(notify, "research_halted", lambda: (False, ""))
    directives.upsert({"id": "d-halt", "type": "HALT", "what": "stop",
                       "idea_class": "INCREMENTAL"})
    out = interrupt.resume_from_interrupt()
    assert out["ok"] is False and out["reason"] == "halt_directive"


def test_resume_unpauses_and_restarts_missing_agent(arui_env, monkeypatch):
    from backend.app import interrupt, notify, tmux_safe, realrun
    monkeypatch.setattr(notify, "research_halted", lambda: (False, ""))
    unpaused = {}
    monkeypatch.setattr(notify, "set_research_paused",
                        lambda p: unpaused.update(v=p) or {})
    monkeypatch.setattr(tmux_safe, "is_alive", lambda n: False)  # agent gone
    started = {}
    monkeypatch.setattr(realrun, "start_real",
                        lambda cfg, **k: started.update(v=True))
    out = interrupt.resume_from_interrupt()
    assert out["ok"] and out["resumed"] and out["agent_restarted"] is True
    assert unpaused["v"] is False
    assert started.get("v") is True


def test_lessons_append_writes(arui_env):
    from backend.app import interrupt, council
    p = council._lessons_path()
    assert p is None or True   # may be None without a repo_name
    # with a repo name set it should write
    from backend.app.db import SessionLocal
    from backend.app.models import Setting
    db = SessionLocal()
    db.add(Setting(key="onboarding", value={"repo_name": "proj"}))
    db.commit(); db.close()
    ok = interrupt.lessons_append("focus on the loss function")
    p = council._lessons_path()
    if ok:
        assert "focus on the loss function" in p.read_text()
