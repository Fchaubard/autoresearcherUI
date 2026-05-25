#!/usr/bin/env bash
# autoresearcherUI — end-to-end integration test runner.
# This is the merge-to-main gate: it must exit 0 before any merge to main.
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

echo "→ running the e2e integration test"
exec "$PY" tests/e2e_test.py
