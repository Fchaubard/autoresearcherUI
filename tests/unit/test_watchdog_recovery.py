"""Guards for the cron backend-resurrection watchdog (bin/arui_watchdog.sh).

The watchdog closes the gap PR 10 left open: PR 10's in-session `while true`
supervisor only survives a process crash *inside* the tmux session, not the
session/server itself dying (which is what stranded the pod on 2026-06-06).
Static guards verify setup.sh wiring; behavioral tests run the real script
against fake infrastructure commands and isolated temporary state.
"""
import os
import signal
import shutil
import subprocess
import time
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WATCHDOG = os.path.join(ROOT, "bin", "arui_watchdog.sh")
SETUP = os.path.join(ROOT, "setup.sh")


def test_watchdog_exists_and_is_executable():
    assert os.path.isfile(WATCHDOG), "bin/arui_watchdog.sh missing"
    assert os.access(WATCHDOG, os.X_OK), "watchdog must be executable"


def test_watchdog_is_valid_bash():
    """`bash -n` parses without spawning anything."""
    if not shutil.which("bash"):
        return
    r = subprocess.run(["bash", "-n", WATCHDOG], capture_output=True, text=True)
    assert r.returncode == 0, f"watchdog has a bash syntax error: {r.stderr}"


def test_watchdog_never_touches_agent_sessions():
    """The watchdog must only manage the infra sessions (arui, arui-cf).
    It must NEVER kill or spawn the agent/author REPLs."""
    src = open(WATCHDOG).read()
    for forbidden in ("kill-session -t agent", "kill-session -t author",
                      "new-session -d -s agent", "new-session -d -s author"):
        assert forbidden not in src, f"watchdog must not manage: {forbidden}"


def test_watchdog_relaunches_missing_backend_and_has_strike_guard():
    src = open(WATCHDOG).read()
    # resurrects on a missing session
    assert "have_session arui" in src
    assert "launch_backend" in src
    # Failure accounting is PID-aware and requires five consecutive checks;
    # this prevents a loaded fresh backend from entering a cron restart loop.
    assert "backend_pid" in src
    assert '"$COUNT" -ge 5' in src
    assert "STRIKE" in src and "healthz" in src.lower()
    # checks the real liveness endpoint
    assert "/healthz" in src


def test_setup_registers_watchdog_in_cron():
    src = open(SETUP).read()
    assert "arui_watchdog.sh" in src, "setup.sh must register the watchdog"
    assert "crontab" in src, "setup.sh must install a crontab entry"
    # must start cron without systemd (containers have docker-init as PID 1)
    assert "service cron start" in src or "cron" in src


def _fake_command(directory: Path, name: str, body: str):
    path = directory / name
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o755)


def test_watchdog_recycles_alive_but_unreachable_tunnel_over_http2(tmp_path):
    """A live cloudflared process is not proof that its route is usable."""
    root = tmp_path / "repo"
    data = root / "data"
    fake_bin = tmp_path / "bin"
    data.mkdir(parents=True)
    fake_bin.mkdir()
    (data / "cloudflared.log").write_text(
        "https://dead-tunnel-route.trycloudflare.com\n"
    )
    tmux_calls = tmp_path / "tmux.calls"

    _fake_command(
        fake_bin,
        "tmux",
        f'echo "$*" >> "{tmux_calls}"\n'
        'case "$1" in has-session) exit 0 ;; *) exit 0 ;; esac',
    )
    _fake_command(
        fake_bin,
        "curl",
        'case "$*" in *127.0.0.1*) exit 0 ;; *) exit 1 ;; esac',
    )
    _fake_command(fake_bin, "pgrep", "exit 1")
    _fake_command(fake_bin, "cloudflared", "exit 0")

    env = os.environ.copy()
    env.update({
        "ARUI_ROOT": str(root),
        "PATH": f"{fake_bin}:{env['PATH']}",
    })
    first = subprocess.run(["bash", WATCHDOG], env=env, capture_output=True)
    assert first.returncode == 0
    first_calls = tmux_calls.read_text()
    assert "kill-session" not in first_calls
    assert "new-session" not in first_calls, "one transient failure must be tolerated"

    second = subprocess.run(["bash", WATCHDOG], env=env, capture_output=True)
    assert second.returncode == 0
    calls = tmux_calls.read_text()
    assert "kill-session -t =arui-cf" in calls
    assert "new-session -d -s arui-cf" in calls
    assert "cloudflared tunnel --protocol http2" in calls
    assert "--no-autoupdate" in calls
    assert "--url http://127.0.0.1:8000" in calls
    assert not (data / ".watchdog_tunnel_strike").exists()


def test_watchdog_preserves_only_safe_runtime_configuration(tmp_path):
    """A cron resurrection keeps tmpfs/model paths without copying tokens."""
    root = tmp_path / "repo"
    data = root / "data"
    fake_bin = tmp_path / "bin"
    data.mkdir(parents=True)
    fake_bin.mkdir()
    environ = tmp_path / "environ"
    environ.write_bytes(
        b"ARUI_DATA_DIR=/dev/shm/arui-runtime.test\0"
        b"ARUI_CODEX_BIN=/opt/codex test\0"
        b"OPENAI_API_KEY=must-not-be-copied\0"
    )
    tmux_calls = tmp_path / "tmux.calls"

    _fake_command(
        fake_bin,
        "tmux",
        f'echo "$*" >> "{tmux_calls}"\n'
        'case "$1:$3" in has-session:=arui) exit 1 ;; '
        'has-session:=arui-cf) exit 0 ;; *) exit 0 ;; esac',
    )
    _fake_command(fake_bin, "curl", "exit 0")
    _fake_command(fake_bin, "pgrep", "echo 4242")
    _fake_command(fake_bin, "cloudflared", "exit 0")

    env = os.environ.copy()
    env.update({
        "ARUI_ROOT": str(root),
        "ARUI_WATCHDOG_ENVIRON_FILE": str(environ),
        "PATH": f"{fake_bin}:{env['PATH']}",
    })
    result = subprocess.run(["bash", WATCHDOG], env=env, capture_output=True)
    assert result.returncode == 0
    saved = (data / ".watchdog_runtime.env").read_text()
    assert "ARUI_DATA_DIR=/dev/shm/arui-runtime.test" in saved
    assert "ARUI_CODEX_BIN=/opt/codex\\ test" in saved
    assert "OPENAI_API_KEY" not in saved
    assert "must-not-be-copied" not in saved
    calls = tmux_calls.read_text()
    assert ".watchdog_runtime.env" in calls


def test_watchdog_does_not_confuse_arui_cf_with_missing_backend(tmp_path):
    """tmux prefix matching must not hide a missing `arui` session."""
    root = tmp_path / "repo"
    data = root / "data"
    fake_bin = tmp_path / "bin"
    data.mkdir(parents=True)
    fake_bin.mkdir()
    tmux_calls = tmp_path / "tmux.calls"

    _fake_command(
        fake_bin,
        "tmux",
        f'echo "$*" >> "{tmux_calls}"\n'
        'case "$1:$3" in has-session:=arui) exit 1 ;; '
        'has-session:=arui-cf) exit 0 ;; *) exit 0 ;; esac',
    )
    _fake_command(fake_bin, "curl", "exit 0")
    _fake_command(fake_bin, "pgrep", "exit 1")
    _fake_command(fake_bin, "cloudflared", "exit 0")

    env = os.environ.copy()
    env.update({"ARUI_ROOT": str(root), "PATH": f"{fake_bin}:{env['PATH']}"})
    result = subprocess.run(["bash", WATCHDOG], env=env, capture_output=True)
    assert result.returncode == 0
    calls = tmux_calls.read_text()
    assert "has-session -t =arui" in calls
    assert "new-session -d -s arui" in calls
    assert "kill-session -t =arui" in calls


def _tunnel_fixture(tmp_path):
    root = tmp_path / "repo"
    data = root / "data"
    fake_bin = tmp_path / "bin"
    data.mkdir(parents=True)
    fake_bin.mkdir()
    (data / "cloudflared.log").write_text(
        "https://old-test-tunnel.trycloudflare.com\n"
    )
    calls = tmp_path / "tmux.calls"
    _fake_command(fake_bin, "tmux", f'echo "$*" >> "{calls}"\nexit 0')
    _fake_command(fake_bin, "pgrep", "exit 1")
    _fake_command(fake_bin, "cloudflared", "exit 0")
    _fake_command(fake_bin, "curl",
                  'case "$*" in *127.0.0.1*) exit 0 ;; *) exit 1 ;; esac')
    env = {**os.environ, "ARUI_ROOT": str(root),
           "PATH": f"{fake_bin}:{os.environ['PATH']}"}
    return data, fake_bin, calls, env


def test_watchdog_tunnel_failures_reset_when_hostname_changes(tmp_path):
    data, _, calls, env = _tunnel_fixture(tmp_path)
    strike = data / ".watchdog_tunnel_strike"

    subprocess.run(["bash", WATCHDOG], env=env, check=True, timeout=5)
    assert strike.read_text().split() == [
        "https://old-test-tunnel.trycloudflare.com", "1"]

    # A freshly launched URL can briefly fail DNS while being provisioned.
    # It must get its own first strike, not complete the previous URL's pair.
    with (data / "cloudflared.log").open("a") as log:
        log.write("https://replacement-test-tunnel.trycloudflare.com\n")
    subprocess.run(["bash", WATCHDOG], env=env, check=True, timeout=5)
    assert strike.read_text().split() == [
        "https://replacement-test-tunnel.trycloudflare.com", "1"]
    assert "kill-session" not in calls.read_text()

    subprocess.run(["bash", WATCHDOG], env=env, check=True, timeout=5)
    assert "kill-session -t =arui-cf" in calls.read_text()
    assert not strike.exists()


def test_watchdog_discards_legacy_count_only_tunnel_strike(tmp_path):
    data, _, calls, env = _tunnel_fixture(tmp_path)
    strike = data / ".watchdog_tunnel_strike"
    strike.write_text("1\n")
    subprocess.run(["bash", WATCHDOG], env=env, check=True, timeout=5)
    assert strike.read_text().split() == [
        "https://old-test-tunnel.trycloudflare.com", "1"]
    assert "kill-session" not in calls.read_text()


def test_watchdog_overlapping_check_exits_without_probing_or_striking(tmp_path):
    data, fake_bin, calls, env = _tunnel_fixture(tmp_path)
    entered, release = tmp_path / "entered", tmp_path / "release"
    _fake_command(fake_bin, "curl",
                  'case "$*" in *127.0.0.1*) exit 0 ;; esac\n'
                  f'touch "{entered}"\n'
                  f'while [ ! -f "{release}" ]; do sleep 0.02; done\n'
                  'exit 1')
    first = subprocess.Popen(["bash", WATCHDOG], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 5
        while not entered.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert entered.exists(), "first watchdog did not enter its health check"
        first_calls = calls.read_text()
        second = subprocess.run(["bash", WATCHDOG], env=env,
                                capture_output=True, timeout=2)
        assert second.returncode == 0
        assert calls.read_text() == first_calls
        assert not (data / ".watchdog_tunnel_strike").exists()
    finally:
        release.touch()
        first.communicate(timeout=5)
    assert first.returncode == 0
    assert (data / ".watchdog_tunnel_strike").read_text().split()[-1] == "1"
    assert "kill-session" not in calls.read_text()


def test_watchdog_lock_is_not_inherited_by_spawned_session(tmp_path):
    data, fake_bin, calls, env = _tunnel_fixture(tmp_path)
    child_pid = tmp_path / "session-child.pid"
    _fake_command(
        fake_bin, "tmux",
        f'echo "$*" >> "{calls}"\n'
        'case "$1:$3" in has-session:=arui) exit 1 ;; esac\n'
        'if [ "$1" = new-session ]; then\n'
        '  sleep 30 >/dev/null 2>&1 &\n'
        f'  echo "$!" > "{child_pid}"\n'
        'fi\nexit 0',
    )
    _fake_command(fake_bin, "curl", "exit 0")
    try:
        subprocess.run(["bash", WATCHDOG], env=env, check=True, timeout=5)
        pid = int(child_pid.read_text())
        os.kill(pid, 0)  # The simulated tmux child outlives the watchdog.
        lock = subprocess.run(["flock", "-n", str(data / ".watchdog.lock"),
                               "true"], capture_output=True, timeout=2)
        assert lock.returncode == 0, "session child retained the watchdog lock"
    finally:
        if child_pid.exists():
            try:
                os.kill(int(child_pid.read_text()), signal.SIGTERM)
            except ProcessLookupError:
                pass
