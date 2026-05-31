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
    --force-claude-auth) ;;   # handled later
    --skip-claude-auth) ;;    # handled later
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
step "installing system packages (python3, tmux, curl)…"
if command -v apt-get >/dev/null 2>&1; then
  $SUDO apt-get update -qq >/dev/null 2>&1 || true
  $SUDO apt-get install -y -qq python3 python3-venv python3-pip tmux curl \
    ca-certificates >/dev/null 2>&1 || warn "some apt packages may have failed"
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

# ── 4c. one-time Claude Code authentication ─────────────────────────────
# Modern Claude Code (`@anthropic-ai/claude-code` npm package) prefers
# its persisted OAuth credentials over ANTHROPIC_API_KEY when running
# --dangerously-skip-permissions. On a fresh node with no prior login,
# it falls back to interactive OAuth — which the autonomous tmux-spawned
# agent CANNOT complete because there's no human in front of that pane.
# So we do OAuth ONCE HERE, in the foreground, BEFORE we install
# cloudflared / start the backend / open the tunnel.
#
# Detection has to be careful: a previous FAILED OAuth attempt leaves
# bookkeeping files in ~/.claude/ (config, projects, settings.json) but
# NO credentials file — and the older check "is the dir empty?" wrongly
# skipped the auth step in that case. So we look for known credential
# filenames specifically, and we ALSO honour an explicit override flag
# in case the detection is wrong for some Claude Code version.

# Allow the user to force or skip claude auth on re-runs.
FORCE_CLAUDE_AUTH=0
SKIP_CLAUDE_AUTH=0
for a in "$@"; do
  case "$a" in
    --force-claude-auth) FORCE_CLAUDE_AUTH=1 ;;
    --skip-claude-auth)  SKIP_CLAUDE_AUTH=1 ;;
  esac
done

claude_has_creds() {
  # Return 0 (true) iff a non-empty credential file exists in any
  # location Claude Code is known to use across versions.
  for f in \
      "$HOME/.claude/.credentials.json" \
      "$HOME/.claude/credentials.json" \
      "$HOME/.claude/auth.json" \
      "$HOME/.claude.json" \
      "$HOME/.config/claude/credentials.json" \
      "$HOME/.config/claude/.credentials.json"; do
    [ -s "$f" ] && return 0
  done
  return 1
}

NEEDS_CLAUDE_AUTH=0
if command -v claude >/dev/null 2>&1; then
  if [ "$FORCE_CLAUDE_AUTH" -eq 1 ]; then
    NEEDS_CLAUDE_AUTH=1
  elif [ "$SKIP_CLAUDE_AUTH" -eq 1 ]; then
    NEEDS_CLAUDE_AUTH=0
  elif claude_has_creds; then
    ok "Claude Code already authenticated (credentials in ~/.claude)"
  else
    NEEDS_CLAUDE_AUTH=1
  fi
fi

if [ "$NEEDS_CLAUDE_AUTH" -eq 1 ]; then
  echo
  bold "┌──────────────────────────────────────────────────────────────┐"
  bold "│  One-time Claude Code authentication (required)              │"
  bold "└──────────────────────────────────────────────────────────────┘"
  echo
  echo "  Claude Code needs to authenticate ONCE on this node BEFORE the"
  echo "  backend can start the autonomous research agent. We're launching"
  echo "  Claude interactively now. You will see three things:"
  echo
  echo "    1. \"Bypass Permissions\" consent  →  press  2  then  Enter"
  echo "    2. An OAuth URL  →  open it in your browser, sign in to"
  echo "       Anthropic, copy the code it shows, paste it back here."
  echo "    3. The Claude REPL ('How can I help…')  →  type:  /exit"
  echo
  echo "  After this, the autoresearcher agent reuses these credentials"
  echo "  automatically — no OAuth prompt during onboarding or restarts."
  echo
  echo "  (Re-run setup.sh with --force-claude-auth to re-do this later,"
  echo "  or --skip-claude-auth to suppress this step entirely.)"
  echo
  if [ "$YES" -eq 1 ]; then
    warn "--yes was passed; SKIPPING interactive auth."
    warn "Before using the dashboard, SSH back in and run:"
    warn "    IS_SANDBOX=1 claude --dangerously-skip-permissions"
    warn "and finish the OAuth flow."
  else
    read -r -p "  Press Enter to launch Claude Code (Ctrl-C to skip)…" _ || true
    # Foreground claude — user clicks through consent + OAuth + /exit.
    # IS_SANDBOX=1 lets root use --dangerously-skip-permissions in
    # containers (the autoresearcher agent sets the same flag, so we
    # mirror that env here for max parity).
    IS_SANDBOX=1 claude --dangerously-skip-permissions || true
    echo
    if claude_has_creds; then
      ok "Claude Code authenticated — credentials persisted to ~/.claude/"
    else
      warn "Claude Code may NOT have completed authentication."
      warn "Verify with:  ls -la ~/.claude"
      warn "If the dashboard shows an OAuth prompt later, SSH back in"
      warn "and run:    IS_SANDBOX=1 claude --dangerously-skip-permissions"
      warn "to finish the flow, then restart the agent from the dashboard."
      if [ "$FORCE_CLAUDE_AUTH" -eq 0 ]; then
        read -r -p "  Continue with setup anyway? [y/N] " ans || true
        case "$ans" in
          y|Y|yes|YES) ;;
          *) echo "  Aborting. Re-run setup.sh once auth is sorted."
             exit 1 ;;
        esac
      fi
    fi
  fi
fi

# ── 4d. cloudflared (for the public URL) ────────────────────────────────
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

# ── 5. start backend in tmux 'arui' ─────────────────────────────────────
step "starting backend in tmux session 'arui'…"
tmux kill-session -t arui 2>/dev/null || true
LOG="$ROOT/data/arui.log"
mkdir -p "$ROOT/data"
: > "$LOG"
tmux new-session -d -s arui \
  "cd $ROOT && .venv/bin/python -m backend.main 2>&1 | tee -a $LOG"

# wait for /healthz
PORT="${PORT:-8000}"
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
  tmux new-session -d -s arui-cf \
    "cloudflared tunnel --url http://localhost:$PORT 2>&1 | tee -a $CFLOG"
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
