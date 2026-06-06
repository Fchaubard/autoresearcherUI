#!/usr/bin/env bash
# autoresearcherUI — one-command installer for a fresh GPU node.
#
#   git clone <repo> autoresearcherui
#   cd autoresearcherui
#   ./setup.sh
#
# What it does:
#   1. installs system deps (python3, tmux, curl)
#   2. installs uv + the autoresearcherUI package (and its python deps)
#   3. starts the backend in tmux session 'arui'
#   4. opens a cloudflared quick-tunnel in tmux 'arui-cf' and prints the URL
#      so you can hit the dashboard from anywhere — no Tailscale needed
#
# Flags:
#   --no-tunnel    skip cloudflared (localhost only)
#   --yes          non-interactive
#
# Re-running setup.sh is safe; it restarts everything.
set -e
cd "$(dirname "$0")"
ROOT="$(pwd)"

YES=0; NO_TUNNEL=0
for a in "$@"; do
  case "$a" in
    --yes) YES=1 ;;
    --no-tunnel) NO_TUNNEL=1 ;;
  esac
done

bold() { printf "\033[1m%s\033[0m\n" "$*"; }
step() { printf "  \033[1;36m→\033[0m %s\n" "$*"; }
ok()   { printf "  \033[1;32m✓\033[0m %s\n" "$*"; }
warn() { printf "  \033[1;33m!\033[0m %s\n" "$*"; }

SUDO=""
[ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1 && SUDO="sudo"

bold "┌────────────────────────────────────────────┐"
bold "│  autoresearcherUI — fresh node setup        │"
bold "└────────────────────────────────────────────┘"

# ── 1. preflight ────────────────────────────────────────────────────────
if command -v nvidia-smi >/dev/null 2>&1; then
  GPUS=$(nvidia-smi --query-gpu=name --format=csv,noheader | wc -l | tr -d ' ')
  ok "detected $GPUS GPU(s)"
else
  warn "no NVIDIA GPU detected — UI will still work, agent will run on CPU"
fi

# ── 2. system packages ──────────────────────────────────────────────────
step "installing system packages (python3, tmux, curl, TeX Live)…"
if command -v apt-get >/dev/null 2>&1; then
  $SUDO apt-get update -qq >/dev/null 2>&1 || true
  $SUDO apt-get install -y -qq python3 python3-venv python3-pip tmux curl \
    ca-certificates >/dev/null 2>&1 || warn "some apt packages may have failed"
  # TeX Live for Paper Mode's pdflatex build. Without this, every paper
  # render shows "TeX Live not installed" and the PDF iframe stays
  # empty — Francois hit this on the live pod 2026-06-06. The
  # -recommended bundles are ~700 MB instead of 4 GB for texlive-full;
  # they cover everything our LaTeX template uses (article + amsmath +
  # graphicx + hyperref + booktabs + geometry). We swallow failures
  # because TeX Live isn't required for research-mode — the warning
  # surfaces in the UI instead.
  if ! command -v pdflatex >/dev/null 2>&1; then
    step "installing TeX Live (pdflatex — needed by Paper Mode)…"
    $SUDO apt-get install -y -qq --no-install-recommends \
      texlive-latex-recommended texlive-fonts-recommended \
      texlive-latex-extra >/dev/null 2>&1 \
      && ok "pdflatex installed" \
      || warn "TeX Live install failed — Paper Mode PDF builds will warn"
  fi
fi

# ── 3. python deps via uv (fast) with pip fallback ──────────────────────
if ! command -v uv >/dev/null 2>&1; then
  step "installing uv (fast python package manager)…"
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1 || true
  export PATH="$HOME/.local/bin:$PATH"
fi

step "installing autoresearcherUI python deps…"
if command -v uv >/dev/null 2>&1; then
  uv venv .venv >/dev/null 2>&1 || true
  uv pip install --python .venv -e . -q
else
  python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -e .
fi
ok "deps installed"

# ── 4a. Node.js (needed by Claude Code) ─────────────────────────────────
if ! command -v node >/dev/null 2>&1; then
  step "installing Node.js (Claude Code's runtime)…"
  if command -v apt-get >/dev/null 2>&1; then
    # nvm-free path: use NodeSource's prebuilt apt repo.
    curl -fsSL https://deb.nodesource.com/setup_lts.x | $SUDO bash - \
      >/dev/null 2>&1 || warn "could not add NodeSource repo"
    $SUDO apt-get install -y -qq nodejs >/dev/null 2>&1 \
      || warn "apt-get install nodejs failed"
  fi
  if command -v node >/dev/null 2>&1; then
    ok "node $(node --version) installed"
  else
    warn "Node install didn't finish — install it manually then re-run setup.sh"
  fi
fi

# ── 4b. Claude Code (the research + author agent runtimes) ──────────────
if ! command -v claude >/dev/null 2>&1; then
  step "installing Claude Code (the autonomous agent)…"
  if command -v npm >/dev/null 2>&1; then
    # --location=global is the future-proof flag; fall back to -g for older
    # npm versions. Either way the binary lands in /usr/local/bin/claude.
    $SUDO npm install -g --location=global @anthropic-ai/claude-code \
      >/dev/null 2>&1 \
      || $SUDO npm install -g @anthropic-ai/claude-code >/dev/null 2>&1 \
      || warn "npm install -g @anthropic-ai/claude-code failed"
  fi
  if command -v claude >/dev/null 2>&1; then
    ok "claude $(claude --version 2>/dev/null | head -1) installed"
  else
    warn "Claude Code missing — research agent won't spawn." \
         "Install with: npm install -g @anthropic-ai/claude-code"
  fi
fi

# ── 4c. cloudflared (for the public URL) ────────────────────────────────
if [ "$NO_TUNNEL" -eq 0 ] && ! command -v cloudflared >/dev/null 2>&1; then
  step "installing cloudflared (gives you a public https URL)…"
  ARCH=$(uname -m)
  case "$ARCH" in
    x86_64)  CF=cloudflared-linux-amd64 ;;
    aarch64) CF=cloudflared-linux-arm64 ;;
    *) CF="" ;;
  esac
  if [ -n "$CF" ]; then
    $SUDO curl -fsSL -o /usr/local/bin/cloudflared \
      "https://github.com/cloudflare/cloudflared/releases/latest/download/$CF" \
      && $SUDO chmod +x /usr/local/bin/cloudflared && ok "cloudflared installed" \
      || warn "cloudflared install failed — will fall back to localhost"
  else
    warn "unknown arch $ARCH — skipping cloudflared"
  fi
fi

# ── 5. start backend in tmux 'arui' (with supervisor loop) ──────────────
step "starting backend in tmux session 'arui'…"
PORT="${PORT:-8000}"
tmux kill-session -t arui 2>/dev/null || true
LOG="$ROOT/data/arui.log"
mkdir -p "$ROOT/data"
: > "$LOG"
# SUPERVISE the backend the same way we supervise cloudflared. Without
# this, if a user (or operator, hi Francois) ever sends Ctrl-C into
# the 'arui' pane, the backend stays dead until someone SSHes back in
# and relaunches it manually. The while-loop respawns on any exit
# (including OOM, Python tracebacks, accidental Ctrl-C in the pane),
# and ARUI_PORT is pinned so a respawn can't accidentally land on
# a different port and orphan cloudflared.
tmux new-session -d -s arui \
  "cd $ROOT && while true; do \
     ARUI_PORT=$PORT .venv/bin/python -m backend.main 2>&1 | tee -a $LOG; \
     echo \"[arui] backend exited at \$(date -u +%FT%TZ); respawning in 2s\" >>$LOG; \
     sleep 2; \
   done"

# wait for /healthz
for i in $(seq 1 40); do
  if curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
    ok "backend is up on http://127.0.0.1:$PORT"
    break
  fi
  sleep 0.5
done

# ── 6. cloudflared tunnel ───────────────────────────────────────────────
URL="http://localhost:$PORT"
if [ "$NO_TUNNEL" -eq 0 ] && command -v cloudflared >/dev/null 2>&1; then
  step "opening cloudflared tunnel in tmux 'arui-cf'…"
  CFLOG="$ROOT/data/cloudflared.log"
  : > "$CFLOG"
  tmux kill-session -t arui-cf 2>/dev/null || true
  # SUPERVISE the tunnel — wrap cloudflared in a while-loop so the
  # tunnel auto-respawns when it dies. Without this, ANY backend
  # restart (or a network blip) kills cloudflared permanently and the
  # bookmarked URL goes to NXDOMAIN. trycloudflare assigns a fresh
  # random hostname on each respawn, so the user grabs it via the
  # `/api/url` endpoint (or by tailing data/cloudflared.log) — the
  # important thing is the tunnel never just dies and stays dead.
  tmux new-session -d -s arui-cf \
    "while true; do cloudflared tunnel --url http://localhost:$PORT 2>&1 | tee -a $CFLOG; echo '[arui-cf] cloudflared exited; respawning in 2s' >>$CFLOG; sleep 2; done"
  # parse the public URL out of cloudflared's log
  for i in $(seq 1 40); do
    PUB=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$CFLOG" \
          | head -1 || true)
    [ -n "$PUB" ] && { URL="$PUB"; break; }
    sleep 0.5
  done
  [ -n "$PUB" ] && ok "tunnel up: $URL" \
    || warn "tunnel didn't print a URL — check tmux attach -t arui-cf"
fi

# ── 7. done ─────────────────────────────────────────────────────────────
echo
bold "✅  autoresearcherUI is running."
echo "    Dashboard:    $URL"
echo "    Backend logs: tmux attach -t arui      (Ctrl-b d to detach)"
[ "$NO_TUNNEL" -eq 0 ] && \
  echo "    Tunnel logs:  tmux attach -t arui-cf"
echo
echo "    Next: open $URL in your browser and complete onboarding."
echo "    (paste a Claude API key + your purpose, and the agent starts.)"
echo
