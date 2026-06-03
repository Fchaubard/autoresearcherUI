# autoresearcherUI

**`v0.0.1`** &nbsp;·&nbsp; **MIT** &nbsp;·&nbsp; **self-hosted** &nbsp;·&nbsp; 

> **AutoresearcherUI = Autoresearcher + wandb + datadog + overleaf + iTerm + claude code. All local and all free! Just boot up a node, git clone, bash setup.sh, fill out the onboarding form and let it research rip! Login to the research cockpit URL anywhere on earth.**

AutoresearcherUI is a opensource, single-binary cockpit that makes you the PI with as many autonomous researchers as you want writing papers for you. 

Basic AutoresearcherUI flow per node:
1. Rent a GPU box
2. git clone
3. `bash setup.sh`
4. copy the URL
5. Open it and fill out the onboarding form (namely the research purpose and objective function)
6. Then the research agent is good to go! Leave it alone and let it hillclimb. It will create a fresh research repo, write the `train.py`, spin up a council of agents to review the code, do a baseline run, then try to beat it, maintain a priority queue of ideas, and runs them around the clock updating you via email and the live dashboard until you are ready for your research to stop exploring and start writing the paper. Then the researcher agent will hand off the control of the GPUs to an author agent to nail down and prove specific claims and auto-draft the LaTeX paper and do all required ablation runs to support the claims of the paper. More detail below.

---

## Quickstart

```bash
git clone https://github.com/Fchaubard/autoresearcherui
cd autoresearcherui && bash setup.sh
```

The full installer is one script that installs all system deps, Node.js, Claude Code, `uv`,
Python deps, auto-login to Claude Code from your api key, the backend in tmux, and a
cloudflared quick-tunnel so the dashboard is reachable from anywhere on
earth. You can specify a passcode as you need to lock down the node. 

Re-running `setup.sh` is safe; if `~/.claude/` already has credentials
the OAuth step is skipped. Re-runs restart everything else.

## What you get

It's five tools collapsed into one self-hosted process. Bring your own GPU box;
none of these services need to be reachable, paid for, or signed up for.

| You used to need | autoresearcherUI gives you |
|---|---|
| **karpathy-style autoresearcher agent + iTerm + council** | Same `program.md` / `train.py` / `ideas.md` philosophy, plus a web terminal UI that allows you to control the node, a scheduler that keeps every GPU saturated, and a research journal that writes itself. We also have a council of agents (Gemini/GPT/Claude) to review work and improve code/ideas. |
| **wandb / neptune / mlflow** | for tracking and analysis. The `arui` SDK (drop-in `wandb`-compatible API) writing into local DuckDB, live charts with shared-hover, an Analysis tab with filters/eye-toggles, and a per-run drawer with full plots and logs. |
| **datadog / grafana** | Live per-GPU utilization and memory monitoring, run reconciler, system-stats block (disk / RAM / GPU) alerts in every email and systems. |
| **overleaf** | Paper Mode: a real LaTeX repo under `paper/`, an Author Agent that takes runs that win and ablates them to see if they will scale, hardening claims, and integrates finished ablations into figures and sections. |
| **PI Agent / Council** | An hourly PI Agent that nags whichever one is active (research agent or author agent), and a Council (Gemini + GPT-5, Claude tiebreaker) to review all code and reviewing every kept run to generate lessons and next ideas to try. |

## Two modes

**Research Mode** is the default. You write a one paragraph purpose and you can seed a
few ideas. The Research Agent (Claude Code, autonomous, in a `tmux` session
called `agent`) edits karpathy's `train.py`, queues runs, extends `ideas.md`, and the
orchestrator fans them across your GPUs. The Council reviews all code and each kept
result and feeds "lessons learned" back into the ideas queue. The PI Agent
checks in hourly helping with: idle GPUs, diverging runs, off-track queues, it types
messages straight into the agent's tmux as if a real PI walked by. 

**Paper Mode** is for when you think you've found something worth publishing. Flip the
toggle in the *Write the paper* tab to switch agents. The Research Agent pauses research runs. The Author
Agent (Claude Code, in a `tmux` session called `author`) takes over for ablation: writes the paper, it owns
the ablation queue, picks the experiments that will fill the figures, watches
results stream in via `arui`, kills divergers, and integrates every finished
run into the LaTeX. You approve a small Decision Queue (claim wording,
baselines to add, related-work to cite). The Lit Agent pulls candidates from
arXiv + Semantic Scholar. Flip back to Research at any time.

## The agents

```
                       ┌─────────────────────┐
                       │      PI Agent       │  every hour, nags whoever's active
                       └──────────┬──────────┘
                                  │ switches by mode
                  ┌───────────────┴───────────────┐
                  ▼                               ▼
   ┌──────────────────────────┐      ┌──────────────────────────┐
   │     Research Agent       │      │      Author Agent        │
   │     (Claude Code)        │      │      (Claude Code)       │
   │  ideas.md → train.py     │      │  ablations + LaTeX +     │
   │  → queue → kill divergers│      │  figure integration      │
   └────────────┬─────────────┘      └────────────┬─────────────┘
                │ on every kept run               │ on every paper run finish
                ▼                                 ▼
   ┌──────────────────────────┐      ┌──────────────────────────┐
   │        Council           │      │      Paper Runner        │
   │  Gemini ↔ GPT-5 debate   │      │  bin-packs paper-mode    │
   │  + Claude tiebreaker     │      │  runs onto idle GPUs     │
   └──────────────────────────┘      └──────────────────────────┘
                                                  │
                                                  ▼
                                     ┌──────────────────────────┐
                                     │       Lit Agent          │
                                     │  arXiv + Semantic Scholar│
                                     │  → cite candidates       │
                                     └──────────────────────────┘
```

- **Research Agent** — Claude Code in `tmux:agent`. Owns `ideas.md` and
  `train.py`. Generates ideas, edits the script, queues runs, kills divergers.
- **Author Agent** — Claude Code in `tmux:author`. Owns the ablation queue
  and the LaTeX. Each finished paper-mode run is integrated into figures and
  sections in real time via a tmux poke from `/api/track/finish`.
- **PI Agent** — hourly. Reads GPU saturation, the last ~12 runs, the agent's
  recent output, and the top of `ideas.md`; types short concrete nudges.
  Switches persona by mode.
- **The Council** — runs in two places. **(1)** Once at startup as the
  **code-bless gate** (see below). **(2)** After every kept run (batched
  every N), Gemini and GPT-5 independently review then debate up to N
  rounds; consensus applies, deadlocks go to Claude. Every round is
  persisted on the run.
- **Lit Agent** — pulls candidates from arXiv + Semantic Scholar, ranks by
  relevance, files cite-candidate decisions.
- **Paper Runner** — daemon that reads paper-mode `Run` rows with
  `status='queued'`, resolves deps, bin-packs onto the GPU table, launches
  them. Local backend in v0.0.1; SLURM/K8s pluggable later.

## The default safety pattern: council code-bless

**No training runs launch until the council has reviewed and approved
the codebase.** This is the default behaviour, on by default, and you
cannot turn it off through the UI. It's the most important guardrail
to make sure the original code is functioning properly and avoids hallucinations.

Every time the Research Agent (re)spawns a new project:

```
agent: scaffolds program.md, train.py, prepare.py, ideas.md
agent: (recommended) launches a _probe or _smoke run that bypasses the
       gate, to confirm the script actually imports + does one optimiser
       step before wasting council tokens
agent: POST /api/council/bless
backend: reads every .py / .md / .yaml / .json / .sh in the workspace
         (skipping caches, datasets, checkpoints, .venv)
         and sends it to every available reviewer (Gemini, OpenAI) in
         parallel with a strict "find BLOCKERS not style nits" prompt
reviewers: return {approved, blockers:[...], suggestions:[...]}
backend: ALL reviewers must approve. Verdict persisted to Setting
         `code_bless` and broadcast on SSE.
agent: polls GET /api/council/bless/status every ~10 s
   - "pending"   → keep waiting
   - "approved"  → launch the baseline
   - "rejected"  → reads the `blockers` list, fixes the code, then
                   POST /api/council/bless again
```

Server-side enforcement: `POST /api/track/run` returns
**HTTP 423 Locked** unless `code_blessed=true`. The agent's `arui.init()`
call fails immediately, the agent reads the JSON body's `bless_status`
field, and knows exactly what to fix. (Run names starting with `_probe`
or `_smoke` bypass the gate so the agent can sanity-check the script
imports BEFORE submitting for review.)

The council's brief, paraphrased: catch BLOCKERS, not style nits. Real
blockers it looks for: `arui.summary["__METRIC__"]` misspelled or
missing; metric direction mismatch (logging loss while the project says
"maximize"); training set leaks into the eval set; baseline doesn't
match `program.md`; script crashes on import; never calls `.backward()`;
off-by-ones in epoch/step counting; dataset path that doesn't exist on
this node. What it explicitly DOESN'T flag: style, hyperparameter
choices, "consider also trying X".

You see the verdict live on the dashboard:

| State | Banner |
|---|---|
| `approved` | small green ✓ **code blessed** notification in the header |
| `pending` | violet banner: *"Council is reviewing the codebase…"* |
| `rejected` | red banner listing every blocker as bullets + *Clear & await re-review* button |
| `not_requested` | grey *"Awaiting code review"* note |

If you have no OpenAI or Gemini key configured, the bless auto-approves
with an honest "no reviewers configured, auto-approved" note. So you
can still run autoresearcherUI Claude-only; you just don't get the
code-bless protection on the baseline.

Want to force a re-review (e.g., the agent fixed something the council
flagged)? Hit **Clear & await re-review** on the banner, or
`POST /api/council/bless/reset`, the next run attempt will trigger a
fresh review.

## Screens

**Onboarding & Settings** — one form is the entire config surface. Email
for alerts, optional GitHub creds for repo sync, optional Claude / Gemini /
OpenAI tokens (Claude unlocks the agents; Gemini + OpenAI unlock the
council), the project's research question, seed ideas, validation metric,
baseline, the dangerously-skip-permissions toggle, and the agent's raw
`program.md`. Everything is editable later from the Settings modal.
![Onboarding / Settings](docs/screenshots/onboarding-settings.png)



**Dashboard** — the live cockpit. Headline metric vs. baseline plotted
across every experiment ever run, a per-GPU heat strip up top, the
running-best vs. baseline summary cards, a sortable / filterable table of
all runs, and the right-rail Research Agent terminal so you can see what
Claude is actually thinking. The amber banner is fired when the research
agent is intentionally paused (paper mode).
![Dashboard](docs/screenshots/dashboard.png)



**Analysis** — W&B-style multi-run charts. Eye-toggle column to control
which runs are drawn, filter modal (status / metric / config), shared-hover
across every panel, two-way row↔line hover, smoothing slider, log toggle,
and expand-any-panel-to-full-pane. Click a row to open the per-run drawer
with every plot, the raw logs, and the council's review.
![Analysis](docs/screenshots/analysis.png)



**Lessons learned** — auto-written by the Council after each strategic
review. Every entry summarizes a batch of runs ("twelfth batch in a row
repeats the same y=5 bf16 diffusion jobs…"), names what to do next, and
links the run ids it's reasoning over. The Research Agent reads these on
every tick. It's how the system avoids re-trying ideas that already
failed.
![Lessons learned](docs/screenshots/lessons-learned.png)



**Sessions** — live tmux output for any training run, the research agent,
or the author agent. Useful for the times when a specific run is
misbehaving and you want raw stdout/stderr instead of the aggregated
metric view.
![Tmux Sessions](docs/screenshots/tmux-sessions.png)



**Write the paper (Paper Mode)** — flip the toggle and the Research Agent
pauses, the Author Agent starts. Live LaTeX PDF preview on the left,
sub-tabs across the bottom (Today, Claim Coverage, Paper Plan, Critical
Path, Related Work, Versions, Rebuttal, Share), and the Author Agent
terminal on the right showing it integrating finished ablations into the
draft in real time.
![Write the paper](docs/screenshots/write-the-paper.png)


**Read-only share link** at `/p/<token>` — mint it from the Share tab,
send it to a co-author. They see the latest PDF, the claims (with
evidence-strength chips), and the section-status pills: no login, no
write access, no risk of someone editing your in-flight LaTeX.
![Send the paper / share link](docs/screenshots/send-the-paper.png)



**System Stats** — per-GPU utilization + VRAM + temperature, host CPU /
RAM / disk-free, API latency. Two **Maintenance** buttons that have saved
my pod twice this week: *Purge old run logs* (configurable age + bottom-%)
and *Keep SOTA only* (aggressive: drops every checkpoint except the
project-best run).
![System Stats](docs/screenshots/system-stats.png)


**Research-mode email digest** — hourly by default (configurable
`immediate` / `1h` / `4h` / `12h` / `24h` / `off`). Headline progress
chart, what beat baseline, what's training now with ETAs, what's next on
deck. The "node health" block at the bottom (not shown here) shows disk
and RAM with a warning chip if anything is low.
![Research-mode email](docs/screenshots/email-researcher.png)


**Paper-mode email digest** — daily. Different content: claims completed,
days to deadline, decisions waiting on you, citations the Lit Agent
pulled, ablations finished and integrated, author-agent commits. The
forward-to-co-author button drops them straight onto the read-only share
view.
![Paper-mode email](docs/screenshots/email-author.png)


## Emails

- **Research Mode** — hourly digest by default (configurable: `immediate` /
  `1h` / `4h` / `12h` / `24h` / `off`). `immediate` sends the moment a run
  beats the project's best metric.
- **Paper Mode** — daily 9-section digest: progress, claims coverage,
  decisions waiting on you, recent ablations, figure integration status,
  related-work additions, the council's latest take, system-stats, and a
  read-only co-author share link.
- Delivery auto-detects: Resend if a key is present, otherwise SMTP (Gmail
  app-password works out of the box).

### Getting a Gmail app password

If you want emails, you need to set `EMAIL` to a Gmail address during onboarding, you need an **app password** from
`GMAIL_APP_PW` (your normal login won't work — Google blocks SMTP for it).

1. Turn on **2-Step Verification** at
   [myaccount.google.com/security](https://myaccount.google.com/security).
   This is required; app passwords don't exist without it.
2. Open [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).
3. Type `autoresearcherUI` (or anything) in the "App name" box and click
   **Create**.
4. Google shows a 16-character code formatted like `abcd efgh ijkl mnop`.
   **Strip the spaces** and paste it into `GMAIL_APP_PW=` during onboarding.

If you don't see an "App passwords" page at all, 2-Step Verification isn't
on yet. 

## Configuration

All config lives in the onboarding form and then the settings tab: project purpose, validation metric,
optional API keys (Gemini, OpenAI, Anthropic), passcode gate, email recipients,
digest cadence, extra GPU nodes (SSH paste-in), and the raw `program.md` for
hand-tuning the agent's setup prompt. 

## Disk maintenance

Pods fill up fast with checkpoints and logs (we recommend a least 1TB on the node for this reason) — tmux scrollback and checkpoints. Two one-click cleanups
in System Stats if you need:

- **Purge old run logs** — drops stdout/stderr files of bottom-half runs older
  than N days. Run rows, metrics, reviews stay. Frees GBs in seconds.
- **Keep SOTA only** — walks each run's checkpoint folder and deletes
  everything that isn't the SOTA for that run.

A disk warning auto-appears in both email digests when free space is low.

## Architecture

One FastAPI process (`backend/main.py`) serves REST, SSE, the `arui` ingest,
and the static dashboard (vanilla JS, no build step). Metrics in **DuckDB**
(`data/metrics.duckdb`), metadata in **SQLite** (`data/arui.sqlite`). The
orchestrator launches `train.py` subprocesses against a GPU-slot scheduler.
Agents run as **tmux** sessions (`agent`, `author`) — observable, killable,
attachable. Background services: `monitor` (GPU telemetry + reconciliation),
`pi` (hourly oversight), `paper_runner`, `paper_watcher`, `notify`.

## Hacking

```bash
bash dev.sh                            # local dev server
pytest tests/unit/                     # unit tests
python tests/e2e_test.py               # full e2e (hardware-free, ~20s)
bash tests/run_e2e.sh                  # the merge gate
```

Source layout: `backend/app/` is where everything lives. `api.py` is the route
surface. `orchestrator.py` is the research loop. `agent.py` has the
`FakeAgent` / `RealAgent` split. `paper.py`, `author_agent.py`, `paper_runner.py`,
`paper_watcher.py`, `paper_compile.py`, `lit_agent.py` are paper mode.
`council.py`, `pi.py`, `monitor.py`, `notify.py`, `maintenance.py` are the
support services. `arui/` is the tracker SDK.

## License

MIT — see [LICENSE](LICENSE).

## Credits

Karpathy's `zero_order_diffusion_autoresearcher` (the `program.md` / `train.py` /
`ideas.md` philosophy), Anthropic's Claude Code (the agents), FastAPI, DuckDB,
uv, cloudflared (the boring magic).
