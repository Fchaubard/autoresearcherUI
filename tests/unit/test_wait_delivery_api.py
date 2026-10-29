"""A successful tmux key write must not erase an unconsumed event wait."""
import pytest


@pytest.fixture
def waiting_client(arui_env, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.app import agent_wait, api
    from backend.app.db import SessionLocal
    from backend.app.models import Project, Run
    with SessionLocal() as db:
        db.add(Project(id="p", name="research", purpose="Test waiting delivery"))
        db.add(Run(id="r1", project_id="p", run_name="baseline", status="running"))
        db.commit()
    agent_wait.arm("Compare r1 when it completes.")
    monkeypatch.setattr(api, "_tmux_alive", lambda _: True)
    monkeypatch.setattr("time.sleep", lambda _: None)
    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize("success_after", [None, 1, 2])
def test_operator_message_requires_consumed_input(waiting_client, monkeypatch, nudge_terminal, success_after):
    from backend.app import agent_wait, supervisor
    from backend.app.db import SessionLocal
    from backend.app.models import ChatMessage
    terminal = nudge_terminal(success_after=success_after)
    monkeypatch.setattr(supervisor._sp, "run", terminal)
    result = waiting_client.post("/api/agent/send", json={"text": "Inspect the baseline"}).json()
    assert result["ok"] is (success_after is not None)
    with SessionLocal() as db:
        assert db.query(ChatMessage).count() == (1 if success_after is not None else 0)
    if success_after is None:
        assert agent_wait.status()["status"] == "waiting"
        assert terminal.enters == 2 and terminal.clears == 1
    else:
        assert agent_wait.status() == {}
        assert terminal.enters == success_after


@pytest.mark.parametrize("success_after", [None, 1])
def test_terminal_enter_keeps_unconsumed_operator_draft(waiting_client, monkeypatch, nudge_terminal, success_after):
    from backend.app import agent_wait, supervisor
    terminal = nudge_terminal(success_after=success_after)
    terminal.draft = "My instruction\nPreserve this second line"
    monkeypatch.setattr(supervisor._sp, "run", terminal)
    result = waiting_client.post("/api/agent/keys", json={"data": "\r", "session": "agent"}).json()
    assert result["ok"] is (success_after is not None)
    assert terminal.enters == 1 and terminal.clears == 0
    if success_after is None:
        assert agent_wait.status()["status"] == "waiting"
        assert terminal.draft == "My instruction\nPreserve this second line"
    else:
        assert agent_wait.status() == {}


@pytest.mark.parametrize("placeholder", ["", "Ask Codex to do anything", 'Try "run tests"'])
def test_empty_terminal_enter_does_not_cancel_wait(waiting_client, monkeypatch, nudge_terminal, placeholder):
    from backend.app import agent_wait, supervisor
    terminal = nudge_terminal(success_after=None)
    terminal.placeholder = placeholder
    monkeypatch.setattr(supervisor._sp, "run", terminal)
    result = waiting_client.post("/api/agent/keys", json={"data": "\r", "session": "agent"}).json()
    assert result["ok"] is True
    assert agent_wait.status()["status"] == "waiting"
    assert terminal.enters == 1 and terminal.clears == 0


@pytest.mark.parametrize('success_after', [None, 1, 2])
@pytest.mark.parametrize('session,endpoint', [('agent', '/api/agent/send'), ('author', '/api/paper/author/send')])
def test_idle_send_verifies_enter_without_a_registered_wait(waiting_client, monkeypatch, nudge_terminal, success_after, session, endpoint):
    from backend.app import agent_wait, supervisor
    from backend.app.db import SessionLocal
    from backend.app.models import ChatMessage
    agent_wait.cancel()
    terminal = nudge_terminal(success_after=success_after)
    monkeypatch.setattr(supervisor._sp, 'run', terminal)
    original = 'Please inspect the baseline before choosing the next experiment.'
    result = waiting_client.post(endpoint, json={'text': original}).json()
    assert result['ok'] is (success_after is not None)
    if success_after is not None:
        assert result['delivery'] == 'submitted'
        assert result['message']['content'] == original
        assert result['message']['role'] == 'researcher'
        assert result['message']['id'] and result['message']['created_at']
    assert terminal.enters == (success_after or 2)
    assert all(cmd[3] == '=' + session + ':' for cmd in terminal.calls if cmd[:2] == ['tmux', 'send-keys'])
    with SessionLocal() as db:
        assert db.query(ChatMessage).count() == (0 if success_after is None else 1)


def test_author_send_does_not_cancel_research_wait(waiting_client, monkeypatch, nudge_terminal):
    from backend.app import agent_wait, supervisor
    terminal = nudge_terminal(success_after=1)
    monkeypatch.setattr(supervisor._sp, 'run', terminal)
    result = waiting_client.post('/api/paper/author/send', json={'text': 'Revise the introduction.'}).json()
    assert result['ok'] and agent_wait.status()['status'] == 'waiting'


def test_long_send_retains_full_original_chat_and_uses_short_private_input(waiting_client, monkeypatch, nudge_terminal):
    from backend.app import supervisor
    terminal = nudge_terminal(success_after=2)
    monkeypatch.setattr(supervisor._sp, 'run', terminal)
    original = '\n'.join(f'Full instruction {i}: preserve every result and continue the experiment.' for i in range(60))
    result = waiting_client.post('/api/agent/send', json={'text': original}).json()
    assert result['ok'] and result['delivery'] == 'submitted'
    assert result['message']['content'] == original
    literals = [cmd[-1] for cmd in terminal.calls if cmd[:2] == ['tmux', 'send-keys'] and '-l' in cmd]
    assert len(literals) == 1 and len(literals[0]) < 128 and '\n' not in literals[0]
    assert (terminal.workspace / literals[0].split()[1]).read_text() == original
    assert terminal.enters == 2


@pytest.mark.parametrize('success_after', [None, 1, 2])
def test_busy_send_requires_input_consumption_without_interrupting_turn(waiting_client, monkeypatch, nudge_terminal, success_after):
    from backend.app import supervisor
    class BusyTerminal(nudge_terminal):
        def screen(self):
            return (self.history + '› ' + (self.draft or self.placeholder)
                    + '\n  tab to queue message\n')
        def __call__(self, command, **kwargs):
            result = super().__call__(command, **kwargs)
            if command[:2] == ['tmux', 'send-keys'] and command[-1] == 'C-u':
                self.draft = ''
            return result
    terminal = BusyTerminal(success_after=success_after)
    terminal.history = 'Working (3s · esc to interrupt)\n'
    terminal.busy, terminal.placeholder = True, 'Ask Codex to do anything'
    monkeypatch.setattr(supervisor._sp, 'run', terminal)
    result = waiting_client.post('/api/agent/send', json={'text': 'Review this once the current step finishes.'}).json()
    assert result['ok'] is (success_after is not None)
    if success_after is not None:
        assert result['delivery'] == 'queued'
    assert terminal.enters == (success_after or 2)
    assert not terminal.draft and terminal.busy
    assert not any(cmd[-1] in {'Escape', 'C-c'} for cmd in terminal.calls if cmd[:2] == ['tmux', 'send-keys'])


def test_chat_limit_returns_latest_messages_in_ascending_order(waiting_client):
    from backend.app.db import SessionLocal
    from backend.app.models import ChatMessage
    with SessionLocal() as db:
        db.add_all(ChatMessage(id=f'm{i}', role='researcher', content=str(i),
                               created_at=f'2026-10-29T00:00:0{i}+00:00') for i in range(4))
        db.commit()
    assert [row['id'] for row in waiting_client.get('/api/chat?limit=2').json()] == ['m2', 'm3']
    assert len(waiting_client.get('/api/chat').json()) == 4
    assert waiting_client.get('/api/chat?limit=0').status_code == 422
    assert waiting_client.get('/api/chat?limit=501').status_code == 422


def test_busy_failed_send_preserves_cursor_home_draft(waiting_client, monkeypatch, nudge_terminal):
    from backend.app import supervisor
    terminal = nudge_terminal(success_after=None)
    terminal.history = 'Working (3s · esc to interrupt)\n'
    terminal.placeholder = 'Ask Codex to do anything'
    terminal.cursor_x_override = 2
    terminal.screen = lambda: terminal.history + '› ' + (terminal.draft or terminal.placeholder) + '\n tab to queue message\n'
    monkeypatch.setattr(supervisor._sp, 'run', terminal)
    result = waiting_client.post('/api/agent/send', json={'text': 'This draft now has its cursor at Home.'}).json()
    assert result['ok'] is False and terminal.enters == 2
    assert terminal.draft == 'This draft now has its cursor at Home.'
    assert not any(cmd[-1] in {'C-u', 'C-c', 'Escape'} for cmd in terminal.calls if cmd[:2] == ['tmux', 'send-keys'])


@pytest.mark.parametrize('original', ['x' * 150, 'x' * 500, '結果を確認してください。' * 3, '🧪' * 30])
def test_narrow_phone_send_uses_one_line_private_reference(waiting_client, monkeypatch, nudge_terminal, original):
    from backend.app import supervisor
    from subprocess import CompletedProcess
    terminal = nudge_terminal(success_after=1)
    terminal.placeholder = 'Ask Codex to do anything'
    # The native Codex composer remains visible even when a five-row pane
    # hides all output and activity indicators.
    terminal.screen = lambda: '› ' + (terminal.draft or terminal.placeholder) + '\nbypass permissions on\n'
    def run(command, **kwargs):
        if command[-1] == '#{pane_width} #{pane_height}':
            return CompletedProcess(command, 0, stdout=b'51 5')
        return terminal(command, **kwargs)
    monkeypatch.setattr(supervisor._sp, 'run', run)
    result = waiting_client.post('/api/agent/send', json={'text': original}).json()
    assert result['ok'] and result['message']['content'] == original
    literals = [cmd[-1] for cmd in terminal.calls if cmd[:2] == ['tmux', 'send-keys'] and '-l' in cmd]
    assert len(literals) == 1 and len(literals[0]) < 47
    assert (terminal.workspace / literals[0].split()[1]).read_text() == original


@pytest.mark.parametrize('height', [5, 35])
def test_repainted_busy_spinner_with_unsent_draft_is_not_delivery(waiting_client, monkeypatch, nudge_terminal, height):
    from backend.app import agent_wait, supervisor
    from subprocess import CompletedProcess
    class RepaintedBusyTerminal(nudge_terminal):
        def screen(self):
            self.history = 'Working (3s · esc to interrupt)\n' if self.enters else ''
            return self.history + '› ' + (self.draft or self.placeholder) + '\nbypass permissions on\n'
        def __call__(self, command, **kwargs):
            if command[-1] == '#{pane_width} #{pane_height}':
                return CompletedProcess(command, 0, stdout=f'51 {height}'.encode())
            return super().__call__(command, **kwargs)
    terminal = RepaintedBusyTerminal(success_after=None)
    terminal.placeholder = 'Ask Codex to do anything'
    monkeypatch.setattr(supervisor._sp, 'run', terminal)
    result = waiting_client.post('/api/agent/send', json={'text': 'x' * 500}).json()
    assert result['ok'] is False and terminal.enters == 2
    assert terminal.clears == 1 and not terminal.draft
    assert agent_wait.status()['status'] == 'waiting'
    assert not any(cmd[-1] in {'C-c', 'Escape'} for cmd in terminal.calls if cmd[:2] == ['tmux', 'send-keys'])
