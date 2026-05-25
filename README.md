# autoresearcherUI

**A self-hosted web cockpit for autonomous AI research.**

Rent a GPU box, `git clone` this repo, run one script, and get a URL. Fill in a
research purpose and a few seed ideas, and a Claude Code agent autonomously
creates a fresh research repo, writes the training code, and runs experiments
around the clock — keeping every GPU busy — while a live dashboard streams
plots, an experiment table, terminal access, and a chat channel to the agent.

It is the cockpit for Karpathy's
[`zero_order_diffusion_autoresearcher`](https://github.com/Fchaubard/zero_order_diffusion_autoresearcher):
same "the human edits `program.md`, the agent edits `train.py`" philosophy, plus
a real-time UI and a private, open-source experiment tracker (a single-researcher
alternative to Weights & Biases).

> **Status — v0.2.** This repo contains the **full specification** (`docs/`),
> a **runnable backend + dashboard**, the `arui` tracking SDK, the
> **autonomous research orchestrator** (idea queue → EV-ranked scheduling →
> real `train.py` subprocesses → metric ingestion → journal), and a green
> **end-to-end integration test** that gates every merge to `main`. The agent
> is pluggable: `FakeAgent` (deterministic, hardware-free — used by the e2e
> test and demoable today) works; `RealAgent` (drives the real Claude Code CLI
> on GPUs) is the one remaining seam — see [`RUNBOOK.md`](RUNBOOK.md) and
> [`docs/10-roadmap-and-milestones.md`](docs/10-roadmap-and-milestones.md).

---

## Quick start (test it in 30 seconds)

```bash
git clone <this-repo> autoresearcherui
cd autoresearcherui
./dev.sh
```

Then open **http://localhost:8000**.

`dev.sh` creates a virtualenv, installs dependencies (FastAPI, DuckDB,
SQLAlchemy — uses [`uv`](https://docs.astral.sh/uv/) if present, else `pip`),
and starts the dashboard. No Node.js, no build step — the dashboard is served
directly by the backend.

You'll land in a **populated, live dashboard**: a demo project (`bs1learning`)
with four experiments running on four GPUs, charts updating in realtime, an
EV-ranked experiment table, an auto-written research journal, and a working
agent chat. When a run finishes, the next queued idea is automatically launched
on the freed GPU — a live preview of the autonomous loop.

### On an actual GPU node

```bash
./setup.sh          # installs deps, optionally joins your Tailscale net, runs
```

### Troubleshooting

If startup fails with a SQLite `disk I/O error` (can happen when the repo
lives on an iCloud-synced or network folder), point the data directory at
local disk:

```bash
ARUI_DATA_DIR=/tmp/autoresearcherui ./dev.sh
```

## What's in the box

```
autoresearcherui/
├── docs/              ← the full 12-part spec (start at docs/README.md)
├── backend/           ← FastAPI: REST + SSE + arui ingest + the dashboard
│   ├── main.py
│   └── app/
│       ├── api.py          ← all routes (doc 08)
│       ├── models.py       ← SQLAlchemy metadata models
│       ├── metrics.py      ← DuckDB metric store (doc 06 / doc 11 D2)
│       ├── bus.py          ← SSE pub/sub (doc 11 D1)
│       ├── orchestrator.py ← the autonomous research loop (doc 05)
│       ├── agent.py        ← FakeAgent + RealAgent (the pluggable agent)
│       ├── repo.py         ← ideas.md parser
│       ├── sim.py          ← live demo simulator
│       ├── seed.py         ← demo data
│       └── static/         ← the dashboard (vanilla JS, no build)
├── arui/              ← the experiment-tracking SDK (wandb/mlop-compatible)
├── tests/             ← the e2e integration test + the example project
│   ├── e2e_test.py
│   ├── run_e2e.sh     ← the merge-to-main gate
│   └── example_project/  ← tiny-sgd: the e2e research fixture
├── prompts/           ← the agent setup-prompt + program.md templates
├── setup.sh           ← GPU-node installer
└── dev.sh             ← local dev runner
```

## Testing

```bash
bash tests/run_e2e.sh
```

The end-to-end integration test boots the real backend, runs the autonomous
loop on the bundled `tiny-sgd` example project (the baseline plus four ideas,
launched as real `train.py` subprocesses that log through `arui`), and asserts
the whole pipeline via the HTTP API — 17 checks. It is hardware-free (CPU, no
LLM) and gates every merge to `main` ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)).

## The dashboard

| View | What it shows |
|------|---------------|
| **Overview** | Headline metric vs. baseline, live charts, GPU utilization, activity feed, the EV-ranked queue. |
| **Experiments** | Every run — running / upcoming / completed — with heatmap-tinted results, client-side search & filter. Click any row for a full detail drawer. |
| **Live Graphs** | All runs overlaid, charts with a shared hover cursor, updating in realtime. |
| **Journal** | The auto-written narrative of the project. |
| **Agent Chat** | Talk to the Principal Researcher. |

Resize the window narrow to see the mobile layout (bottom tab bar).

## Using the `arui` tracker

`arui` is a tiny, dependency-free, `wandb`-compatible logger. Point any training
script at the running backend and its metrics appear live in the dashboard:

```python
import arui
arui.init(project="bs1learning", name="my-experiment",
          config={"lr": 1e-4})
for step in range(1000):
    arui.log({"train_loss": loss, "at5_acc": acc}, step=step)
arui.finish()
```

## The spec

The complete design lives in [`docs/`](docs/). Start with
[`docs/README.md`](docs/README.md). It was reviewed by GPT-4o and Gemini;
[`docs/11-refinements-v0.2.md`](docs/11-refinements-v0.2.md) records that review
and the final architecture decisions.

## License

MIT
