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
#   - If cloudflared is alive but its published URL is unreachable for two
#     consecutive checks -> recycle it using HTTP/2 (some GPU providers drop
#     the QUIC/UDP transport while leaving the process alive forever).
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
TUNNEL_STRIKE="$ROOT/data/.watchdog_tunnel_strike"
mkdir -p "$ROOT/data"

have_session() { tmux has-session -t "$1" 2>/dev/null; }
backend_up()   { curl -fsS -m 10 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; }

# Probe the public route, not merely the cloudflared process. A quick tunnel
# can remain alive while every QUIC reconnect times out and its hostname has
# already gone NXDOMAIN. Restrict the probe target to Cloudflare-assigned
# hostnames parsed from our own log so this can never become an arbitrary URL
# fetcher if the log contains unrelated text.
tunnel_url() {
  [ -f "$CFLOG" ] || return 1
  grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$CFLOG" 2>/dev/null | tail -1
}

tunnel_up() {
  URL="$(tunnel_url)" || return 1
  [ -n "$URL" ] || return 1
  curl -fsS -m 10 "$URL/healthz" >/dev/null 2>&1
}

# Return the Python backend PID (not the tmux/bash supervisor). Recording the
# PID with the strike count prevents failures from carrying across a normal
# respawn and gives a fresh process time to load a large historical registry.
backend_pid() {
  pgrep -f "^[^ ]*python[^ ]* -m backend\.main$" 2>/dev/null | head -1
}

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
  tmux kill-session -t arui-cf 2>/dev/null || true
  tmux new-session -d -s arui-cf \
    "while true; do cloudflared tunnel --protocol http2 --url http://localhost:$PORT 2>&1 | tee -a $CFLOG; echo '[arui-cf] cloudflared exited; respawning in 2s' >>$CFLOG; sleep 2; done"
  echo "[watchdog $(date -u +%FT%TZ)] relaunched tunnel session 'arui-cf'" >>"$CFLOG"
}

# ── backend ──────────────────────────────────────────────────────────────
if ! have_session arui; then
  launch_backend
  rm -f "$STRIKE"
elif ! backend_up; then
  # A loaded backend can spend well over three seconds reconciling hundreds
  # of historical runs. The old binary two-strike marker repeatedly killed
  # each fresh process before it stabilized. Count failures per Python PID and
  # recycle only after five consecutive cron checks (~5 minutes). A normal
  # process respawn resets the count automatically.
  PID="$(backend_pid)"
  OLD_PID=""; COUNT=0
  if [ -f "$STRIKE" ]; then
    read -r OLD_PID COUNT <"$STRIKE" || true
  fi
  case "$COUNT" in ''|*[!0-9]*) COUNT=0 ;; esac
  if [ -z "$PID" ] || [ "$PID" != "$OLD_PID" ]; then COUNT=1; else COUNT=$((COUNT + 1)); fi
  printf '%s %s\n' "$PID" "$COUNT" >"$STRIKE"
  if [ "$COUNT" -ge 5 ]; then
    echo "[watchdog $(date -u +%FT%TZ)] healthz failed $COUNT consecutive checks for pid ${PID:-missing}; recycling 'arui'" >>"$LOG"
    launch_backend
    rm -f "$STRIKE"
  fi
else
  rm -f "$STRIKE"
fi

# ── tunnel ───────────────────────────────────────────────────────────────
if command -v cloudflared >/dev/null 2>&1; then
  if ! have_session arui-cf; then
    launch_tunnel
    rm -f "$TUNNEL_STRIKE"
  elif ! tunnel_up; then
    COUNT=0
    if [ -f "$TUNNEL_STRIKE" ]; then
      read -r COUNT <"$TUNNEL_STRIKE" || true
    fi
    case "$COUNT" in ''|*[!0-9]*) COUNT=0 ;; esac
    COUNT=$((COUNT + 1))
    printf '%s\n' "$COUNT" >"$TUNNEL_STRIKE"
    if [ "$COUNT" -ge 2 ]; then
      echo "[watchdog $(date -u +%FT%TZ)] public tunnel failed $COUNT consecutive checks; recycling 'arui-cf' with HTTP/2" >>"$CFLOG"
      launch_tunnel
      rm -f "$TUNNEL_STRIKE"
    fi
  else
    rm -f "$TUNNEL_STRIKE"
  fi
fi
