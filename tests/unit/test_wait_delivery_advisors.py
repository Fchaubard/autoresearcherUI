"""PI/watchdog wakes verify acceptance and never overwrite operator input."""
from subprocess import CompletedProcess

import pytest


@pytest.mark.parametrize("sender", ["pi", "watchdog"])
@pytest.mark.parametrize("outcome", ["accepted", "second_enter", "lost_enter", "operator_draft", "edited_draft", "capture_failed"])
def test_advisor_wait_wake_requires_verified_acceptance(
        arui_env, make_project, make_run, monkeypatch, sender, outcome):
    from backend.app import agent_wait, pi, supervisor
    from backend.app.watchdog import runner
    make_project()
    make_run(status="running")
    agent_wait.arm("Await experiment completion.")
    message = "[WATCHDOG] Inspect this run." if sender == "watchdog" else "Inspect this run before the next batch."
    draft = "Keep this operator draft intact" if outcome == "operator_draft" else ""
    busy = False
    enters = 0
    commands = []

    def terminal(cmd, **kwargs):
        nonlocal draft, busy, enters
        commands.append(cmd)
        if cmd[:2] == ["tmux", "capture-pane"]:
            if outcome == "capture_failed":
                return CompletedProcess(cmd, 1, stdout=b"")
            if outcome == "edited_draft" and enters:
                draft = "Keep this operator draft intact"
            pane = ("Working (2s · esc to interrupt)\n" if busy
                    else "› " + draft + "\nbypass permissions on\n")
            return CompletedProcess(cmd, 0, stdout=pane.encode())
        if cmd[:2] == ["tmux", "display-message"]:
            return CompletedProcess(cmd, 0, stdout=b"0\n")
        assert cmd[:4] == ["tmux", "send-keys", "-t", "=agent:"]
        if "-l" in cmd:
            draft = cmd[-1]
        elif cmd[-1] == "Enter":
            enters += 1
            if outcome == "accepted" or (outcome == "second_enter" and enters == 2):
                draft, busy = "", True
        elif cmd[-1] == "C-c":
            assert draft == message, "An unrelated operator draft must never be cleared"
            draft = ""
        return CompletedProcess(cmd, 0, stdout=b"")

    monkeypatch.setattr(supervisor._sp, "run", terminal)
    monkeypatch.setattr("time.sleep", lambda _: None)
    send = pi._send_to_agent if sender == "pi" else runner._page_agent
    accepted = outcome in {"accepted", "second_enter"}
    assert send(message) is accepted
    if accepted:
        assert agent_wait.status() == {}
        assert enters == (2 if outcome == "second_enter" else 1)
    else:
        assert agent_wait.status()["status"] == "waiting"
        if outcome in {"operator_draft", "capture_failed"}:
            assert not any(cmd[:2] == ["tmux", "send-keys"] for cmd in commands)
        if outcome in {"operator_draft", "edited_draft"}:
            assert draft == "Keep this operator draft intact"
        if outcome == "lost_enter":
            assert enters == 2 and draft == ""


def test_paper_pi_send_does_not_cancel_research_wait(arui_env, make_project, make_run, monkeypatch):
    from backend.app import agent_wait, pi
    make_project()
    make_run(status="running")
    agent_wait.arm("Await research experiment.")
    commands = []

    def terminal(cmd, **kwargs):
        commands.append(cmd)
        return CompletedProcess(cmd, 0)

    monkeypatch.setattr(pi.subprocess, "run", terminal)
    assert pi._send_to_agent("Paper intervention", session="author")
    assert agent_wait.status()["status"] == "waiting"
    assert all(cmd[3] == "=author:" for cmd in commands)
