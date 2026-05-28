# autoresearcherUI

**`v0.0.1`** &nbsp;·&nbsp; **MIT** &nbsp;·&nbsp; **self-hosted** &nbsp;·&nbsp; **no API keys required**

> **AutoresearcherUI = Autoresearcher + wandb + datadog + overleaf + claude code. All local and all free! No api keys! Just boot up a node, git clone, fill out the onboarding form and let it rip! Grab the URL and you are in anywhere on earth.**

A single-binary cockpit for autonomous ML research. Rent a GPU box, clone,
`setup.sh`, get a public URL. Open it, fill the onboarding form, and a Claude
Code agent spins up a fresh research repo, writes `train.py`, queues ideas,
and runs them around the clock — while a live dashboard streams plots, the
agent's terminal, a decision queue, and an auto-drafted LaTeX paper.

---

## Quickstart

```bash
git clone https://github.com/Fchaubard/autoresearcherui
cd autoresearcherui && bash setup.sh
# → setup prints a public https://<id>.trycloudflare.com URL
# → open it, fill onboarding, let it rip
```

For local-only development without the public tunnel:

```bash
bash dev.sh                # or:  python -m backend.main
# → http://localhost:8000
```

The full installer is one script — system deps, `uv`, Python deps, the backend
in tmux, and a cloudflared quick-tunnel so the dashboard is reachable from
anywhere on earth. Re-running `setup.sh` is safe; it restarts everything.

## What you get

It's five tools collapsed into one self-hosted process. Bring your own GPU box;
none of these services need to be reachable, paid for, or signed up for.

| You used to need | autoresearcherUI gives you |
|---|---|
| **wandb / mlflow** for tracking | The `arui` SDK (drop-in `wandb`-compatible API) writing into local DuckDB, live charts with shared-hover, an Analysis tab with filters/eye-toggles, and a per-run drawer with full plots and logs. |
| **datadog / grafana** for ops | Live GPU strip, per-GPU utilization and memory, run reconciler, system-stats block (disk / RAM / GPU) in every email, and two one-click disk-purge buttons. |
| **overleaf** for the paper | Paper Mode: a real LaTeX repo under `paper/`, an Author Agent that integrates finished ablations into figures and sections, a Critical Path Gantt, a Decision Queue, and a `/p/<token>` share link for co-authors. |
| **claude code** + a tmux babysitter | Research Agent + Author Agent running as named tmux sessions, an hourly PI Agent that nags whichever one is active, and a Council (Gemini + GPT-5, Claude tiebreaker) reviewing every kept run. |
| **karpathy's autoresearcher** | Same `program.md` / `train.py` / `ideas.md` philosophy, plus a UI, a scheduler that keeps every GPU saturated, and a research journal that writes itself. |

One process. One port. No external dashboards.

## Two modes

**Research Mode** is the default. You write a one-paragraph purpose and seed a
few ideas. The Research Agent (Claude Code, autonomous, in a `tmux` session
called `agent`) extends `ideas.md`, edits `train.py`, queues runs, and the
orchestrator bin-packs them across your GPUs. The Council reviews each kept
result and feeds "lessons learned" back into the ideas queue. The PI Agent
checks in hourly: idle GPUs, diverging runs, off-track queues — it types
messages straight into the agent's tmux as if a real PI walked by. You can
walk away for a week.

**Paper Mode** is for when you've found something worth publishing. Flip the
toggle in the *Write the paper* tab. The Research Agent pauses. The Author
Agent (Claude Code, in a `tmux` session called `author`) takes over: it owns
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
- **The Council** — after every kept run (batched every N), Gemini and GPT-5
  independently review then debate up to N rounds. Consensus applies;
  deadlocks go to Claude. Every round is persisted on the run.
- **Lit Agent** — pulls candidates from arXiv + Semantic Scholar, ranks by
  relevance, files cite-candidate decisions.
- **Paper Runner** — daemon that reads paper-mode `Run` rows with
  `status='queued'`, resolves deps, bin-packs onto the GPU table, launches
  them. Local backend in v0.0.1; SLURM/K8s pluggable later.

## Screens

![Dashboard](docs/screenshots/dashboard.png)
**Dashboard** — headline metric vs. baseline, live GPU strip, reorderable
idea queue, and the agent rail (Research Agent terminal + Summary feed of
kept runs with council verdicts).

![Analysis](docs/screenshots/analysis.png)
**Analysis** — W&B-style multi-run charts with shared-hover, eye-toggle
column, filter modal, two-way row↔line hover, and expand-to-pane panels.
Click any row → per-run drawer with all plots, logs, and council review.

![Write the paper](docs/screenshots/write-paper.png)
**Write the paper** — Paper Plan, Today, Critical Path, Decision Queue,
Related Work, Sections, Figures, Compile, Rebuttal. Live PDF preview;
one-click recompile.

![Critical Path](docs/screenshots/critical-path.png)
**Critical Path** — real Gantt over the planned ablation set with claim
coverage and section-health pills.

![Decision Queue](docs/screenshots/decision-queue.png)
**Decision Queue** — color-chipped strategic decisions (claim wording,
baselines, citations). `j`/`k`/`Enter`/`R`/`D` shortcuts, bulk actions.

![System Stats](docs/screenshots/system-stats.png)
**System Stats** — disk / RAM / GPU at a glance, plus two **Purge** buttons:
*Purge old run logs* and *Keep SOTA only*.

![Daily email](docs/screenshots/email.png)
**Daily email** — embedded progress chart, ranked runs, ETAs, what's next on
deck, system-stats block (disk warning when low).

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

## Configuration

All config lives in the onboarding form: project purpose, validation metric,
optional API keys (Gemini, OpenAI, Anthropic), passcode gate, email recipients,
digest cadence, extra GPU nodes (SSH paste-in), and the raw `program.md` for
hand-tuning the agent's setup prompt. Keys are optional. Adding them unlocks:

- **Anthropic** → the Research / Author Agents (otherwise FakeAgent runs the
  e2e and the demo).
- **Gemini + OpenAI** → the Council's dual-reviewer debate (Claude tiebreaks).
- **Any LLM key** → Lit Agent + PI Agent.

## Disk maintenance

Pods fill up fast — tmux scrollback and checkpoints. Two one-click janitors
in System Stats:

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

MIT. (Add a `LICENSE` file before tagging the release.)

## Credits

Karpathy's `zero_order_diffusion_autoresearcher` (the `program.md` / `train.py` /
`ideas.md` philosophy), Anthropic's Claude Code (the agents), FastAPI, DuckDB,
uv, cloudflared (the boring magic).
