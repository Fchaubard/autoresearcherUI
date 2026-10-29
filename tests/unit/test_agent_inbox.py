import stat
import subprocess

import pytest


def test_long_message_is_private_exact_and_ignored_even_before_git_init(tmp_path, monkeypatch):
    from backend.app import agent_inbox
    workspace = tmp_path / 'project'
    workspace.mkdir()
    run = subprocess.run
    monkeypatch.setattr(agent_inbox.subprocess, 'run', lambda command, **kwargs:
                        subprocess.CompletedProcess(command, 0, stdout=str(workspace)))
    original = '\n'.join(f'Instruction {i}: keep every detail, including Unicode π.' for i in range(60))
    instruction = agent_inbox.delivery_text(original, 'agent')
    assert len(instruction) < 128 and '\n' not in instruction
    relative = instruction.split()[1]
    message = workspace / relative
    assert message.read_text() == original
    assert stat.S_IMODE(message.stat().st_mode) == 0o600
    assert stat.S_IMODE(message.parent.stat().st_mode) == 0o700
    assert agent_inbox.delivery_text(original, 'agent') == instruction
    assert len(list(message.parent.iterdir())) == 2
    run(['git', 'init', str(workspace)], check=True, capture_output=True)
    assert run(['git', '-C', str(workspace), 'status', '--porcelain', '--untracked-files=all'],
               capture_output=True, text=True).stdout == ''


def test_short_message_keeps_original_text_without_files(monkeypatch):
    from backend.app import agent_inbox
    monkeypatch.setattr(agent_inbox.subprocess, 'run', lambda *a, **kw: pytest.fail('no workspace lookup needed'))
    assert agent_inbox.delivery_text('Inspect the baseline.', 'agent') == 'Inspect the baseline.'


def test_private_inbox_refuses_symlink(tmp_path, monkeypatch):
    from backend.app import agent_inbox
    workspace, outside = tmp_path / 'project', tmp_path / 'outside'
    workspace.mkdir(); outside.mkdir()
    (workspace / '.arui-inbox').symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(agent_inbox.subprocess, 'run', lambda command, **kwargs:
                        subprocess.CompletedProcess(command, 0, stdout=str(workspace)))
    with pytest.raises(OSError):
        agent_inbox.delivery_text('Private operator text ' * 100, 'agent')
    assert list(outside.iterdir()) == []
