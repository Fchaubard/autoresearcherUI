"""Pause ownership never authorizes clearing a legacy or edited draft."""
import json
import stat
from subprocess import CompletedProcess

import pytest


@pytest.fixture
def paused_terminal(arui_env, monkeypatch, nudge_terminal):
    from backend.app import notify, supervisor
    terminal = nudge_terminal(success_after=2)
    terminal.pane_identity = '%17 43210'
    def run(command, **kwargs):
        if command[-1] == '#{pane_id} #{pane_pid}':
            return CompletedProcess(command, 0, stdout=terminal.pane_identity.encode())
        return terminal(command, **kwargs)
    monkeypatch.setattr(supervisor._sp, 'run', run)
    monkeypatch.setattr(notify, '_tmux_session_alive', lambda session: session == 'agent')
    monkeypatch.setattr('time.sleep', lambda _: None)
    return terminal


def test_pause_marker_is_private_and_resume_clears_only_verified_owned_input(paused_terminal):
    from backend.app import notify
    terminal = paused_terminal
    assert notify._tmux_interrupt_agent('agent', 'Pause while reviewing the results.')
    path = notify._pause_draft_path('agent')
    record = json.loads(path.read_text())
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert record == {'text': terminal.draft, 'pane': terminal.pane_identity}
    assert terminal.draft.startswith('[ARUI paused ') and len(terminal.draft) < 35
    assert terminal.enters == 0
    terminal.calls.clear()
    assert notify._tmux_resume_agent('agent', 'Resume the research directive.')
    keys = [cmd[-1] for cmd in terminal.calls if cmd[:2] == ['tmux', 'send-keys']]
    assert keys == ['C-u', 'Resume the research directive.', 'Enter', 'Enter']
    assert not terminal.draft and terminal.busy and not path.exists()


@pytest.mark.parametrize('kind', ['legacy', 'edited', 'cursor_home', 'replacement_pane'])
def test_resume_preserves_nonowned_or_edited_pause_draft(paused_terminal, kind):
    from backend.app import notify
    terminal = paused_terminal
    if kind == 'legacy':
        terminal.draft = '@arui: research is now paused — please STOP and wait for a /resume directive.'
    else:
        assert notify._tmux_interrupt_agent('agent', 'Paused')
        if kind == 'edited':
            terminal.draft += ' Operator is still editing.'
        elif kind == 'cursor_home':
            terminal.cursor_x_override = 2
        else:
            terminal.pane_identity = '%18 43211'
    original = terminal.draft
    terminal.calls.clear()
    assert not notify._tmux_resume_agent('agent', 'Resume the research directive.')
    assert terminal.draft == original
    assert not any(cmd[:2] == ['tmux', 'send-keys'] for cmd in terminal.calls)


def test_resume_reports_lost_enter_and_keeps_retry_possible(paused_terminal):
    from backend.app import notify
    terminal = paused_terminal
    terminal.success_after = None
    assert notify._tmux_interrupt_agent('agent', 'Paused')
    assert not notify._tmux_resume_agent('agent', 'Resume the research directive.')
    assert terminal.enters == 2 and not terminal.draft and not terminal.busy
    terminal.success_after = 3
    assert notify._tmux_resume_agent('agent', 'Resume the research directive.')
    assert terminal.enters == 3


@pytest.mark.parametrize('setter', ['set_research_paused', 'set_research_halted'])
def test_resume_control_reports_failure_instead_of_claiming_agent_resumed(paused_terminal, monkeypatch, setter):
    from backend.app import notify, runtime_settings
    terminal = paused_terminal
    terminal.draft = 'Legacy draft must remain untouched.'
    monkeypatch.setattr(runtime_settings, 'apply_pending', lambda: {'warnings': []})
    result = getattr(notify, setter)(False)
    assert result['ok'] is False and result['sessions_interrupted'] == []
    assert any('Resume was not accepted by agent' in error for error in result['errors'])
    assert terminal.draft == 'Legacy draft must remain untouched.'
    assert not any(cmd[:2] == ['tmux', 'send-keys'] for cmd in terminal.calls)
