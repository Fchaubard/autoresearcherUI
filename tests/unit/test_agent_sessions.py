"""Native continuation must select this live conversation, never latest globally."""
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid

import pytest


def transcript(tmp_path, *, model="gpt-6-astra", workspace=None, provider="arui_openai"):
    workspace = workspace or tmp_path / "project"
    workspace.mkdir(parents=True, exist_ok=True)
    home = tmp_path / "private-codex"
    session_id = str(uuid.uuid4())
    rollout = home / "sessions" / f"rollout-test-{session_id}.jsonl"
    rollout.parent.mkdir(parents=True, exist_ok=True)
    rollout.write_text(json.dumps({"type": "session_meta", "payload": {
        "id": session_id, "cwd": str(workspace), "model_provider": provider,
        "source": "cli"}}) + "\n")
    database = home / "state_5.sqlite"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE IF NOT EXISTS threads (id TEXT,rollout_path TEXT,cwd TEXT,model_provider TEXT,model TEXT,archived INTEGER)")
        db.execute("INSERT INTO threads VALUES (?,?,?,?,?,0)",
                   (session_id, str(rollout), str(workspace), provider, model))
    process = {"pid": 123, "birth": "456", "cwd": workspace.resolve(),
               "home": home.resolve(), "files": {rollout.resolve(), database.resolve()}}
    return workspace, home, rollout, database, session_id, process


def test_selects_owned_rollout_not_unrelated_newer_session(tmp_path, monkeypatch):
    from backend.app import agent_sessions
    ws, _, _, _, sid, process = transcript(tmp_path)
    transcript(tmp_path, workspace=tmp_path / "unrelated")
    monkeypatch.setattr(agent_sessions, "_pane_processes", lambda session: [process])
    result = agent_sessions.codex_conversation("agent", ws, "gpt-6-astra")
    assert result["session_id"] == sid
    assert result["workspace"] == str(ws)


@pytest.mark.parametrize("mismatch", ["workspace", "model", "provider", "archived", "rollout"])
def test_rejects_mismatched_thread_identity(tmp_path, monkeypatch, mismatch):
    from backend.app import agent_sessions
    ws, _, rollout, database, _, process = transcript(tmp_path)
    with sqlite3.connect(database) as db:
        column, value = {"workspace": ("cwd", str(tmp_path)),
                         "model": ("model", "gpt-5.6-sol"),
                         "provider": ("model_provider", "custom"),
                         "archived": ("archived", 1),
                         "rollout": ("rollout_path", str(tmp_path / "other.jsonl"))}[mismatch]
        db.execute(f"UPDATE threads SET {column}=?", (value,))
    monkeypatch.setattr(agent_sessions, "_pane_processes", lambda session: [process])
    with pytest.raises(agent_sessions.ConversationUnavailable):
        agent_sessions.codex_conversation("agent", ws, "gpt-6-astra")


def test_changed_pid_identity_is_not_safe_to_restart(tmp_path, monkeypatch):
    from backend.app import agent_sessions
    ws, _, _, _, _, process = transcript(tmp_path)
    monkeypatch.setattr(agent_sessions, "_pane_processes", lambda session: [process])
    original = agent_sessions.codex_conversation("agent", ws, "gpt-6-astra")
    process["birth"] = "a different process reused that PID"
    with pytest.raises(agent_sessions.ConversationUnavailable, match="changed"):
        agent_sessions.revalidate("agent", original)


def test_live_private_tmux_process_owns_conversation(arui_env, tmp_path):
    from backend.app import agent_sessions
    ws, home, rollout, database, sid, _ = transcript(tmp_path)
    ready = tmp_path / "ready"
    code = ("import ctypes,time; from pathlib import Path; "
            "ctypes.CDLL(None).prctl(15,b'codex',0,0,0); "
            f"a=open({str(rollout)!r}); b=open({str(database)!r}); "
            f"Path({str(ready)!r}).touch(); time.sleep(20)")
    subprocess.run(["tmux", "new-session", "-d", "-s", "agent", "-c", str(ws),
                    "-e", f"CODEX_HOME={home}", shlex.join([sys.executable, "-c", code])],
                   check=True, capture_output=True)
    deadline = time.monotonic() + 5
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(.02)
    assert ready.exists()
    result = agent_sessions.codex_conversation("agent", ws, "gpt-6-astra")
    assert result["session_id"] == sid
    assert result["home"] == str(home)
    agent_sessions.revalidate("agent", result)
    assert subprocess.run(["tmux", "has-session", "-t", "=agent"],
                          capture_output=True).returncode == 0


def test_codex_resume_pins_model_effort_provider_workspace_and_uuid(tmp_path, monkeypatch):
    from backend.app import agent_cli
    monkeypatch.setattr(agent_cli, "_binary", lambda provider: "codex")
    sid = str(uuid.uuid4())
    _, command = agent_cli.command("gpt-6-astra", "medium", "Continue retained context",
                                  workspace=tmp_path, resume_session_id=sid)
    argv = shlex.split(command)
    assert argv[:2] == ["codex", "resume"]
    assert argv[argv.index("--cd") + 1] == str(tmp_path)
    assert argv[argv.index("--model") + 1] == "gpt-6-astra"
    assert 'model_provider="arui_openai"' in argv
    assert "model_reasoning_effort=medium" in argv
    assert argv[-2:] == [sid, "Continue retained context"]
    assert "--last" not in argv and "--all" not in argv


def test_codex_effort_cannot_silently_fall_back_when_new_model_rejects_it(monkeypatch):
    from backend.app import agent_cli, runtime_settings
    with pytest.raises(agent_cli.AgentCLIUnavailable, match='Reasoning effort'):
        agent_cli.command('gpt-5.5', 'max')
    before = {'research_agent_model': 'gpt-5.5', 'research_agent_effort': 'high'}
    with pytest.raises(ValueError, match='Reasoning effort'):
        runtime_settings.validate_models(before, {**before, 'research_agent_effort': 'max'})
    monkeypatch.setenv('ARUI_CLAUDE_BIN', 'test-only')
    # The author inherits the research effort, even with its own model.
    before = {'research_agent_model': 'gpt-6-astra', 'research_agent_effort': 'high',
              'author_agent_model': 'gpt-5.5'}
    with pytest.raises(ValueError, match='Reasoning effort'):
        runtime_settings.validate_models(before, {**before, 'research_agent_effort': 'max'})
    # An inherited model also participates in validation using the author's
    # explicit effort, rather than silently using the research role's value.
    before = {'research_agent_model': 'gpt-6-astra', 'research_agent_effort': 'high',
              'author_agent_effort': 'max'}
    with pytest.raises(ValueError, match='Reasoning effort'):
        runtime_settings.validate_models(before, {**before, 'research_agent_model': 'gpt-5.5'})


@pytest.mark.parametrize("sid", ["latest", "../other", "--last"])
def test_resume_never_accepts_a_global_alias_or_path(tmp_path, monkeypatch, sid):
    from backend.app import agent_cli
    with pytest.raises(agent_cli.AgentCLIUnavailable):
        agent_cli.command("gpt-6-astra", workspace=tmp_path, resume_session_id=sid)


def test_effort_change_keeps_running_agent_when_conversation_unknown(
        arui_env, setting_setter, make_project, monkeypatch):
    from backend.app import runtime_settings, tmux_safe, agent_watcher, agent_sessions
    from backend.app.db import SessionLocal
    before = {"research_agent_model": "gpt-6-astra", "research_agent_effort": "xhigh", "author_agent_effort": "high"}
    after = {**before, "research_agent_effort": "medium"}
    setting_setter("onboarding", after)
    make_project()
    with SessionLocal() as db:
        runtime_settings.queue_changes(db, before, after)
        db.commit()
    monkeypatch.setattr(tmux_safe, "pane_alive", lambda role: True)
    from backend.app import research_processes
    monkeypatch.setattr(research_processes, "active_session_workloads", lambda role: [])
    monkeypatch.setattr(agent_sessions, "codex_conversation",
                        lambda *args: (_ for _ in ()).throw(agent_sessions.ConversationUnavailable()))
    monkeypatch.setattr(agent_watcher, "_restart_session",
                        lambda *args, **kwargs: pytest.fail("must preserve current agent"))
    result = runtime_settings.apply_pending()
    assert result["applied"] == [] and result["pending"] == ["agent"]
    assert result["pending_reasons"] == {"agent": "conversation_unavailable"}


@pytest.mark.parametrize("role,prefix", [("agent", "research_agent"), ("author", "author_agent")])
@pytest.mark.parametrize("target", ["gpt-6-astra", "gpt-5.6-sol"])
def test_effort_change_forwards_verified_conversation_and_reports_native(
        arui_env, setting_setter, make_project, monkeypatch, role, prefix, target, tmp_path):
    from backend.app import runtime_settings, tmux_safe, agent_watcher, agent_sessions, research_processes
    from backend.app.db import SessionLocal
    before = {"author_agent_model": "claude-opus-5", "author_agent_effort": "high", prefix + "_model": "gpt-6-astra", prefix + "_effort": "xhigh"}
    after = {**before, prefix + "_effort": "medium", prefix + "_model": target}
    setting_setter("onboarding", after)
    make_project()
    if role == "author":
        from backend.app import paper
        monkeypatch.setattr(paper, "paper_folder", lambda: tmp_path)
    with SessionLocal() as db:
        runtime_settings.queue_changes(db, before, after)
        db.commit()
    monkeypatch.setattr(tmux_safe, "pane_alive", lambda role: True)
    monkeypatch.setattr(research_processes, "active_session_workloads", lambda role: [])
    conversation = {"session_id": str(uuid.uuid4()), "model": "gpt-6-astra"}
    def discover(selected_role, workspace, source):
        assert selected_role == role and source == "gpt-6-astra"
        return conversation
    monkeypatch.setattr(agent_sessions, "codex_conversation", discover)
    calls = []
    monkeypatch.setattr(agent_watcher, "_restart_session",
                        lambda *args, **kwargs: calls.append((args, kwargs)) or True)
    result = runtime_settings.apply_pending()
    assert result["context_preservation"] == {role: "native_conversation"}
    assert calls == [((role,), {"resume": True, "continuation": {**conversation, "target_model": target}})]
    assert runtime_settings._get_pending() == []


@pytest.mark.parametrize("source,mode", [("gpt-5.6-sol", "native_conversation"),
                                        ("gemini-4-argon", "workspace_handoff")])
def test_pending_model_change_then_effort_change_preserves_original_source(arui_env, source, mode):
    from backend.app import runtime_settings
    from backend.app.db import SessionLocal
    from backend.app.models import Setting
    before = {"research_agent_model": source, "research_agent_effort": "high"}
    switched = {**before, "research_agent_model": "gpt-6-astra"}
    final = {**switched, "research_agent_effort": "medium"}
    with SessionLocal() as db:
        runtime_settings.queue_changes(db, before, switched)
        db.commit()
        runtime_settings.queue_changes(db, switched, final)
        db.commit()
        item = db.query(Setting).filter_by(key="settings.pending_agents").one().value
    assert item["continuations"]["agent"]["mode"] == mode
    assert item["continuations"]["agent"]["source_model"] == source
    assert item["continuations"]["agent"]["target_model"] == "gpt-6-astra"


def test_restart_never_kills_when_verified_conversation_changes(
        arui_env, setting_setter, fake_subprocess, monkeypatch):
    from backend.app import agent_watcher, agent_sessions, research_processes
    setting_setter("onboarding", {"research_agent_model": "gpt-6-astra"})
    monkeypatch.setattr(research_processes, "active_session_workloads", lambda session: [])
    monkeypatch.setattr(agent_sessions, "revalidate", lambda *args:
                        (_ for _ in ()).throw(agent_sessions.ConversationUnavailable()))
    assert not agent_watcher._restart_session("agent", resume=True,
                                             continuation={"model": "gpt-6-astra"})
    assert not any(isinstance(call["args"], (list, tuple)) and
                   call["args"][:2] == ["tmux", "kill-session"] for call in fake_subprocess)


def test_native_research_launch_keeps_brief_and_notifies_transient_state_reset(
        arui_env, tmp_path, fake_subprocess, monkeypatch):
    from backend.app import agent, agent_cli, pane_stream
    monkeypatch.setattr(agent_cli, "_binary", lambda provider: "codex")
    monkeypatch.setattr(agent_cli, "accept_codex_project_trust", lambda *args: True)
    monkeypatch.setattr(pane_stream, "enable", lambda *args, **kw: None)
    monkeypatch.setattr(pane_stream, "apply_remembered_size", lambda *args: None)
    prompt = tmp_path / "_setup_prompt.txt"
    prompt.write_text("preserve the original research instructions")
    sid = str(uuid.uuid4())
    research = agent.RealAgent(str(tmp_path), "test", "http://local", str(tmp_path),
        model="gpt-6-astra", effort="medium", setup_prompt="do not overwrite",
        continuation={"session_id": sid, "home": str(tmp_path / "codex")})
    research.start()
    launches = [call["args"] for call in fake_subprocess
                if isinstance(call["args"], (list, tuple)) and call["args"][:2] == ["tmux", "respawn-pane"]]
    assert len(launches) == 1
    assert sid in launches[0][-1] and "codex resume" in launches[0][-1]
    assert "functions.store/load" in launches[0][-1]
    assert f"CODEX_HOME={tmp_path / 'codex'}" in launches[0]
    assert prompt.read_text() == "preserve the original research instructions"


def test_unrelated_provider_settings_do_not_restart_astra(arui_env):
    from backend.app import runtime_settings
    from backend.app.db import SessionLocal
    from backend.app.models import Setting
    before = {"research_agent_model": "gpt-6-astra", "author_agent_model": "gpt-6-astra",
              "research_agent_effort": "xhigh", "author_agent_effort": "high"}
    flint = {**before, "flint_mode": "managed", "flint_device": "cpu",
             "flint_timeout_seconds": 240, "flint_model": "new-local-model",
             "flint_base_url": "http://127.0.0.1:8077/v1", "flint_token": "local-key"}
    assert runtime_settings.changed_roles(before, flint) == []
    after = {**flint, "research_agent_effort": "medium"}
    with SessionLocal() as db:
        runtime_settings.queue_changes(db, before, after)
        db.commit()
        value = db.get(Setting, "settings.pending_agents").value
    assert value["roles"] == ["agent"]
    assert value["continuations"]["agent"]["mode"] == "native_conversation"
    assert value["continuations"]["agent"]["changed_fields"] == ["research_agent_effort"]


def test_selected_flint_connection_changes_require_handoff(arui_env):
    from backend.app import runtime_settings
    from backend.app.db import SessionLocal
    from backend.app.models import Setting
    before = {"research_agent_model": "flint", "author_agent_model": "gpt-6-astra", "flint_device": "auto"}
    after = {**before, "flint_device": "cpu"}
    assert runtime_settings.changed_roles(before, after) == ["agent"]
    with SessionLocal() as db:
        runtime_settings.queue_changes(db, before, after)
        db.commit()
        assert db.get(Setting, "settings.pending_agents").value["continuations"]["agent"]["mode"] == "workspace_handoff"


def test_author_inherited_openai_model_and_effort_changes_use_native_context(arui_env):
    from backend.app import runtime_settings
    from backend.app.db import SessionLocal
    from backend.app.models import Setting
    before = {'research_agent_model': 'gpt-5.6-sol', 'research_agent_effort': 'high'}
    after = {'research_agent_model': 'gpt-6-astra', 'research_agent_effort': 'medium'}
    assert runtime_settings.changed_roles(before, after) == ['agent', 'author']
    with SessionLocal() as db:
        runtime_settings.queue_changes(db, before, after)
        db.commit()
        value = db.get(Setting, 'settings.pending_agents').value['continuations']['author']
    assert value['mode'] == 'native_conversation'
    assert value['source_model'] == 'gpt-5.6-sol' and value['target_model'] == 'gpt-6-astra'


def test_managed_flint_warmup_keeps_current_agent_until_ready(
        arui_env, setting_setter, monkeypatch):
    from backend.app import runtime_settings, tmux_safe, research_processes, agent_watcher, flint_manager
    from backend.app.db import SessionLocal
    before = {"research_agent_model": "gpt-6-astra", "author_agent_model": "claude-opus-5"}
    after = {**before, "research_agent_model": "flint/local", "flint_mode": "managed"}
    setting_setter("onboarding", after)
    with SessionLocal() as db:
        runtime_settings.queue_changes(db, before, after)
        db.commit()
    monkeypatch.setattr(tmux_safe, "pane_alive", lambda role: True)
    monkeypatch.setattr(research_processes, "active_session_workloads", lambda role: [])
    class Local:
        ready = False
        prepares = 0
        phase = 'loading'
        manually_stopped = False
        def is_ready(self): return self.ready
        def status(self): return {'status': self.phase, 'manually_stopped': self.manually_stopped}
        def prepare(self, device):
            self.prepares += 1
            return self.status()
    local = Local()
    monkeypatch.setattr(flint_manager, 'manager', lambda: local)
    restarts = []
    monkeypatch.setattr(agent_watcher, '_restart_session', lambda *a, **kw: restarts.append((a, kw)) or True)
    assert runtime_settings.apply_pending()['pending_reasons'] == {'agent': 'model_starting'}
    assert local.prepares == 1 and not restarts
    local.phase, local.manually_stopped = 'stopped', True
    assert runtime_settings.apply_pending()['pending_reasons'] == {'agent': 'model_unavailable'}
    assert local.prepares == 1 and not restarts
    local.manually_stopped = False
    local.phase = 'error'
    assert runtime_settings.apply_pending()['pending_reasons'] == {'agent': 'model_unavailable'}
    assert local.prepares == 1 and not restarts
    local.ready = True
    result = runtime_settings.apply_pending()
    assert result['applied'] == ['agent'] and len(restarts) == 1


def test_research_restart_uses_new_openai_selection_with_verified_old_conversation(
        arui_env, setting_setter, fake_subprocess, monkeypatch):
    from backend.app import agent_watcher, agent_sessions, research_processes, realrun
    setting_setter('onboarding', {'research_agent_model': 'gpt-6-astra',
        'research_agent_effort': 'medium', 'openai_token': 'unused-fixture-key'})
    monkeypatch.setattr(research_processes, 'active_session_workloads', lambda role: [])
    verified = []
    monkeypatch.setattr(agent_sessions, 'revalidate', lambda role, value: verified.append(value))
    launched = []
    monkeypatch.setattr(realrun, 'start_real', lambda cfg, **kw: launched.append((cfg, kw)) or object())
    conversation = {'model': 'gpt-5.6-sol', 'target_model': 'gpt-6-astra', 'session_id': str(uuid.uuid4())}
    assert agent_watcher._restart_session('agent', resume=True, continuation=conversation)
    assert verified == [conversation]
    assert launched[0][0]['research_agent_model'] == 'gpt-6-astra'
    assert launched[0][1]['continuation'] == conversation


def test_handoff_saves_visible_context_runs_and_redacts_credentials(
        arui_env, tmp_path, make_project, make_run, setting_setter, monkeypatch):
    from backend.app import agent_sessions, agent_wait
    ws, _, rollout, _, _, process = transcript(tmp_path)
    subprocess.run(['git', 'init', '-q', str(ws)], check=True)
    project = make_project(repo_path=str(ws))
    make_run(id="running-kept-alive", project_id=project.id, status="running")
    setting_setter("onboarding", {"openai_token": "test-secret-value-123"})
    setting_setter(agent_wait.HANDOFF_KEY, {"project_id": project.id,
        "summary": "Wait for running-kept-alive, then compare its held-out loss."})
    (ws / "AGENT_STATE.md").write_text("durable checkpoint")
    with rollout.open('a') as out:
        for role, channel, content in [('user', None, 'Keep the baseline; token=test-secret-value-123'),
                                       ('assistant', 'analysis', 'PRIVATE_REASONING_NOT_FOR_HANDOFF'),
                                       ('assistant', 'final', 'Current status: evaluation is pending.')]:
            out.write(json.dumps({'type': 'response_item', 'payload': {'type': 'message',
                'role': role, 'channel': channel, 'content': [{'type': 'input_text' if role == 'user' else 'output_text',
                                                             'text': content}]}}) + '\n')
    monkeypatch.setattr(agent_sessions, '_pane_processes', lambda role: [process])
    mode = agent_sessions.save_workspace_handoff('agent', {'source_model': 'gpt-6-astra', 'target_model': 'gemini-4-argon'})
    text = (ws / 'ARUI_HANDOFF.md').read_text()
    assert mode == 'workspace_handoff'
    assert 'running-kept-alive' in text and '[AGENT_STATE.md](AGENT_STATE.md)' in text
    assert 'Keep the baseline' in text and 'evaluation is pending' in text
    assert 'test-secret-value-123' not in text and 'PRIVATE_REASONING' not in text
    assert '[REDACTED]' in text and 'not restored' in text
    assert (ws / 'ARUI_HANDOFF.md').stat().st_mode & 0o777 == 0o600
    assert subprocess.run(['git', '-C', str(ws), 'check-ignore', '-q', 'ARUI_HANDOFF.md']).returncode == 0


def test_missing_conversation_is_honestly_durable_state_only(arui_env, tmp_path, make_project):
    from backend.app import agent_sessions
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    make_project(repo_path=str(tmp_path))
    mode = agent_sessions.save_workspace_handoff('agent', {'source_model': 'gemini-4-argon', 'target_model': 'gpt-6-astra'})
    assert mode == 'workspace_state_only'
    assert 'workspace_state_only' in (tmp_path / 'ARUI_HANDOFF.md').read_text()


def test_private_handoff_is_ignored_in_a_linked_worktree(tmp_path):
    from backend.app.agent_sessions import _exclude_private_handoff
    repo, worktree = tmp_path / 'repo', tmp_path / 'worktree'
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                    'commit', '-q', '--allow-empty', '-m', 'fixture'], check=True)
    subprocess.run(['git', '-C', str(repo), 'worktree', 'add', '-q', '-b', 'fixture', str(worktree)], check=True)
    _exclude_private_handoff(worktree)
    (worktree / 'ARUI_HANDOFF.md').touch()
    assert subprocess.run(['git', '-C', str(worktree), 'check-ignore', '-q', 'ARUI_HANDOFF.md']).returncode == 0
    assert not (worktree / '.gitignore').exists()


def test_failed_native_spawn_recovers_exact_conversation_after_old_process_exits(
        arui_env, tmp_path, make_project, setting_setter, monkeypatch):
    from backend.app import agent_sessions, agent_watcher, realrun, research_processes
    ws, home, rollout, database, sid, _ = transcript(tmp_path)
    make_project(repo_path=str(ws))
    setting_setter('onboarding', {'research_agent_model': 'gpt-6-astra',
        'research_agent_effort': 'medium', 'openai_token': 'unused-fixture-key'})
    monkeypatch.setattr(research_processes, 'active_session_workloads', lambda role: [])
    def spawn(number):
        ready = tmp_path / f'ready-{number}'
        code = ("import ctypes,time; from pathlib import Path; "
                "ctypes.CDLL(None).prctl(15,b'codex',0,0,0); "
                f"a=open({str(rollout)!r}); b=open({str(database)!r}); "
                f"Path({str(ready)!r}).touch(); time.sleep(20)")
        subprocess.run(['tmux', 'new-session', '-d', '-s', 'agent', '-c', str(ws),
                        '-e', f'CODEX_HOME={home}', shlex.join([sys.executable, '-c', code])],
                       check=True, capture_output=True)
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert ready.exists()
    spawn(0)
    original = agent_sessions.codex_conversation('agent', ws, 'gpt-6-astra')
    original['target_model'] = 'gpt-6-astra'
    attempts = []
    def start(cfg, *, resume, continuation):
        attempts.append(continuation)
        assert resume and continuation['session_id'] == sid
        assert cfg['research_agent_effort'] == 'medium'
        if len(attempts) == 1:
            raise OSError('simulated replacement startup failure')
        spawn(1)
        return object()
    monkeypatch.setattr(realrun, 'start_real', start)
    assert not agent_watcher._restart_session('agent', resume=True, continuation=original)
    assert agent_sessions.resume_intent('agent')['session_id'] == sid
    assert subprocess.run(['tmux', 'has-session', '-t', '=agent'], capture_output=True).returncode != 0
    # The retry has no in-memory continuation argument; recovery comes from DB.
    assert agent_watcher._restart_session('agent', resume=True)
    restored = agent_sessions.codex_conversation('agent', ws, 'gpt-6-astra')
    assert restored['session_id'] == sid and restored['pid'] != original['pid']
    assert agent_sessions.resume_intent('agent') is None
    assert len(attempts) == 2


def test_invalid_persisted_resume_never_starts_a_fresh_conversation(
        arui_env, tmp_path, make_project, setting_setter, monkeypatch):
    from backend.app import agent_sessions, agent_watcher, realrun, research_processes
    ws, _, rollout, _, _, process = transcript(tmp_path)
    make_project(repo_path=str(ws))
    setting_setter('onboarding', {'research_agent_model': 'gpt-6-astra', 'openai_token': 'unused-key'})
    monkeypatch.setattr(agent_sessions, '_pane_processes', lambda role: [process])
    original = agent_sessions.codex_conversation('agent', ws, 'gpt-6-astra')
    agent_sessions.remember_resume('agent', original)
    rollout.write_text('{"type":"session_meta","payload":{"id":"unrelated"}}\n')
    monkeypatch.setattr(research_processes, 'active_session_workloads', lambda role: [])
    monkeypatch.setattr(realrun, 'start_real', lambda *a, **kw: pytest.fail('must not lose prior context'))
    assert not agent_watcher._restart_session('agent', resume=True)
    assert agent_sessions.resume_intent('agent')['session_id'] == original['session_id']


def test_failed_native_recovery_retries_are_rate_limited(arui_env, monkeypatch):
    from backend.app import agent_sessions, agent_watcher, tmux_safe
    monkeypatch.setattr(agent_sessions, 'resume_intent', lambda session: {'session_id': 'fixture'})
    monkeypatch.setattr(agent_sessions, 'confirm_resume', lambda session: False)
    monkeypatch.setattr(tmux_safe, 'pane_alive', lambda session: False)
    monkeypatch.setattr(agent_watcher, '_last_restart', {})
    monkeypatch.setattr(agent_watcher, '_emit', lambda *a: None)
    calls = []
    monkeypatch.setattr(agent_watcher, '_restart_session', lambda *a, **kw: calls.append((a, kw)) or False)
    agent_watcher._check_resume_recovery('agent')
    agent_watcher._check_resume_recovery('agent')
    assert calls == [(('agent',), {'resume': True, 'only_if_missing': True})]


def test_direct_launch_cannot_bypass_failed_conversation_recovery(arui_env, setting_setter, monkeypatch):
    from backend.app import agent_sessions, realrun, author_agent
    def fail(session):
        raise agent_sessions.ConversationUnavailable('fixture transcript is unavailable')
    monkeypatch.setattr(agent_sessions, 'recovery_conversation', fail)
    with pytest.raises(agent_sessions.ConversationUnavailable):
        realrun.start_real({'research_agent_model': 'gpt-6-astra'}, resume=True)
    setting_setter('onboarding', {'author_agent_model': 'gpt-6-astra'})
    monkeypatch.setattr(author_agent, '_tmux_alive', lambda session: False)
    result = author_agent.start()
    assert result['status'] == 'error' and 'no fresh' in result['detail']


def test_native_author_receives_paper_continuation_without_research_wait(
        arui_env, tmp_path, setting_setter, fake_subprocess, monkeypatch):
    from types import SimpleNamespace
    from backend.app import author_agent, agent_cli, pane_stream
    from backend.app.agent_sessions import AUTHOR_RESUME_NOTICE
    setting_setter('onboarding', {'author_agent_model': 'gpt-6-astra', 'author_agent_effort': 'medium'})
    monkeypatch.setattr(author_agent, '_tmux_alive', lambda role: False)
    monkeypatch.setattr(author_agent, '_meta_block', lambda *a: '')
    monkeypatch.setattr(author_agent, '_record_spawn', lambda: None)
    monkeypatch.setattr(author_agent.paper, 'paper_folder', lambda: tmp_path)
    monkeypatch.setattr(author_agent.paper, 'ensure_paper_repo', lambda *a: None)
    monkeypatch.setattr(author_agent.paper, 'write_projections', lambda: None)
    monkeypatch.setattr(agent_cli, '_binary', lambda role: 'codex')
    monkeypatch.setattr(pane_stream, 'enable', lambda *a, **kw: None)
    monkeypatch.setattr(pane_stream, 'apply_remembered_size', lambda *a: None)
    feeds = []
    monkeypatch.setattr(author_agent, 'threading', SimpleNamespace(
        Thread=lambda **kw: SimpleNamespace(start=lambda: feeds.append(kw))))
    monkeypatch.delenv('ARUI_DISABLE_BG')
    result = author_agent.start(continuation={'session_id': str(uuid.uuid4()), 'home': str(tmp_path / 'private-home')})
    assert result['status'] == 'started'
    brief = feeds[0]['kwargs']['brief']
    assert brief == AUTHOR_RESUME_NOTICE and 'main.tex' in brief
    assert '/api/agent/wait' not in brief and '/api/agent/context' not in brief
    assert 'functions.store/load' in brief


@pytest.mark.skipif(not shutil.which("codex"), reason="optional installed CLI resume integration")
def test_installed_codex_renders_exact_saved_conversation_without_inference(arui_env, tmp_path):
    """A real CLI loads prior messages; unreachable loopback prevents paid calls."""
    from backend.app.agent_cli import command
    home, workspace = tmp_path / "codex", tmp_path / "project"
    workspace.mkdir()
    sid, marker = str(uuid.uuid4()), "SYNTHETIC_RETAINED_CONVERSATION_71923"
    rollout = home / "sessions" / f"rollout-2026-10-29T19-00-00-{sid}.jsonl"
    rollout.parent.mkdir(parents=True)
    stamp = "2026-10-29T19:00:00Z"
    entries = [{"type": "session_meta", "payload": {
        "id": sid, "session_id": sid, "timestamp": stamp, "cwd": str(workspace),
        "originator": "codex-tui", "cli_version": "0.153.4", "source": "cli",
        "model_provider": "arui_openai"}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "Remember " + marker}]}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "Remember " + marker,
         "images": [], "local_images": [], "text_elements": []}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "Stored " + marker}]}},
        {"type": "event_msg", "payload": {"type": "agent_message", "message": "Stored " + marker}}]
    rollout.write_text("".join(json.dumps({"timestamp": stamp, **entry}) + "\n" for entry in entries))
    _, cli = command("gpt-6-astra", "medium", workspace=workspace, resume_session_id=sid)
    cli += " -c " + shlex.quote('model_providers.arui_openai.base_url="http://127.0.0.1:9/v1"')
    subprocess.run(["tmux", "new-session", "-d", "-s", "resume-fixture", "-c", str(workspace),
                    "-e", f"CODEX_HOME={home}", "-e", "OPENAI_API_KEY=unused-test-key",
                    "-x", "120", "-y", "40", cli], check=True, capture_output=True)
    deadline, screen = time.monotonic() + 15, ""
    while time.monotonic() < deadline:
        screen = subprocess.run(["tmux", "capture-pane", "-p", "-t", "=resume-fixture:", "-S", "-200"],
                                capture_output=True, text=True).stdout
        if "Do you trust the contents" in screen and "Yes, continue" in screen:
            subprocess.run(["tmux", "send-keys", "-t", "=resume-fixture:", "Enter"], capture_output=True)
        if marker in screen:
            break
        time.sleep(.1)
    assert marker in screen, "CLI did not restore the selected fixture conversation"
    assert "gpt-6-astra" in screen
