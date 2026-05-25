# 04 — Onboarding & Agent Bootstrap

This document specifies **Phase B**: from the researcher opening the dashboard
URL for the first time, through the onboarding form, to the Principal Researcher
agent having created the experiment repo, written the code, and started the
baseline run.

## 4.1 First open

The researcher opens the dashboard URL on a laptop or phone. The backend has no
completed onboarding yet, so all routes redirect to **`/onboarding`**.

If a passcode was set by `setup.sh`, the researcher is first shown a single
passcode field. On success they get a signed session cookie and proceed.

## 4.2 The onboarding form — fields

The form is one scrollable page, grouped into labelled sections. Every field has
inline help text and validation. Secrets render as masked inputs with a
show/hide toggle. The form auto-saves a draft to the backend on every change so
a dropped connection does not lose work.

### Section 1 — You

| Field | Type | Required | Notes / validation |
|-------|------|----------|--------------------|
| **Your email** | email | yes | Where digests and alerts go. Validated format. Prefilled if known. |

### Section 2 — GitHub

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| **GitHub token** | secret | yes | A PAT with `repo` scope. Tested live (see §4.3). |
| **GitHub username** | text | yes | Used for commits and the repo owner. |
| **GitHub email** | email | yes | Used for `git config user.email`. |
| **New repo name** | text | yes | The experiment repo the agent will create. Validated against GitHub naming rules. Must not already exist on the account (checked live). Also becomes the default dashboard passcode (§4.5). |

### Section 3 — Model providers

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| **Claude (Anthropic) token** | secret | yes | Powers the Principal Researcher. Tested live. |
| **Gemini token** | secret | no | Enables Gemini as a 2nd-opinion consultant. |
| **OpenAI token** | secret | no | Enables OpenAI as a 3rd-opinion consultant. |
| **Dangerously skip permissions** | checkbox | no (default **on**) | Runs `claude --dangerously-skip-permissions`. Inline warning explains the implication; isolation still applies ([doc 09](./09-notifications-and-security.md)). |

### Section 4 — The research

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| **Purpose** | large textarea | yes | The "why" of the research. The big box from the brief — e.g. "uncover a new paradigm to get closer to how humans learn…". Markdown supported. Stored to `data/config/purpose.md`. |
| **Seed ideas** | large textarea | yes | The researcher's starting ideas. Markdown. The agent expands each into a full idea block in `ideas.md`. Stored to `data/config/seed_ideas.md`. |
| **Evaluate function** | large textarea | yes | How results are judged — the val set, the eval procedure (e.g. "@5 accuracy on LFW over all held-out images", "ARC-AGI3 leaderboard score in Local mode"). Stored to `data/config/eval_spec.md`. |
| **Validation metric** | select + unit | yes | One of: `perplexity`, `f1`, `accuracy`, `rmse`, `mse`, `fid`, `bpb`, `reward`, `custom`. Choosing it sets **metric direction** (minimize/maximize) automatically; `custom` exposes a direction toggle and a name field. |
| **Baseline methods** | large textarea | yes | What to run first — the baseline run and any first methods to try before exploring ideas. Stored to `data/config/baseline_methods.md`. |

### Section 5 — Operations

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| **Alert / digest cadence** | select | yes | `off`, `immediate`, `every 1h`, `every 4h`, `every 12h`, `every 24h`. How often proactive emails go out. Crash/stall alerts are always sent regardless if not `off`. |
| **Email sending** | radio + fields | yes | Either **SMTP** (host, port, user, password — Gmail app password works) or **Resend API key**. Needed because "your email" is only the *recipient*; sending needs a relay. See [doc 09](./09-notifications-and-security.md). |
| **Dashboard passcode** | text | no | Gate for the dashboard URL. **Default = the new repo name** (shown as placeholder). **If left blank, no passcode** — the URL alone grants access. |

> Note on the brief: the onboarding list in the brief did not include an
> email-sending relay, only "my email" (the recipient). Sending mail genuinely
> requires SMTP or an API key, so the **Email sending** field is added here as a
> required addition. It is called out explicitly so it is a conscious decision,
> not a silent one.

## 4.3 Live validation

Before **Start** is enabled, the form validates as much as it can without side
effects:

- **GitHub token** — `GET /user` and a scope check; show the resolved username.
- **Repo name** — `GET /repos/{user}/{name}` must 404 (does not exist yet).
- **Claude token** — a minimal Anthropic API call; show the model it resolves.
- **Gemini / OpenAI tokens** — a minimal call each, if provided.
- **SMTP / Resend** — a connection (and optionally a test email to "your email"
  via a "Send test email" button).
- **Required text fields** — non-empty; metric selected.

Each check shows a green check, a red error, or a spinner. **Start** is disabled
until all required checks pass. A "Validate all" button runs them on demand.

## 4.4 Bulk paste — fill everything from one block

This is an explicit, emphasized requirement from the brief: a researcher setting
up many nodes must not click through the whole form each time.

At the top of the onboarding form sits a **"Paste full config"** panel with one
large textarea and a **Parse & fill** button.

- The researcher pastes one block in a documented `KEY: value` format (multi-line
  values use a fenced or indented convention). The frontend parses it and
  populates every matching field, then runs live validation.
- The same panel has a **"Copy current config"** button that serializes the
  *current* form state back into that block — so a researcher fills the form
  once, copies the block, and reuses it on every subsequent node.
- Secrets are included in the copied block (it is the researcher's own machine
  and clipboard); a small warning notes this.

Canonical bulk-paste format:

```
email: fchaubard@gmail.com
github_token: ghp_xxx
github_username: Fchaubard
github_email: fchaubard@gmail.com
repo_name: bs1learning
claude_token: sk-ant-xxx
gemini_token: xxx
openai_token: sk-xxx
dangerously_skip_permissions: true
validation_metric: at5_accuracy
metric_direction: maximize
alert_cadence: 4h
smtp_host: smtp.gmail.com
smtp_port: 587
smtp_user: fchaubard@gmail.com
smtp_pass: xxxx xxxx xxxx xxxx
dashboard_passcode:
purpose: |
  We want to uncover a new paradigm in machine learning to get us closer
  to the way humans learn. Humans update our model meaningfully with only
  a single example (effective batch size = 1). ...
seed_ideas: |
  - Idea 1: ICL (in-context learning) ...
  - Idea 2: JEPA + per-task adapter heads ...
  - Idea 3: gradient agreement filtering ...
  - Idea 4: whitened rolling mix-up ...
eval_spec: |
  Pretrain/representation on ImageNet; continual learning with batch size 1
  on LFW; measure @5 accuracy over all held-out images for all people. ...
baseline_methods: |
  1. Run train.py unmodified to establish the baseline.
  2. Naive SGD batch-size-1 fine-tuning.
  3. Frozen backbone + linear probe.
```

Keys map 1:1 to the form fields. `|` introduces a multi-line block (YAML-style),
which is how the big text fields are pasted. Parsing is tolerant: unknown keys
are ignored with a warning; missing keys leave that field for manual entry.

The same format is what `data/config/onboarding.yaml` is saved as, so a
researcher can also drop a file on the box and the form offers to import it.

## 4.5 The dashboard passcode rule

Per the brief, exactly:

- The **Dashboard passcode** field defaults to **the new repo name**.
- If the researcher **leaves it blank**, the dashboard has **no passcode** — the
  Tailscale URL alone is the access control.
- If set, the dashboard requires it (signed-cookie session, [doc 09](./09-notifications-and-security.md)).

The placeholder text in the field literally shows the repo name so the default
is obvious. A note explains the blank-means-open behavior.

## 4.6 Hitting Start — the bootstrap sequence

When the researcher hits **Start**, the backend runs the `bootstrap/` state
machine. The dashboard immediately switches to the **Bootstrap** view (§4.8),
which shows each step live.

**Step 0 — Persist config.**
Write `data/secrets/.env`, write the four large text files under
`data/config/`, hash and store the passcode, create the `project` row in SQLite.

**Step 1 — Prepare the agent environment.**
Ensure the `claude` user exists and its workspace
(`/home/claude/experiments/`) is ready. Write the agent's `~/.gitconfig`
(`user.name`, `user.email` from onboarding) and its model credentials.

**Step 2 — Open the agent tmux session.**
`tmux new-session -d -s agent`, then inside it `su - claude`, then launch
Claude Code: `claude` plus `--dangerously-skip-permissions` if the checkbox was
set. The session is named `agent` and registered in the `tmux_session` table.

**Step 3 — Feed the setup prompt.**
The backend renders the **setup prompt** (§4.7) from `prompts/setup_prompt.md.j2`
with the onboarding values, and sends it into the agent's tmux pane.

**Step 4 — Watch the agent bootstrap.**
The `agent/` module tails the tmux pane and the `repo/` module watches the
filesystem. The Bootstrap view streams progress as the agent:
1. Clones `zero_order_diffusion_autoresearcher` for reference and reads it
   (especially `program.md`).
2. Creates the new GitHub repo (`ARUI_NEW_REPO_NAME`) under the researcher's
   account and clones it locally.
3. Writes `program.md` adapted to this project's purpose and metric.
4. Writes `train.py`, `prepare.py`, and the `.toml` for this use case.
5. Writes `ideas.md`, seeded from the purpose + the researcher's seed ideas,
   each expanded into a full idea block.
6. Adds `arui` logging to `train.py` ([doc 06](./06-experiment-tracking.md)).
7. Commits and pushes the initial repo.

**Step 5 — Self-test, then baseline.**
The agent runs a quick smoke test of the pipeline, then launches the **baseline
run(s)** — `train.py` unmodified plus the researcher's baseline methods — via the
scheduler ([doc 05](./05-autoresearch-engine.md)). The first completed run is
recorded as the baseline; all later runs compare against it.

**Step 6 — Enter the loop.**
Once the baseline is recorded, the project status flips to `running`, the
dashboard's home becomes the **Experiments** view, and the autonomous research
loop begins. Onboarding is complete.

Each step reports `pending → running → done/failed` to the Bootstrap view over
the `events` WebSocket. A failed step shows the error, the relevant log, and a
**Retry step** button; the state machine is resumable.

## 4.7 The agent setup prompt

The setup prompt is the programmatic equivalent of the giant metaprompts in the
project brief (the "batch size 1 learning" and "ARC PRIZE V3" examples). It is a
Jinja template, `prompts/setup_prompt.md.j2`, rendered with onboarding values.

Its structure:

```
You are the Principal Researcher for an autonomous ML research project.

# Reference
First, git clone https://github.com/Fchaubard/zero_order_diffusion_autoresearcher
locally and read it thoroughly — especially program.md — to understand the
autoresearch loop, the idea-block format, and the experiment discipline.

# Your project
Purpose:
{{ purpose }}

Build a NEW repository in this directory with a similar structure but for this
use case. Create it on GitHub as: {{ github_username }}/{{ repo_name }}
(a {{ "private" if private else "public" }} repo) and clone it locally.

# Required surgery (mirror the reference repo's structure)
- Rewrite train.py for this use case.
- Update the .toml accordingly.
- Update prepare.py: replace the evaluation function with one that computes
  {{ validation_metric }} as defined below; rename val_* accordingly.
- Rewrite program.md so it makes sense for this challenge. You have
  {{ gpu_count }}x {{ gpu_model }} at your disposal.
- Reset ideas.md and seed it with the ideas below — investigate each and
  expand it into a full idea block per program.md's idea template.

# Evaluation
{{ eval_spec }}
Validation metric: {{ validation_metric }} ({{ metric_direction }}).

# Seed ideas
{{ seed_ideas }}

# Baseline / first methods
{{ baseline_methods }}
Your very first run must be the unmodified baseline.

# Experiment tracking
All runs must log via the `arui` SDK (the project's open-source W&B). Import
`arui` in train.py and call arui.init()/arui.log()/arui.finish() — details in
the arui README. Do NOT use wandb.

# Consultants
{% if gemini_key %}You may consult Gemini for a second opinion.{% endif %}
{% if openai_key %}You may consult OpenAI for a third opinion.{% endif %}

# Operating rules
- Keep every GPU busy; never leave a GPU idle.
- Follow program.md's experiment loop exactly: pick highest-EV idea, implement,
  run, log, analyze, update ideas.md, repeat. Never stop on your own.
- After bootstrap and the baseline, enter the loop and run indefinitely.
```

The template is part of the repo and is the main thing a power user would tune;
it is also viewable (read-only) in the dashboard so the researcher always knows
exactly what the agent was told.

## 4.8 The Bootstrap view (UI)

A dedicated full-screen view shown during Phase B. It contains:

- A **vertical stepper** of the six bootstrap steps with live status icons.
- A **file tree** of the experiment repo that fills in as the agent writes
  files; clicking a file shows its current contents (live-updating) —
  `program.md`, `train.py`, `ideas.md` rendered nicely.
- A **live log pane** tailing the agent's tmux session (read-only here; full
  terminal access comes later).
- An **idea preview** that populates as `ideas.md` is written, showing the idea
  blocks the agent generated from the seed ideas.
- A progress line: "Creating GitHub repo…", "Writing train.py…", "Running
  baseline…".

When Step 6 completes, the view offers **"Go to dashboard"** and auto-redirects
after a few seconds. From here on, the journey is [doc 05](./05-autoresearch-engine.md)
and [doc 07](./07-dashboard-ui.md).

## 4.9 Re-onboarding and editing config

Onboarding is a one-time flow, but the config is not frozen:

- A **Settings** screen ([doc 07](./07-dashboard-ui.md) §7.9) lets the
  researcher later edit email, cadence, tokens, passcode, and the four research
  text files. Editing `purpose`/`ideas`/`eval` after the fact updates the files
  the agent reads (and can prompt the agent to re-read them).
- Rotating a token rewrites `.env` and restarts only the affected component.
- The dashboard passcode can be changed or cleared (cleared = open dashboard).
