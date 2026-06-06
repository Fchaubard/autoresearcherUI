"""Unit tests for agent_watcher — the background thread that turns
agent tmux pane content into Events for the activity feed."""
from __future__ import annotations

import pytest


@pytest.fixture
def watcher_env(monkeypatch, tmp_path):
    """Provide a hermetic env: per-test pane_stream dir + a fake
    _emit that just records the (phase_key, message) tuples it would
    have persisted. Returns the recorded list."""
    from backend.app import pane_stream, agent_watcher
    term_dir = tmp_path / ".term"
    term_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(pane_stream, "_TERM_DIR", term_dir)
    # Reset per-test bookkeeping
    monkeypatch.setattr(agent_watcher, "_offset", {})
    monkeypatch.setattr(agent_watcher, "_stream_origin", {})
    monkeypatch.setattr(agent_watcher, "_emitted", {})
    captured: list = []

    def fake_emit(phase_key, severity, message):
        captured.append((phase_key, severity, message))

    monkeypatch.setattr(agent_watcher, "_emit", fake_emit)
    return agent_watcher, captured


def test_scan_detects_known_phases(watcher_env):
    """Each phase keyword in the pane should emit exactly one Event
    with the expected message — and only once per session."""
    aw, captured = watcher_env
    from backend.app import pane_stream
    sess = "agent"
    pane_stream.term_file(sess).write_bytes(
        b"Read the file _setup_prompt.txt in this directory\n"
        b"Running nvidia-smi to see GPUs\n"
        b"Scaffolding research code (writing train.py)\n"
        b"Calling _smoke run to test the script\n"
        b"POST /api/council/bless\n"
    )
    aw._scan_session(sess)
    phases = [k for (k, _, _) in captured]
    assert "brief_sent" in phases
    assert "nvidia_check" in phases
    assert "scaffold_code" in phases
    assert "smoke_test" in phases
    assert "request_bless" in phases
    # Re-scanning the same pane content should NOT re-emit.
    captured.clear()
    aw._scan_session(sess)
    assert captured == []


def test_scan_handles_ansi_escapes(watcher_env):
    """The pane stream contains ANSI escapes; the watcher should
    strip them before pattern matching so green/red coloring of
    log lines doesn't break detection."""
    aw, captured = watcher_env
    from backend.app import pane_stream
    sess = "agent"
    # Colored "Scaffolding research code" — green prefix + reset.
    pane_stream.term_file(sess).write_bytes(
        b"\x1b[32mScaffolding research code\x1b[0m now\n")
    aw._scan_session(sess)
    phases = [k for (k, _, _) in captured]
    assert "scaffold_code" in phases


def test_scan_incremental_only_new_bytes(watcher_env):
    """Subsequent scans should only process bytes written since the
    previous scan — older content shouldn't fire phases again."""
    aw, captured = watcher_env
    from backend.app import pane_stream
    sess = "agent"
    tf = pane_stream.term_file(sess)
    tf.write_bytes(b"Read the file _setup_prompt.txt\n")
    aw._scan_session(sess)
    initial = list(captured)
    assert any(k == "brief_sent" for (k, _, _) in initial)
    # Append more — second scan should add only NEW phases.
    with open(tf, "ab") as f:
        f.write(b"Running nvidia-smi now\n")
    aw._scan_session(sess)
    new_only = captured[len(initial):]
    new_phases = [k for (k, _, _) in new_only]
    assert "nvidia_check" in new_phases
    # And nothing from the first scan re-fires.
    assert "brief_sent" not in new_phases


def test_scan_no_session_is_noop(watcher_env):
    """Scanning a session that doesn't exist yet (no raw file) should
    not crash and not emit anything."""
    aw, captured = watcher_env
    aw._scan_session("never_existed")
    assert captured == []


def test_event_ids_are_unique_across_calls():
    """Regression test: agent_watcher originally used a *seeded* RNG
    (random.Random(20260531)) at module scope, so every backend
    restart produced the same first-N event IDs — which collided
    with the existing rows on disk and the SQLite UNIQUE constraint
    rejected every emit. Now uses os.urandom; this test guards
    against accidental re-seeding."""
    from backend.app.agent_watcher import _event_id
    ids = {_event_id() for _ in range(200)}
    assert len(ids) == 200, "event IDs collided — RNG re-seeded?"
    for x in ids:
        assert x.startswith("ev-")


def test_emit_uses_real_bus_instance_not_module():
    """Regression test for the original bug: ``from . import bus`` then
    ``bus.publish(...)`` resolves to the MODULE (no publish attr), not
    the Bus instance. Every other module does ``from .bus import bus``;
    agent_watcher must do the same.

    Check: agent_watcher.bus is the Bus INSTANCE and has a callable
    `publish` method. If someone changes the import line in the future,
    this test will fail loudly instead of silently breaking the
    activity feed."""
    from backend.app import agent_watcher
    from backend.app.bus import bus as real_bus
    assert agent_watcher.bus is real_bus
    assert callable(getattr(agent_watcher.bus, "publish", None))


def test_council_approved_distinct_from_rejected(watcher_env):
    """Approval and rejection are different phases — both should be
    detectable from their JSON payload signatures."""
    aw, captured = watcher_env
    from backend.app import pane_stream
    sess = "agent"
    pane_stream.term_file(sess).write_bytes(
        b'curl response: {"status": "approved", "summary": "ok"}\n')
    aw._scan_session(sess)
    phases = [k for (k, _, _) in captured]
    assert "council_approved" in phases
    assert "council_rejected" not in phases


def test_auth_zombie_triggers_restart(watcher_env, monkeypatch):
    """When the author pane shows 'Not logged in · Please run /login'
    AND a Claude API key is present in env, the watchdog must
    automatically kill+respawn the author session, then emit a recovery
    Event so the user sees what happened. This is the 2026-06-06 case
    where /clear corrupted the REPL session state."""
    import backend.app.agent_watcher as aw_mod
    aw, captured = watcher_env
    # Mock subprocess.run (used by tmux capture-pane).
    class _R:
        def __init__(self, stdout=b""):
            self.stdout = stdout
            self.returncode = 0
    def fake_run(cmd, *a, **k):
        if isinstance(cmd, list) and cmd[:2] == ["tmux", "capture-pane"]:
            return _R(b"Welcome back!\n"
                     b"\xe2\x9d\xaf  test prompt\n"
                     b"Not logged in \xc2\xb7 Please run /login\n")
        return _R()
    monkeypatch.setattr(aw_mod.__dict__["subprocess"]
                        if "subprocess" in aw_mod.__dict__
                        else __import__("subprocess"),
                        "run", fake_run)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    restarted: list = []
    monkeypatch.setattr(aw, "_restart_session",
                        lambda s: (restarted.append(s), True)[1])
    aw._last_restart.clear()
    aw._last_auth_check.clear()
    aw._check_auth_zombie("author")
    assert restarted == ["author"]
    assert any(k == "auth_zombie_recovered"
               for (k, _, _) in captured)


def test_auth_zombie_rate_limited(watcher_env, monkeypatch):
    """Two zombie detections within the cooldown window must only
    trigger ONE restart — otherwise a genuine auth outage (expired
    key) would melt into a restart loop."""
    import subprocess as _sp
    aw, _ = watcher_env
    def fake_run(cmd, *a, **k):
        class R: pass
        R.stdout = b"Not logged in \xc2\xb7 Please run /login\n"
        R.returncode = 0
        return R
    monkeypatch.setattr(_sp, "run", fake_run)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    calls: list = []
    monkeypatch.setattr(aw, "_restart_session",
                        lambda s: (calls.append(s), True)[1])
    aw._last_restart.clear()
    aw._last_auth_check.clear()
    aw._check_auth_zombie("author")
    aw._last_auth_check.clear()  # bypass per-session check interval
    aw._check_auth_zombie("author")
    assert calls == ["author"]


def test_auth_zombie_skipped_if_no_api_key(watcher_env, monkeypatch):
    """If no Claude key is in env, don't restart — respawn would just
    hit the same wall, and we'd emit noise every cycle."""
    import subprocess as _sp
    aw, captured = watcher_env
    def fake_run(cmd, *a, **k):
        class R: pass
        R.stdout = b"Not logged in \xc2\xb7 Please run /login\n"
        R.returncode = 0
        return R
    monkeypatch.setattr(_sp, "run", fake_run)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ARUI_CLAUDE_BIN", raising=False)
    calls: list = []
    monkeypatch.setattr(aw, "_restart_session",
                        lambda s: (calls.append(s), True)[1])
    aw._last_restart.clear()
    aw._last_auth_check.clear()
    aw._check_auth_zombie("author")
    assert calls == []
    assert not any(k == "auth_zombie_recovered"
                   for (k, _, _) in captured)


def test_port_pin_refuses_taken_port(monkeypatch):
    """backend.main._check_port_or_die must SystemExit if 8000 is
    already bound, instead of silently re-binding to a random port and
    leaving cloudflared pointing at a dead origin."""
    from backend import main as _main
    import socket as _s
    # Bind a sentinel socket on a port, then verify the check raises.
    sock = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
    sock.setsockopt(_s.SOL_SOCKET, _s.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", 0))
    port = sock.getsockname()[1]
    sock.listen(1)
    try:
        with pytest.raises(SystemExit) as exc:
            _main._check_port_or_die(port)
        assert exc.value.code == 2
    finally:
        sock.close()


def test_port_pin_passes_when_free():
    """When the port is free, _check_port_or_die returns silently."""
    from backend import main as _main
    import socket as _s
    s = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
    s.bind(("0.0.0.0", 0))
    port = s.getsockname()[1]
    s.close()
    # No SystemExit expected — function returns None.
    assert _main._check_port_or_die(port) is None
