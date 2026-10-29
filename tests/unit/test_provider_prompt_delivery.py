"""Native OpenCode mini / Gemini screen-reader input framing and wakeups."""
from subprocess import CompletedProcess

import pytest


PLACEHOLDERS = {
    'opencode': 'Ask anything, / for commands, @ for context…',
    'gemini': 'Type your message or @path/to/file',
}


@pytest.fixture
def provider_terminal(nudge_terminal):
    class ProviderTerminal(nudge_terminal):
        def __init__(self, provider, **kwargs):
            super().__init__(**kwargs)
            self.provider = provider
            self.placeholder = PLACEHOLDERS[provider]

        def screen(self, styled=False):
            if self.busy:
                return 'Working (3s · esc to interrupt)\n'
            if self.provider == 'opencode':
                lines = (self.draft or self.placeholder).split('\n')
                color = 255 if self.draft else 8
                lines = [f'\x1b[38;5;168m┃\x1b[38;5;15m \x1b[38;5;{color}m{line}\x1b[39m'
                         if styled else '┃ ' + line for line in lines]
                return ('▪ oc mini v2.0.29\n\n' + '\n'.join(lines) +
                        '\n\narui-research · nemotron-3-ultra-free · OpenCode · ctrl+p menu\n')
            if self.draft:
                body = (f'\x1b[38;5;231m{self.draft}\x1b[7m\x1b[39m \x1b[0m'
                        if styled else self.draft + ' ')
            else:
                body = (f'\x1b[7m \x1b[0m\x1b[38;5;248m {self.placeholder}\x1b[39m'
                        if styled else '  ' + self.placeholder)
            return 'workspace\ngemini-3.8-flash\nYOLO Ctrl+Y\nYOLO mode ' + body + '\n'

        def __call__(self, command, **kwargs):
            if command[:2] == ['tmux', 'display-message'] and command[-1] in {
                    '#{pane_width} #{pane_height}', '#{pane_current_path}'}:
                return super().__call__(command, **kwargs)
            if command[:2] == ['tmux', 'capture-pane']:
                self.calls.append(command)
                return CompletedProcess(command, 0, stdout=self.screen(styled='-e' in command).encode())
            if command[:2] == ['tmux', 'display-message']:
                self.calls.append(command)
                lines = self.draft.split('\n')
                row = (2 if self.provider == 'opencode' else 3) + len(lines) - 1
                if self.provider == 'opencode':
                    column = 2 + len(lines[-1])
                else:
                    column = (10 if len(lines) == 1 else 0) + len(lines[-1]) + 1 if self.draft else 46
                if self.cursor_x_override is not None:
                    column = self.cursor_x_override
                if self.cursor_override is not None:
                    row = self.cursor_override
                output = f'{row} {column}' if '#{cursor_x}' in command[-1] else str(row)
                return CompletedProcess(command, 0, stdout=output.encode())
            return super().__call__(command, **kwargs)
    return ProviderTerminal


@pytest.mark.parametrize('provider', ['opencode', 'gemini'])
def test_actual_provider_placeholder_can_wake_without_becoming_a_draft(monkeypatch, provider_terminal, provider):
    from backend.app import supervisor as sv
    terminal = provider_terminal(provider, success_after=1)
    monkeypatch.setattr(sv._sp, 'run', terminal)
    monkeypatch.setattr('time.sleep', lambda _: None)
    assert sv._agent_idle_prompt(terminal.screen().lower())
    assert sv._nudge_prompt(terminal.screen())[0] == PLACEHOLDERS[provider]
    assert not sv._agent_has_draft(terminal.screen().lower())
    assert sv._send_agent_nudge('[RESEARCH EVENT] completed run')
    assert terminal.enters == 1 and terminal.clears == 0


@pytest.mark.parametrize('provider', ['opencode', 'gemini'])
@pytest.mark.parametrize('at_home', [False, True])
def test_provider_typed_placeholder_remains_a_protected_draft(monkeypatch, provider_terminal, provider, at_home):
    from backend.app import supervisor as sv
    terminal = provider_terminal(provider)
    terminal.draft = PLACEHOLDERS[provider]
    if at_home:
        terminal.cursor_x_override = 2 if provider == 'opencode' else 46
    monkeypatch.setattr(sv._sp, 'run', terminal)
    assert sv._agent_has_draft(terminal.screen().lower())
    assert not sv._send_agent_nudge('[RESEARCH EVENT] completed run')
    assert not any(command[:2] == ['tmux', 'send-keys'] for command in terminal.calls)
    assert terminal.draft == PLACEHOLDERS[provider]


@pytest.mark.parametrize('provider', ['opencode', 'gemini'])
def test_provider_multiline_failed_event_is_cleared_and_can_retry(monkeypatch, provider_terminal, provider):
    from backend.app import supervisor as sv
    terminal = provider_terminal(provider, success_after=None)
    monkeypatch.setattr(sv._sp, 'run', terminal)
    monkeypatch.setattr('time.sleep', lambda _: None)
    message = '[RESEARCH EVENT] completed run\n\nReview the result without stopping training.'
    assert not sv._send_agent_nudge(message)
    assert terminal.enters == 2 and terminal.clears == 1 and not terminal.draft
    assert not sv._agent_has_draft(terminal.screen().lower())
    terminal.success_after = 3
    assert sv._send_agent_nudge(message)
    assert terminal.enters == 3


@pytest.mark.parametrize('provider', ['opencode', 'gemini'])
def test_provider_multiline_operator_draft_is_preserved(monkeypatch, provider_terminal, provider):
    from backend.app import supervisor as sv
    terminal = provider_terminal(provider)
    terminal.draft = 'Operator first line\n\nOperator last line'
    monkeypatch.setattr(sv._sp, 'run', terminal)
    assert sv._nudge_prompt(terminal.screen())[0] == 'Operator first line Operator last line'
    assert sv._agent_has_draft(terminal.screen().lower())
    assert not sv._send_agent_nudge('[RESEARCH EVENT] completed run')
    assert not any(command[:2] == ['tmux', 'send-keys'] for command in terminal.calls)
