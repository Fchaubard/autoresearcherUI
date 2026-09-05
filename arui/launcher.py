"""Objective-agnostic process supervisor used by ``bin/arun``.

User research programs are untrusted from the harness's perspective: they may
return without calling ``arui.finish()``, raise, receive SIGTERM, or be killed
by the OOM killer. This wrapper keeps the tmux parent alive long enough to tee
output and independently report the real process exit code to the backend.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request


def _report(run_id: str, returncode: int) -> bool:
    base = os.environ.get(
        "ARUI_INGEST_URL", "http://127.0.0.1:8000").rstrip("/")
    token = os.environ.get("ARUI_INGEST_TOKEN", "")
    payload = json.dumps({"run_id": run_id,
                          "exit_code": int(returncode)}).encode()
    req = urllib.request.Request(
        f"{base}/api/track/process-exit", data=payload, method="POST",
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {token}"} if token else {})})
    # Keep the tmux wrapper alive across backend reloads. In production a
    # reload can take tens of seconds while DuckDB releases its single-writer
    # lock; the old 7.75-second retry window let a completed run disappear
    # before its exit was recorded, and crashed_silently then mislabeled it.
    # The endpoint is idempotent, so retain the tmux session and retry for a
    # bounded five-minute reconciliation window. This also means the watchdog
    # continues to see the session as alive while accounting catches up.
    try:
        retry_window = max(1.0, float(os.environ.get(
            "ARUI_EXIT_REPORT_RETRY_SEC", "300")))
    except ValueError:
        retry_window = 300.0
    deadline = time.monotonic() + retry_window
    delay = 0.0
    while True:
        if delay:
            time.sleep(delay)
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return 200 <= response.status < 300
        except (OSError, urllib.error.URLError):
            if time.monotonic() >= deadline:
                return False
            delay = min(15.0, 0.25 if not delay else delay * 2)


def run(command: list[str], run_id: str, log_path: str) -> int:
    if not command:
        raise ValueError("a command is required")
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    child = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        start_new_session=True)

    def _forward(signum, _frame):
        try:
            os.killpg(child.pid, signum)
        except ProcessLookupError:
            pass

    previous = {}
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        previous[sig] = signal.signal(sig, _forward)
    try:
        assert child.stdout is not None
        with path.open("ab", buffering=0) as log:
            while True:
                chunk = child.stdout.read(65536)
                if not chunk:
                    break
                log.write(chunk)
                try:
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.buffer.flush()
                except (BrokenPipeError, OSError):
                    pass
        returncode = child.wait()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    if not _report(run_id, returncode):
        print(f"[arun] warning: could not report process exit for {run_id}",
              file=sys.stderr, flush=True)
    return returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--log", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    return run(command, args.run_id, args.log)


if __name__ == "__main__":
    raise SystemExit(main())
