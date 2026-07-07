#!/usr/bin/env bash
# autoresearcherUI — backend resurrection watchdog.
#
# WHY THIS EXISTS
#   PR 10 wrapped the backend in an in-session `while true` supervisor, so
#   a Python crash / OOM / accidental Ctrl-C inside the pane respawns in 2s.
#   But that loop lives *inside* the tmux session. If the whole session or
#   the tmux server dies — `tmux kill-server`, a server crash, the session
#   getting unlinked/clobbered, or the pod's container restarting — there is
#   nothing left to bring the backend back. That is exactly what stranded
#   the pod on 2026-06-06: the `arui` session simply vanished and the public
#   URL went to a dead origin until someone SSHed in.
#
#   GPU pods (RunPod / vast) run as containers with `docker-init` as PID 1,
#   not systemd, so a systemd unit is not an option. cron *is* available
#   (started by setup.sh), survives a tmux-server death (separate subsystem),
#   and is the right layer for "make sure the session exists".
#
# WHAT IT DOES (idempotent, safe to run every minute from cron)
#   - If the `arui` backend session is GONE        -> relaunch the supervisor.
#   - If the session exists but /healthz has been failing for two
#     consecutive runs (a wedged/hung process the in-loop supervisor can't
#     catch because the process didn't exit) -> recycle it.
#   - If the `arui-cf` cloudflared session is gone -> relaunch the tunnel.
#
#   It NEVER touches the agent / author sessions, and never kills a healthy
#   backend (single transient healthz blip is tolerated via the 2-strike
#   marker so we don't fight PR 10's normal 2s respawn window).
#
#   Deployment env (ARUI_CLAUDE_BIN, ARUI_TELEMETRY_DISABLED, ARUI_DATA_DIR
#   override, …) is read from an OPTIONAL, gitignored `data/arui.env` if it
#   exists, so a respawned backend matches how it was first launched.
set -u

ROOT="${ARUI_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
PORT="${ARUI_PORT:-8000}"
LOG="$ROOT/data/arui.log"
CFLOG="$ROOT/data/cloudflared.log"
STRIKE="$ROOT/data/.watchdog_healthz_strike"
mkdir -p "$ROOT/data"

have_session() { tmux has-session -t "$1" 2>/dev/null; }
backend_up()   { curl -fsS -m 3 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; }

launch_backend() {
  tmux kill-session -t arui 2>/dev/null || true
  tmux new-session -d -s arui \
    "cd $ROOT && { [ -f data/arui.env ] && set -a && . ./data/arui.env && set +a; } ; while true; do \
       ARUI_PORT=$PORT .venv/bin/python -m backend.main 2>&1 | tee -a $LOG; \
       echo \"[arui] backend exited at \$(date -u +%FT%TZ); respawning in 2s\" >>$LOG; \
       sleep 2; \
     done"
  echo "[watchdog $(date -u +%FT%TZ)] relaunched backend session 'arui'" >>"$LOG"
}

launch_tunnel() {
  tmux new-session -d -s arui-cf \
    "while true; do cloudflared tunnel --url http://localhost:$PORT 2>&1 | tee -a $CFLOG; echo '[arui-cf] cloudflared exited; respawning in 2s' >>$CFLOG; sleep 2; done"
  echo "[watchdog $(date -u +%FT%TZ)] relaunched tunnel session 'arui-cf'" >>"$CFLOG"
}

# ── backend ──────────────────────────────────────────────────────────────
if ! have_session arui; then
  launch_backend
  rm -f "$STRIKE"
elif ! backend_up; then
  # Session exists but isn't answering. Could be the normal 2s respawn
  # window (PR 10) — tolerate ONE failing run; recycle only on the second.
  if [ -f "$STRIKE" ]; then
    echo "[watchdog $(date -u +%FT%TZ)] healthz failed twice; recycling 'arui'" >>"$LOG"
    launch_backend
    rm -f "$STRIKE"
  else
    touch "$STRIKE"
  fi
else
  rm -f "$STRIKE"
fi

# ── tunnel ───────────────────────────────────────────────────────────────
if command -v cloudflared >/dev/null 2>&1 && ! have_session arui-cf; then
  launch_tunnel
fi
