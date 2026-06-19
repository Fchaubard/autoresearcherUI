"""Surface the research agent's high-level phases as Events.

Before this, the Summary / activity feed was empty for the first
several minutes after onboarding while the agent was doing the most
important thing on the timeline — scaffolding code, requesting code
bless, launching baselines. Users couldn't tell whether anything was
happening, and would either reload the page or kill the session.

What this does: a background thread polls the agent's tmux pane
(via :mod:`pane_stream`) every 8 seconds, looks for known phase
keywords in the new bytes since the last scan, and emits an :class:`Event`
the first time each phase appears. Each (phase × project_id × agent
session) is emitted at most once per agent session so the feed stays
informative instead of noisy.

The detection is deliberately conservative — better to miss a phase
than to spam the feed. The dashboard's existing event-rendering code
shows these alongside Council reviews / run start-finish / token
failures.
"""
from __future__ import annotations

import os
import re
import threading
import time
import datetime as dt
from typing import Iterable

from . import pane_stream
from .bus import bus
from .db import SessionLocal
from .models import Event


def _event_id() -> str:
    # 8 hex bytes (16 chars) of os.urandom — no seeding, no collisions
    # across backend restarts. archive.py uses the same scheme.
    return "ev-" + os.urandom(6).hex()


def _iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


# Each rule fires the first time its `pattern` appears in the agent
# pane after `start()` was called. Patterns are matched against UTF-8
# decoded bytes (ANSI escapes are stripped first so we match on the
# user-visible text).
#
# Format: (phase_key, regex, severity, message)
#
# phase_key is what we use to de-duplicate within a session — emit at
# most once per key. Keep keys stable across versions so they don't
# duplicate after a Claude Code text change.
_PHASE_RULES: list[tuple[str, str, str, str]] = [
    ("brief_sent",      r"Read the file _setup_prompt\.txt",
     "info",  "Research agent received the research brief"),
    ("nvidia_check",    r"\bnvidia-smi\b",
     "info",  "Research agent: checking GPU state"),
    ("read_spec",       r"_setup_prompt\.txt|cat .*program\.md",
     "info",  "Research agent: reading the project spec"),
    ("scaffold_code",   r"(Scaffold(?:ing)? research code|create program\.md|"
                        r"writing train\.py|writing prepare\.py)",
     "info",  "Research agent: scaffolding baseline code"),
    ("smoke_test",      r"(_smoke|Smoke test|smoke run|smoke check)",
     "info",  "Research agent: running smoke test"),
    ("request_bless",   r"(council/bless|Requesting council|"
                        r"POST .*bless\b)",
     "info",  "Research agent: requesting council code-bless"),
    ("council_approved", r'"status":\s*"approved"',
     "info",  "Council: code approved — training runs unblocked"),
    ("council_rejected", r'"status":\s*"rejected"',
     "warning", "Council: code rejected — agent is fixing blockers"),
    ("launch_baseline", r"(Launching baseline|new-session -d -s .* train\.py|"
                        r"tmux new-session.*python train)",
     "info",  "Research agent: launching baseline training run"),
    ("ideas_loaded",    r"ideas\.md.*pending",
     "info",  "Research agent: ideas.md backlog loaded"),
    ("env_setup",       r"(pip install|uv pip install|"
                        r"huggingface-cli|hf download|datasets\.load_dataset)",
     "info",  "Research agent: setting up environment"),
    ("api_authed",      r"ARUI_INGEST_TOKEN|Authorization: Bearer",
     "info",  "Research agent: authenticated against dashboard API"),
]

_compiled = [(k, re.compile(p, re.IGNORECASE), sev, msg)
             for (k, p, sev, msg) in _PHASE_RULES]


# Track which phases we've already emitted for which (session, stream_size_at_start)
# A new stream (rotated raw file) resets the set so a restart re-emits.
_emitted: dict[tuple[str, int], set[str]] = {}
# Per-session last-read byte offset.
_offset: dict[str, int] = {}
_stream_origin: dict[str, int] = {}     # bytes at first sight — marker for rotation
_lock = threading.Lock()


# ANSI escape stripping — keeps the regex matching against plain text.
_ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")


def _decode(chunk: bytes) -> str:
    return _ANSI_RE.sub(b"", chunk).decode("utf-8", errors="ignore")


def _emit(phase_key: str, severity: str, message: str) -> None:
    """Persist an Event + publish to SSE. Best-effort — drops on DB error."""
    try:
        db = SessionLocal()
        try:
            ev = Event(
                id=_event_id(),
                type="agent_phase",
                severity=severity,
                actor="research_agent",
                message=message,
                run_id="",
                idea_id="",
                created_at=_iso(),
            )
            db.add(ev)
            db.commit()
            payload = ev.dict()
        finally:
            db.close()
        bus.publish("events", "event", payload)
        bus.publish("events", "runs_changed", {})  # refresh activity feed
        print(f"[agent_watcher] emitted {phase_key}: {message}", flush=True)
    except Exception as e:                              # noqa: BLE001
        print(f"[agent_watcher] _emit failed: {e}", flush=True)


def _scan_session(session: str) -> None:
    """Read new bytes from the session's raw stream and emit any
    newly-detected phase events."""
    cur_size = pane_stream.size(session)
    if cur_size == 0:
        return
    with _lock:
        last_off = _offset.get(session, 0)
        origin = _stream_origin.get(session, cur_size)
        # Rotation detection: file got smaller → reset bookkeeping.
        if cur_size < last_off:
            _offset[session] = 0
            _stream_origin[session] = cur_size
            _emitted[(session, cur_size)] = set()
            last_off = 0
            origin = cur_size
        elif (session, origin) not in _emitted:
            _emitted[(session, origin)] = set()
        seen: set[str] = _emitted[(session, origin)]

    # Read just the new chunk since last scan.
    chunk, new_off, _size = pane_stream.read_range(
        session, last_off, max_bytes=128 * 1024)
    text = _decode(chunk)
    if text:
        for (phase_key, regex, sev, msg) in _compiled:
            if phase_key in seen:
                continue
            if regex.search(text):
                seen.add(phase_key)
                _emit(phase_key, sev, msg)

    with _lock:
        _offset[session] = new_off


_started = False
_started_lock = threading.Lock()


# ── REPL auth-zombie watchdog ─────────────────────────────────────────
# Claude Code's REPL sometimes shows "Not logged in · Please run /login"
# on every prompt while CLI auth (env var + settings.json) is actually
# fine. The trigger we've seen is `/clear` corrupting REPL session
# state — auth survives the CLI path but the persistent REPL session
# is wedged until killed and respawned. We saw this twice on 2026-06-06.
#
# The fix in code: poll each agent pane every 30s; if the most recent
# scrollback contains "Not logged in" markers (and a separate CLI auth
# check succeeds), restart the affected agent automatically and emit
# an Event so the user knows what happened.
#
# We rate-limit to one auto-restart per session every 5 min so a true
# auth outage (expired key) doesn't melt into a restart loop.
_AUTH_ZOMBIE_MARKERS = (
    "Not logged in · Please run /login",
    "Not logged in · Run /login",
)
_last_restart: dict[str, float] = {}
_RESTART_COOLDOWN_S = 300
_AUTH_CHECK_INTERVAL_S = 30
_last_auth_check: dict[str, float] = {}


def _restart_session(session: str) -> bool:
    """Kill+respawn the named agent session via its restart endpoint.
    Returns True on success."""
    try:
        if session == "author":
            from . import author_agent
            import subprocess as _sp
            _sp.run(["tmux", "kill-session", "-t", "author"],
                    capture_output=True, timeout=5)
            author_agent.start()
            return True
        elif session == "agent":
            from . import realrun
            from .db import SessionLocal as _SL
            from .models import Setting as _S
            import subprocess as _sp
            db = _SL()
            try:
                row = db.query(_S).filter(_S.key == "onboarding").first()
                cfg = dict(row.value) if row and isinstance(row.value, dict) else {}
            finally:
                db.close()
            if not cfg.get("claude_token") and not _stat_env_var():
                return False
            _sp.run(["tmux", "kill-session", "-t", "agent"],
                    capture_output=True, timeout=5)
            realrun.start_real(cfg)
            return True
    except Exception as e:                              # noqa: BLE001
        print(f"[agent_watcher] restart {session} failed: {e}",
              flush=True)
    return False


def _stat_env_var() -> bool:
    import os as _os
    return bool(_os.environ.get("ANTHROPIC_API_KEY")
                or _os.environ.get("ARUI_CLAUDE_BIN"))


def _check_auth_zombie(session: str) -> None:
    """If the pane shows 'Not logged in' markers and we have a working
    API key, kill+respawn the session. Rate-limited."""
    now = time.time()
    if now - _last_auth_check.get(session, 0.0) < _AUTH_CHECK_INTERVAL_S:
        return
    _last_auth_check[session] = now
    # Capture the LAST ~80 lines of the pane. The marker appears at the
    # bottom of every wedged prompt, so a small tail window is enough.
    import subprocess as _sp
    try:
        r = _sp.run(["tmux", "capture-pane", "-t", session, "-p",
                     "-S", "-80"],
                    capture_output=True, timeout=4)
        text = (r.stdout or b"").decode("utf-8", errors="ignore")
    except Exception:                                   # noqa: BLE001
        return
    if not text:
        return
    # Only treat the pane as a wedged auth-zombie when a marker is on the LAST
    # few non-empty lines (the live prompt) — not anywhere in scrollback. The
    # agent can legitimately print these strings mid-run, and matching them in
    # history caused false-positive restarts that killed busy sessions.
    _tail = "\n".join([ln for ln in text.splitlines() if ln.strip()][-3:])
    if not any(m in _tail for m in _AUTH_ZOMBIE_MARKERS):
        return
    # Markers present. Don't restart if cooldown is active.
    if now - _last_restart.get(session, 0.0) < _RESTART_COOLDOWN_S:
        return
    if not _stat_env_var():
        return
    _last_restart[session] = now
    print(f"[agent_watcher] auth-zombie detected in {session} — "
          f"auto-restarting", flush=True)
    if _restart_session(session):
        _emit("auth_zombie_recovered", "warning",
              f"Auto-recovered {session} agent: REPL was stuck on "
              f"'Not logged in' but API key works. Killed + respawned.")
    else:
        _emit("auth_zombie_failed", "warning",
              f"Detected {session} auth-zombie but auto-restart failed. "
              f"Check the Settings tab for a valid Claude token.")


def start(sessions: Iterable[str] = ("agent", "author")) -> None:
    """Spawn the background watcher. Idempotent; safe to call from
    main.py lifespan. Sessions default to the two agent tmux names."""
    global _started
    with _started_lock:
        if _started:
            return
        _started = True
    sess_list = tuple(sessions)
    print(f"[agent_watcher] starting — watching {sess_list}", flush=True)

    def _loop() -> None:
        while True:
            try:
                for s in sess_list:
                    _scan_session(s)
                    _check_auth_zombie(s)
            except Exception as e:                      # noqa: BLE001
                print(f"[agent_watcher] loop error: {e}", flush=True)
            time.sleep(8)

    threading.Thread(target=_loop, daemon=True,
                     name="agent-watcher").start()
