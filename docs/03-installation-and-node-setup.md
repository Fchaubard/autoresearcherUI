# 03 — Installation & Node Setup

This document specifies **Phase A** of the user journey: getting from a bare
rented GPU box to a live dashboard URL. It is a CLI-only phase. Everything that
*can* be deferred to the browser *is* deferred to the browser — `setup.sh` asks
for the absolute minimum.

## 3.1 Prerequisites (the rented node)

- A Linux GPU node (vast.ai or RunPod), Ubuntu 22.04+ recommended.
- One or more NVIDIA GPUs with recent drivers + CUDA (the node images from both
  providers ship this).
- `sudo` / root (needed to create the `claude` user and install Tailscale).
- Outbound internet (model APIs, GitHub, package installs, Tailscale).
- A Tailscale account + an **auth key** (reusable or ephemeral) generated at
  <https://login.tailscale.com/admin/settings/keys>.

`setup.sh` checks all of these and fails fast with a clear message if any are
missing (e.g. "no NVIDIA GPU detected", "not running as root/sudo").

## 3.2 The one-command install

```bash
git clone https://github.com/<user>/autoresearcherui
cd autoresearcherui
./setup.sh
```

`setup.sh` is idempotent — re-running it is safe and resumes/repairs rather than
duplicating.

### What `setup.sh` does, step by step

1. **Preflight checks.** Verify OS, `nvidia-smi` works and reports ≥1 GPU,
   `sudo` available, internet reachable. Detect GPU count and model; store for
   the scheduler.
2. **Install system deps.** `tmux`, `git`, `curl`, `build-essential`, Python
   3.11+ (if absent), and **`uv`** (`curl -LsSf https://astral.sh/uv/install.sh | sh`).
3. **Install Tailscale.** `curl -fsSL https://tailscale.com/install.sh | sh`.
4. **Prompt for the Tailscale auth key.** This is the **only secret `setup.sh`
   strictly requires**. Prompt:

   ```
   ┌─ autoresearcherUI setup ────────────────────────────────┐
   │ Paste your Tailscale auth key (tskey-auth-...).          │
   │ Get one at https://login.tailscale.com/admin/settings/keys
   │ It is used once to join this node to your tailnet.       │
   └──────────────────────────────────────────────────────────┘
   Tailscale auth key: ›
   ```

   It may also be supplied non-interactively for scripted/bulk setup:
   `TS_AUTHKEY=tskey-... ./setup.sh --yes` (see §3.7).
5. **Join the tailnet.** `sudo tailscale up --authkey=<key> --hostname=autoresearcher-<short-id>`.
   Capture the assigned tailnet hostname (e.g. `autoresearcher-a1b2.tailnet-xxxx.ts.net`).
6. **Create the `claude` user.** `sudo useradd -m -s /bin/bash claude`. This is
   the confined user the Principal Researcher runs as (see §3.4 and
   [doc 09](./09-notifications-and-security.md)). Grant it access to the GPUs
   (it is added to the `video`/`render` groups) and to a workspace directory,
   but **not** to `autoresearcherui/data/secrets/`.
7. **Install Claude Code** (and optional `gemini` / `codex` CLIs) for the
   `claude` user, or verify they are installed.
8. **Create the backend Python env.** `uv sync` inside `autoresearcherui/`.
9. **Build the frontend.** `cd frontend && npm ci && npm run build` → static
   assets in `frontend/dist/`, served by FastAPI. (Prebuilt assets may ship in
   the repo so a node without Node.js still works; `setup.sh` builds only if
   needed.)
10. **Initialize storage.** Create `data/`, run Alembic migrations to create
    `data/autoresearch.db`, create `data/secrets/` (`chmod 700`, owned by the
    backend user).
11. **Generate a dashboard passcode.** A random human-typeable passcode (e.g.
    6 words or 8 chars). Stored hashed; shown once in the terminal. The
    researcher can change it during onboarding (where the default becomes the
    repo name — see [doc 04](./04-onboarding-and-agent-bootstrap.md)).
12. **Start the backend.** Launch Uvicorn. Two supported modes:
    - **systemd** (preferred when available): install `autoresearcherui.service`,
      `enable --now`. Auto-restarts on crash, survives reboots.
    - **detached tmux** (fallback): a tmux session `autoresearcherui-server`
      running Uvicorn, so it survives the SSH session closing.
13. **Optional HTTPS.** If `--https` is passed (or answered yes), run
    `tailscale serve` so the dashboard is served over TLS with a tailnet cert.
14. **Print the result.**

    ```
    ✅  autoresearcherUI is running.

        Dashboard:  https://autoresearcher-a1b2.tailnet-xxxx.ts.net
        Passcode:   coral-mantis-ridge-92      (change it during onboarding)

        Open that URL on your laptop or phone (must be on your tailnet)
        and complete onboarding to start researching.

        Logs:   journalctl -u autoresearcherui -f   (or: tmux attach -t autoresearcherui-server)
    ```

The whole run targets **under 5 minutes** on a typical node (most of it is
package installs).

## 3.3 What setup.sh deliberately does NOT ask

Everything else — email, GitHub token, Claude/Gemini/OpenAI tokens, repo name,
research purpose, ideas, eval, metric, alert cadence, the
dangerously-skip-permissions checkbox, the passcode — is collected in the
**browser onboarding form** ([doc 04](./04-onboarding-and-agent-bootstrap.md)).

Rationale: a researcher setting up the node is often on a laptop SSH session,
but the *config* is long, full of secrets, and far nicer to paste into a form
(with validation, masking, and bulk paste) than into a terminal. Keeping the CLI
phase to one secret also makes scripted bulk node-bringup trivial.

## 3.4 The `claude` user and isolation

The Principal Researcher runs `claude --dangerously-skip-permissions`, which by
design lets the agent run arbitrary shell commands without confirmation. To
contain that:

- It runs as the dedicated unix user **`claude`**, never as root and never as
  the backend user.
- `claude`'s home is `/home/claude`. The experiment repo is cloned under a
  workspace it owns (default `/home/claude/experiments/<repo-name>/`).
- `claude` can read/write the experiment repo and run GPU jobs. It **cannot**
  read `autoresearcherui/data/secrets/.env` — the backend injects only the
  specific env vars a given step needs (e.g. the GitHub token during repo
  creation) into that step's environment, rather than exposing the whole file.
- `claude` has **no `sudo`**.
- The backend talks to the agent only by attaching to its tmux session
  (sending keystrokes, capturing the pane) — there is no privileged RPC.

The dangerously-skip-permissions checkbox in onboarding controls only whether
the `claude` invocation includes that flag; the user isolation above is always
applied. See [doc 09](./09-notifications-and-security.md) for the full threat
model.

## 3.5 The `.env` schema

After onboarding, the backend writes `data/secrets/.env` (mode `0600`). It is
the single source of truth for configuration and secrets. `setup.sh` writes a
minimal version (Tailscale + node facts); onboarding fills the rest.

```dotenv
# ── Node facts (written by setup.sh) ───────────────────────────────
ARUI_NODE_ID=autoresearcher-a1b2
ARUI_GPU_COUNT=10
ARUI_GPU_MODEL=NVIDIA A40
ARUI_TAILNET_HOSTNAME=autoresearcher-a1b2.tailnet-xxxx.ts.net
ARUI_DASHBOARD_PORT=443
ARUI_HTTPS=true

# ── Auth (setup.sh sets a default; onboarding may override) ────────
ARUI_PASSCODE_HASH=<bcrypt hash>      # empty hash => no passcode (open dashboard)
ARUI_SESSION_SECRET=<random 32 bytes> # signs session cookies

# ── Researcher identity (onboarding) ───────────────────────────────
ARUI_RESEARCHER_EMAIL=fchaubard@gmail.com

# ── GitHub (onboarding) ────────────────────────────────────────────
GITHUB_TOKEN=ghp_xxx
GITHUB_USERNAME=Fchaubard
GITHUB_EMAIL=fchaubard@gmail.com
ARUI_NEW_REPO_NAME=bs1learning

# ── Model providers (onboarding) ───────────────────────────────────
ANTHROPIC_API_KEY=sk-ant-xxx          # the Principal Researcher
GEMINI_API_KEY=xxx                    # 2nd opinion (optional)
OPENAI_API_KEY=sk-xxx                 # 3rd opinion (optional)

# ── Tailscale (setup.sh) ───────────────────────────────────────────
TS_AUTHKEY=tskey-auth-xxx             # may be cleared after first `up`

# ── Agent behavior (onboarding) ────────────────────────────────────
ARUI_DANGEROUSLY_SKIP_PERMISSIONS=true

# ── Research project (onboarding) ──────────────────────────────────
ARUI_PURPOSE_FILE=data/config/purpose.md       # large text -> file, not inline
ARUI_SEED_IDEAS_FILE=data/config/seed_ideas.md
ARUI_EVAL_SPEC_FILE=data/config/eval_spec.md
ARUI_VALIDATION_METRIC=perplexity              # perplexity|f1|accuracy|rmse|mse|fid|bpb|reward|custom
ARUI_METRIC_DIRECTION=minimize                 # minimize|maximize (derived from metric)
ARUI_BASELINE_METHODS_FILE=data/config/baseline_methods.md

# ── Notifications (onboarding) ─────────────────────────────────────
ARUI_ALERT_CADENCE=4h                 # off | immediate | 1h | 4h | 12h | 24h
ARUI_SMTP_HOST=smtp.gmail.com         # email-sending relay (see doc 09)
ARUI_SMTP_PORT=587
ARUI_SMTP_USER=fchaubard@gmail.com
ARUI_SMTP_PASS=<app password>
# -- or, instead of SMTP --
ARUI_RESEND_API_KEY=re_xxx

# ── Tracking (defaults; rarely changed) ────────────────────────────
ARUI_TRACKING_INGEST_TOKEN=<random>   # arui SDK auth, see doc 06
```

Large free-text inputs (purpose, seed ideas, eval spec, baseline methods) are
stored as **files under `data/config/`**, not inline in `.env`, because they
are multi-paragraph and the agent reads them directly. `.env` holds only their
paths.

A fully documented `.env.example` ships in the repo.

## 3.6 Backend service definition

`systemd` unit (`autoresearcherui.service`):

```ini
[Unit]
Description=autoresearcherUI backend
After=network-online.target tailscaled.service

[Service]
Type=simple
User=researcher
WorkingDirectory=/home/researcher/autoresearcherui
ExecStart=/home/researcher/.local/bin/uv run backend/main.py
Restart=always
RestartSec=3
EnvironmentFile=/home/researcher/autoresearcherui/data/secrets/.env

[Install]
WantedBy=multi-user.target
```

If `systemd` is unavailable (some container-based RunPod images), `setup.sh`
falls back to a detached tmux session and writes a tiny `watchdog.sh`
(cron-driven) that restarts Uvicorn if the port stops responding.

## 3.7 Scripted / bulk node bringup

For a researcher spinning up many nodes, `setup.sh` accepts a non-interactive
mode:

```bash
TS_AUTHKEY=tskey-auth-xxx \
ARUI_HTTPS=true \
./setup.sh --yes
```

`--yes` suppresses all prompts. This pairs with the **bulk-paste onboarding**
([doc 04](./04-onboarding-and-agent-bootstrap.md) §4.4): the researcher keeps
one canonical config block, runs `setup.sh --yes` on each node, opens each
dashboard URL, pastes the block, hits Start. Node N is as fast as node 1.

## 3.8 Updating and teardown

- **Update:** `git pull && ./setup.sh` — idempotent; re-runs `uv sync`, rebuilds
  the frontend if changed, runs new Alembic migrations, restarts the service.
  Running experiments and tmux sessions are untouched (they survive a backend
  restart, §2.4).
- **Teardown:** `./setup.sh --uninstall` stops the service, kills tmux sessions,
  optionally `tailscale logout`, and (with `--purge`) deletes `data/`. The
  experiment repo on GitHub is never touched.

## 3.9 Failure handling during setup

| Failure | `setup.sh` behavior |
|---------|---------------------|
| No GPU detected | Abort with a clear message; the product is GPU-only by design. |
| Tailscale `up` fails (bad key) | Re-prompt for the key up to 3 times, then abort with the admin-keys URL. |
| Port 443/80 in use | Pick the next free port, report it in the final URL. |
| `uv sync` fails | Print the failing package and the log path; exit non-zero so bulk scripts notice. |
| Frontend build fails but prebuilt assets exist | Warn, use the prebuilt assets, continue. |
| `claude` user already exists | Reuse it (idempotent); do not recreate. |
| Backend already running | Restart it cleanly rather than spawning a second instance. |
