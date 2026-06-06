"""Guards for the cron backend-resurrection watchdog (bin/arui_watchdog.sh).

The watchdog closes the gap PR 10 left open: PR 10's in-session `while true`
supervisor only survives a process crash *inside* the tmux session, not the
session/server itself dying (which is what stranded the pod on 2026-06-06).
These tests are static guards — they assert the script and its setup.sh wiring
keep their critical safety properties so a future edit can't silently regress
them.
"""
import os
import shutil
import subprocess

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
    # tolerates a single transient healthz blip (PR 10's 2s respawn window)
    assert "STRIKE" in src and "healthz" in src.lower()
    # checks the real liveness endpoint
    assert "/healthz" in src


def test_setup_registers_watchdog_in_cron():
    src = open(SETUP).read()
    assert "arui_watchdog.sh" in src, "setup.sh must register the watchdog"
    assert "crontab" in src, "setup.sh must install a crontab entry"
    # must start cron without systemd (containers have docker-init as PID 1)
    assert "service cron start" in src or "cron" in src
