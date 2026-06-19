#!/usr/bin/env bash
# One-command deploy of autoresearcherUI to a remote GPU node.
#
# Run this FROM YOUR MAC, inside the repo:
#
#     bash deploy.sh
#
# It copies the repo to the node over SSH, installs it, runs the e2e
# integration test ON THE NODE, and starts the dashboard. Your SSH key never
# leaves your Mac.
#
# Override the defaults with env vars if needed:
#     ARUI_NODE=root@1.2.3.4 ARUI_SSH_PORT=22 ARUI_SSH_KEY=~/.ssh/id_rsa bash deploy.sh
set -euo pipefail

if [ -z "${ARUI_NODE:-}" ]; then
  echo "ARUI_NODE is required, e.g. ARUI_NODE=root@1.2.3.4 ARUI_SSH_PORT=22 bash deploy.sh" >&2
  exit 1
fi
NODE="${ARUI_NODE}"
PORT="${ARUI_SSH_PORT:-22}"
KEY="${ARUI_SSH_KEY:-$HOME/.ssh/id_ed25519}"
REMOTE="/root/autoresearcherui"
HERE="$(cd "$(dirname "$0")" && pwd)"
SSH=(ssh -i "$KEY" -p "$PORT" -o StrictHostKeyChecking=accept-new
     -o ConnectTimeout=15)

echo "→ [1/5] checking the node and its GPUs"
"${SSH[@]}" "$NODE" \
  'nvidia-smi --query-gpu=index,name,memory.total --format=csv,noheader \
     2>/dev/null || echo "(no nvidia-smi on this node)"'

echo "→ [2/5] copying the repo to ${NODE}:${REMOTE}"
"${SSH[@]}" "$NODE" "mkdir -p $REMOTE"
tar czf - -C "$HERE" \
    --exclude=.git --exclude=data --exclude=.venv --exclude=node_modules \
    --exclude='*.egg-info' --exclude=__pycache__ --exclude='.deploy' . \
  | "${SSH[@]}" "$NODE" "tar xzf - -C $REMOTE"

echo "→ [3/5] installing dependencies on the node"
"${SSH[@]}" "$NODE" "apt-get install -y -qq python3-venv python3-pip tmux \
     >/dev/null 2>&1 || true; \
   cd $REMOTE && python3 -m venv .venv && \
   .venv/bin/pip install -q --upgrade pip && .venv/bin/pip install -q -e ."

echo "→ [4/5] running the e2e integration test ON THE NODE"
"${SSH[@]}" "$NODE" "cd $REMOTE && .venv/bin/python tests/e2e_test.py"

echo "→ [5/5] starting the dashboard (tmux session 'arui', port 8000)"
"${SSH[@]}" "$NODE" "cd $REMOTE && tmux kill-session -t arui 2>/dev/null || true; \
   tmux new-session -d -s arui '.venv/bin/python -m backend.main'"

echo
echo "✅  deployed — the e2e test ran on the node and the dashboard is up."
echo
echo "View the dashboard from your Mac (SSH port-forward):"
echo "    ssh -i $KEY -p $PORT -L 8000:localhost:8000 $NODE"
echo "    then open  http://localhost:8000"
echo
echo "Watch the backend logs:   ssh ... then  tmux attach -t arui"
echo "Run the autonomous loop:  curl -X POST localhost:8000/api/dev/run-example"
