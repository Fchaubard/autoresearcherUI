#!/usr/bin/env bash
# autoresearcherUI — GPU node installer (doc 03).
# Usage:  ./setup.sh            interactive
#         TS_AUTHKEY=tskey-... ./setup.sh --yes     scripted / bulk
set -e
cd "$(dirname "$0")"
YES=0; [ "$1" = "--yes" ] && YES=1

echo "┌────────────────────────────────────────────┐"
echo "│  autoresearcherUI setup                     │"
echo "└────────────────────────────────────────────┘"

# ── 1. preflight ─────────────────────────────────────────────────────────
if command -v nvidia-smi >/dev/null 2>&1; then
  GPUS=$(nvidia-smi --query-gpu=name --format=csv,noheader | wc -l | tr -d ' ')
  echo "→ detected $GPUS GPU(s)"
else
  echo "!  no NVIDIA GPU detected — running in demo mode (UI only)"
fi

# ── 2. system deps ───────────────────────────────────────────────────────
echo "→ installing system packages (tmux, ttyd, git)…"
if command -v apt-get >/dev/null 2>&1; then
  sudo apt-get update -qq && sudo apt-get install -y -qq tmux git ttyd \
    >/dev/null 2>&1 || echo "!  some packages skipped (no sudo?)"
fi

# ── 3. uv + python deps ──────────────────────────────────────────────────
if ! command -v uv >/dev/null 2>&1; then
  echo "→ installing uv…"
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1
  export PATH="$HOME/.local/bin:$PATH"
fi
echo "→ installing autoresearcherUI…"
uv venv .venv >/dev/null 2>&1 || true
uv pip install --python .venv -e . -q

# ── 4. Tailscale (optional, the only secret setup needs) ─────────────────
if command -v tailscale >/dev/null 2>&1 || [ -n "$TS_AUTHKEY" ]; then
  if [ -z "$TS_AUTHKEY" ] && [ "$YES" -eq 0 ]; then
    echo
    echo "Paste a Tailscale auth key to reach the dashboard remotely,"
    echo "or press Enter to skip (dashboard will be localhost-only)."
    read -rp "Tailscale auth key: " TS_AUTHKEY
  fi
  if [ -n "$TS_AUTHKEY" ]; then
    command -v tailscale >/dev/null 2>&1 || \
      curl -fsSL https://tailscale.com/install.sh | sh >/dev/null 2>&1
    sudo tailscale up --authkey="$TS_AUTHKEY" \
      --hostname="autoresearcher-$(hostname | cut -c1-6)" >/dev/null 2>&1 \
      && echo "→ joined tailnet"
  fi
fi

# ── 5. launch ────────────────────────────────────────────────────────────
echo "→ starting backend in tmux session 'autoresearcherui'…"
tmux kill-session -t autoresearcherui 2>/dev/null || true
tmux new-session -d -s autoresearcherui ".venv/bin/python -m backend.main"

URL="http://localhost:8000"
command -v tailscale >/dev/null 2>&1 && {
  TS=$(tailscale status --json 2>/dev/null | grep -o '"DNSName":"[^"]*"' \
       | head -1 | cut -d'"' -f4 | sed 's/\.$//')
  [ -n "$TS" ] && URL="http://$TS:8000"
}
echo
echo "✅  autoresearcherUI is running."
echo "    Dashboard:  $URL"
echo "    Logs:       tmux attach -t autoresearcherui"
echo
