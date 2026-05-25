#!/usr/bin/env bash
# Run autoresearcherUI locally for development / testing.
# Creates a venv, installs deps, and starts the dashboard at http://localhost:8000
set -e
cd "$(dirname "$0")"

echo "→ autoresearcherUI — local dev"

if command -v uv >/dev/null 2>&1; then
  echo "→ using uv"
  uv venv .venv >/dev/null 2>&1 || true
  uv pip install --python .venv -e . -q
  PY=.venv/bin/python
else
  echo "→ uv not found, using python venv + pip"
  python3 -m venv .venv
  ./.venv/bin/pip install --upgrade pip -q
  ./.venv/bin/pip install -e . -q
  PY=./.venv/bin/python
fi

echo "→ starting backend (demo mode: seeded + live)"
echo "→ open  http://localhost:8000"
exec "$PY" -m backend.main
