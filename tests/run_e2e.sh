#!/usr/bin/env bash
# autoresearcherUI — end-to-end integration tests (the merge-to-main gate).
# Both suites must exit 0 before any merge to main.
#   1. e2e_test.py          — the FakeAgent orchestrator path
#   2. e2e_realagent_test.py — the RealAgent path (mock autonomous agent)
set -e
cd "$(dirname "$0")/.."

echo "→ installing autoresearcherUI for the e2e gate"
if command -v uv >/dev/null 2>&1; then
  uv venv .venv >/dev/null 2>&1 || true
  uv pip install --python .venv -e . -q
  PY=.venv/bin/python
else
  python3 -m venv .venv
  ./.venv/bin/pip install --upgrade pip -q
  ./.venv/bin/pip install -e . -q
  PY=./.venv/bin/python
fi

command -v tmux >/dev/null 2>&1 || {
  echo "→ installing tmux (required by the RealAgent path)"
  apt-get install -y -qq tmux >/dev/null 2>&1 || true
}

echo
echo "──────── 1/2  FakeAgent orchestrator e2e ────────"
"$PY" tests/e2e_test.py

echo
echo "──────── 2/2  RealAgent e2e ────────"
"$PY" tests/e2e_realagent_test.py

echo
echo "✅  all e2e suites passed."
