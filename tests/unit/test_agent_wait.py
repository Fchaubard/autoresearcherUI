import datetime as dt
import importlib

import pytest


def seed(arui_env, status="running"):
    from backend.app.db import SessionLocal
    from backend.app.models import Project, Run
    with SessionLocal() as db:
        db.add(Project(id="p", name="research", purpose="Find a better method"))
        db.add(Run(id="r1", project_id="p", run_name="baseline", status=status,
                   config={"large_private_review": "must never enter compact context"}))
        db.commit()


def set_run(status=None, metric=None):
    from backend.app.db import SessionLocal
    from backend.app.models import Run
    with SessionLocal() as db:
        run = db.get(Run, "r1")
        if status:
            run.status = status
        run.headline_metric = metric
        db.commit()


def check(module, send, **overrides):
    args = dict(alive=True, busy=False, idle=True, blocked=False, send=send)
    args.update(overrides)
    return module.supervise(**args)


def test_wait_survives_reload_and_metric_updates_do_not_wake(arui_env):
    seed(arui_env)
    from backend.app import agent_wait
    result = agent_wait.arm("Baseline running; compare loss when complete.")
    assert result["status"] == "waiting"
    assert "baseline" not in agent_wait.status()
    importlib.reload(agent_wait)
    sent = []
    set_run(metric=.4)
    assert check(agent_wait, lambda message: sent.append(message) or True)
    assert sent == []


def test_run_completion_wakes_once_without_changing_training(arui_env):
    seed(arui_env)
    from backend.app import agent_wait
    agent_wait.arm("Next: inspect r1 and launch the next seed.")
    set_run("kept_novel", .2)
    sent = []
    assert check(agent_wait, lambda message: sent.append(message) or True)
    assert len(sent) == 1 and "r1: running -> kept_novel" in sent[0]
    assert not check(agent_wait, lambda _: pytest.fail("duplicate wake"))
    assert agent_wait.context()["recent_finished"][0]["status"] == "kept_novel"


@pytest.mark.parametrize("overrides", [{"busy": True}, {"idle": False}, {"blocked": True}, {"alive": False}])
def test_never_interrupt_active_draft_paused_or_dead_agent(arui_env, overrides):
    seed(arui_env)
    from backend.app import agent_wait
    agent_wait.arm("Waiting for r1.")
    set_run("crashed")
    assert check(agent_wait, lambda _: pytest.fail("must not interrupt"), **overrides)
    assert agent_wait.status()["status"] == "waiting"


def test_failed_wake_is_retained_and_rate_limited(arui_env):
    seed(arui_env)
    from backend.app import agent_wait
    agent_wait.arm("Waiting for result.")
    set_run("crashed")
    now = dt.datetime.now(dt.timezone.utc)
    assert check(agent_wait, lambda _: False, now=now)
    assert check(agent_wait, lambda _: pytest.fail("retry storm"), now=now + dt.timedelta(seconds=30))
    sent = []
    assert check(agent_wait, lambda message: sent.append(message) or True, now=now + dt.timedelta(seconds=61))
    assert len(sent) == 1


def test_safety_reassessment_is_bounded_and_no_work_cannot_park(arui_env):
    seed(arui_env, status="crashed")
    from backend.app import agent_wait
    with pytest.raises(ValueError, match="No running experiments"):
        agent_wait.arm("Do nothing")
    set_run("running")
    agent_wait.arm("Await result", seconds=1)
    state = agent_wait.status()
    armed = dt.datetime.fromisoformat(state["armed_at"])
    deadline = dt.datetime.fromisoformat(state["deadline"])
    assert (deadline - armed).total_seconds() == 300
    sent = []
    assert check(agent_wait, lambda message: sent.append(message) or True, now=deadline)
    assert "Safety reassessment" in sent[0]


def test_pending_review_wakes_after_verdict_and_operator_can_cancel(arui_env):
    seed(arui_env, status="crashed")
    from backend.app import agent_wait
    from backend.app.db import SessionLocal
    from backend.app.models import Setting
    with SessionLocal() as db:
        db.add(Setting(key="code_bless", value={"status": "pending"}))
        db.commit()
    agent_wait.arm("Await code review")
    with SessionLocal() as db:
        db.get(Setting, "code_bless").value = {"status": "approved"}
        db.commit()
    sent = []
    check(agent_wait, lambda message: sent.append(message) or True)
    assert "code review: approved" in sent[0]
    set_run("running")
    agent_wait.arm("Wait again")
    agent_wait.cancel()
    assert agent_wait.status() == {}
    assert agent_wait.context()["last_handoff"]["summary"] == "Wait again"


def test_context_is_bounded_and_excludes_recursive_review_payloads(arui_env):
    seed(arui_env)
    from backend.app import agent_wait
    from backend.app.db import SessionLocal
    from backend.app.models import Run
    with SessionLocal() as db:
        for i in range(50):
            db.add(Run(id=f"done{i}", project_id="p", run_name="finished", status="kept_novel"))
        db.commit()
    result = agent_wait.context()
    assert len(result["recent_finished"]) == 8
    assert result["active_count"] == 1 and result["total_runs"] == 51
    assert "config" not in result["active_runs"][0]


def test_supervisor_honors_registered_wait_before_idle_nudges(arui_env, monkeypatch):
    seed(arui_env)
    from backend.app import agent_wait, supervisor
    agent_wait.arm("Await baseline")
    monkeypatch.delenv("ARUI_DISABLE_BG", raising=False)
    monkeypatch.setattr(supervisor, "_agent_alive", lambda: True)
    monkeypatch.setattr(supervisor, "_supervise_dead_research_agent", lambda **_: False)
    monkeypatch.setattr(supervisor, "_agent_pane_low", lambda: "idle")
    monkeypatch.setattr(supervisor, "_agent_busy", lambda _: False)
    monkeypatch.setattr(supervisor, "_agent_boot_screen", lambda _: False)
    monkeypatch.setattr(supervisor, "_agent_idle_prompt", lambda _: True)
    monkeypatch.setattr(supervisor, "_agent_has_draft", lambda _: False)
    monkeypatch.setattr(supervisor, "_send_agent_nudge", lambda *_: pytest.fail("unnecessary model wake"))
    monkeypatch.setattr(supervisor, "_should_nudge_idle_agent", lambda *_, **__: pytest.fail("generic idle path must be skipped"))
    supervisor._supervise_research_agent()


def test_http_wait_is_authenticated_and_invalid_requests_do_not_park(arui_env):
    seed(arui_env)
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.app import api, auth, agent_wait
    from backend.app.db import SessionLocal
    from backend.app.models import Setting
    with SessionLocal() as db:
        db.add(Setting(key="onboarding", value={"passcode": "test-only"}))
        db.commit()
    app = FastAPI()
    app.include_router(api.router)
    app.middleware("http")(auth.passcode_middleware)
    with TestClient(app) as client:
        assert client.post("/api/agent/wait", json={"summary": "Wait"}).status_code == 401
        headers = {"Authorization": "Bearer test-only"}
        assert client.post("/api/agent/wait", headers=headers, json={"summary": []}).status_code == 409
        assert agent_wait.status() == {}
        response = client.post("/api/agent/wait", headers=headers, json={"summary": "Wait for baseline"})
        assert response.status_code == 200 and response.json()["status"] == "waiting"
        context = client.get("/api/agent/context", headers=headers).json()
        assert context["active_runs"][0]["id"] == "r1"
        assert "baseline" not in client.get("/api/agent/wait", headers=headers).json()


def test_pi_delivery_cancels_wait_only_after_success(arui_env, monkeypatch):
    seed(arui_env)
    from backend.app import agent_wait, pi
    from subprocess import CompletedProcess
    agent_wait.arm("Await result")
    monkeypatch.setattr(pi.subprocess, "run", lambda args, **kwargs: CompletedProcess(args, 1))
    assert not pi._send_to_agent("A concrete intervention")
    assert agent_wait.status()["status"] == "waiting"
    commands = []
    def succeeds(args, **kwargs):
        commands.append(args)
        return CompletedProcess(args, 0)
    monkeypatch.setattr(pi.subprocess, "run", succeeds)
    assert pi._send_to_agent("A concrete intervention")
    assert agent_wait.status() == {}
    assert commands[0][3:5] == ["=agent:", "-l"]


@pytest.mark.parametrize("failure", ["literal", "enter", "timeout", None])
def test_operator_message_preserves_wait_and_chat_on_failed_delivery(
        arui_env, monkeypatch, failure):
    from subprocess import CompletedProcess, TimeoutExpired
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    seed(arui_env)
    from backend.app import agent_wait, api
    from backend.app.db import SessionLocal
    from backend.app.models import ChatMessage
    agent_wait.arm("Await baseline.")
    monkeypatch.setattr(api, "_tmux_alive", lambda _: True)
    commands = []

    def send(cmd, **kwargs):
        commands.append(cmd)
        if failure == "timeout":
            raise TimeoutExpired(cmd, 4)
        failed = ((failure == "literal" and "-l" in cmd)
                  or (failure == "enter" and cmd[-1] == "Enter"))
        return CompletedProcess(cmd, 1 if failed else 0)

    monkeypatch.setattr(api.subprocess, "run", send)
    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app) as client:
        result = client.post("/api/agent/send", json={"text": "Inspect r1"}).json()
    assert result["ok"] is (failure is None)
    assert all(cmd[3] == "=agent:" for cmd in commands)
    with SessionLocal() as db:
        assert db.query(ChatMessage).count() == (1 if failure is None else 0)
    if failure is None:
        assert agent_wait.status() == {}
    else:
        assert agent_wait.status()["status"] == "waiting"


def test_unsent_event_draft_keeps_wait_registered(arui_env, monkeypatch, nudge_terminal):
    seed(arui_env)
    from backend.app import agent_wait, supervisor
    agent_wait.arm("Await baseline.")
    set_run("kept_novel")
    terminal = nudge_terminal(success_after=None)
    monkeypatch.setattr(supervisor._sp, "run", terminal)
    monkeypatch.setattr("time.sleep", lambda _: None)
    now = dt.datetime.now(dt.timezone.utc)
    assert check(agent_wait, supervisor._send_agent_nudge, now=now)
    assert agent_wait.status()["status"] == "waiting"
    assert terminal.enters == 2 and terminal.clears == 1
    assert not supervisor._agent_has_draft(terminal.screen().lower())
    terminal.success_after = 3
    assert check(agent_wait, supervisor._send_agent_nudge, now=now + dt.timedelta(seconds=30))
    assert terminal.enters == 2  # cooldown still applies after clearing the draft
    assert check(agent_wait, supervisor._send_agent_nudge, now=now + dt.timedelta(seconds=61))
    assert terminal.enters == 3 and agent_wait.status() == {}


def test_registered_wait_does_not_suppress_frozen_turn_recovery(arui_env, monkeypatch):
    seed(arui_env)
    from backend.app import agent_wait, supervisor
    agent_wait.arm("Await baseline.")
    monkeypatch.delenv("ARUI_DISABLE_BG", raising=False)
    monkeypatch.setattr(supervisor, "_agent_alive", lambda: True)
    monkeypatch.setattr(supervisor, "_supervise_dead_research_agent", lambda **_: False)
    pane = "unchanged output\ntab to queue message          35% context left"
    monkeypatch.setattr(supervisor, "_agent_pane_low", lambda: pane)
    supervisor._agent_idle_save({
        "pending_fingerprint": supervisor._hashlib.sha256(pane.encode()).hexdigest(),
        "pending_since": (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat(),
    })
    sent = []
    monkeypatch.setattr(supervisor, "_send_agent_nudge", lambda: sent.append("recovery") or True)
    monkeypatch.setattr(supervisor._sp, "run", lambda *_, **__: None)
    monkeypatch.setattr(supervisor, "_should_nudge_idle_agent", lambda *_, **__: pytest.fail("idle nudge"))
    supervisor._supervise_research_agent()
    assert sent == ["recovery"]


@pytest.mark.parametrize('fails', [False, True])
def test_terminal_enter_clears_wait_only_after_delivery(arui_env, monkeypatch, fails):
    from subprocess import CompletedProcess
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    seed(arui_env)
    from backend.app import agent_wait, api
    agent_wait.arm('Await baseline')
    monkeypatch.setattr(api, '_tmux_alive', lambda _: True)
    commands = []
    def send(command, **kwargs):
        commands.append(command)
        return CompletedProcess(command, int(fails))
    monkeypatch.setattr(api.subprocess, 'run', send)
    app = FastAPI(); app.include_router(api.router)
    with TestClient(app) as client:
        result = client.post('/api/agent/keys', json={'session':'agent', 'data':'\r'}).json()
    assert result['ok'] is (not fails)
    assert commands[0][3:] == ['=agent:', 'Enter']
    assert bool(agent_wait.status()) is fails


@pytest.mark.parametrize('sender', ['api', 'pi'])
def test_operator_and_pi_delivery_wait_for_supervisor_transaction(arui_env, monkeypatch, sender):
    import threading
    from subprocess import CompletedProcess
    seed(arui_env)
    from backend.app import agent_wait, api, pi
    agent_wait.arm('Await baseline')
    started, attempted, completed = threading.Event(), threading.Event(), threading.Event()
    def send(command, **kwargs):
        attempted.set()
        return CompletedProcess(command, 0)
    monkeypatch.setattr(api.subprocess, 'run', send)
    result = []
    def deliver():
        started.set()
        result.append(api._deliver_agent_message('A decision') if sender == 'api' else pi._send_to_agent('A decision'))
        completed.set()
    with agent_wait.delivery_lock():
        thread = threading.Thread(target=deliver)
        thread.start()
        assert started.wait(1)
        assert not attempted.wait(.1)
        assert agent_wait.status()['status'] == 'waiting'
    assert completed.wait(3)
    thread.join(timeout=3)
    assert result == ([{'ok': True}] if sender == 'api' else [True])
    assert agent_wait.status() == {}
