# Contributing

This project has a single maintainer. Keep PRs small and focused on one change.
For a larger change, open an issue first to explain the problem and proposed approach.
Report vulnerabilities privately using [SECURITY.md](SECURITY.md).

## Run locally

From the repository root:

```bash
bash dev.sh
```

The script creates a virtual environment, installs dependencies, and starts the
local dashboard with demo data at `http://localhost:8000`.
See [Architecture and Hacking](docs/ARCHITECTURE.md) for the code layout and
how the backend, agents, and tracking SDK fit together.

## Test

Activate the environment created by `dev.sh`, then run:

```bash
source .venv/bin/activate
pytest tests/unit/
python tests/e2e_test.py
bash tests/run_e2e.sh
```

The unit suite covers individual components. The Python end-to-end suite is
hardware-free. `bash tests/run_e2e.sh` is the merge gate: run it before asking
for review, and include the result in the PR. If a check cannot run, give the
command, error, and missing prerequisite.

## Open a pull request

- Explain the problem and what changes for the user.
- Keep unrelated cleanup in a separate PR.
- Add or update tests when behavior changes, and update relevant documentation.
- List the checks you ran and their results.
- Include screenshots for UI changes.
- Remove API keys, passcodes, tunnel URLs, and private research data from diffs,
  logs, and screenshots before submitting them.

The maintainer reviews and merges PRs; a passing merge gate is required for review.
