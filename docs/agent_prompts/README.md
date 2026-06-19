# autoresearcherUI — Product & Engineering Specification

> **Status:** Draft v0.2 — reviewed by GPT-4o + Gemini; starter repo scaffolded
> **Author:** Francois Chaubard
> **Last updated:** 2026-05-25

> **v0.2 note:** [doc 11](./11-refinements-v0.2.md) records an external
> architecture review and the resulting decisions. **Where doc 11 conflicts with
> docs 02–10, doc 11 wins.** The biggest deltas: SSE (not WebSockets) for
> one-way streams, DuckDB-over-Parquet for metrics (no SQLite metric table),
> `ttyd` for terminals, and a UI "slickness pass". A runnable starter repo
> ships alongside this spec (`backend/`, `frontend/`, `arui/`, `setup.sh`).

autoresearcherUI is a self-hosted control plane that turns an autonomous AI
research agent into something you can **watch, steer, and trust** from your
phone or laptop. You rent a GPU box, `git clone` this repo, run one setup
script, and a few minutes later you have a URL where you fill in what you want
researched. The agent then sets up a brand-new research repository, writes the
training code, seeds an idea backlog, and runs experiments around the clock —
keeping every GPU busy — while streaming live plots, logs, and email digests
back to you.

Think of it as the missing **cockpit** for Karpathy's
[`zero_order_diffusion_autoresearcher`](https://github.com/Fchaubard/zero_order_diffusion_autoresearcher):
same "the human edits `program.md`, the agent edits `train.py`" philosophy, but
with a real-time UI, an open-source experiment tracker (a private,
single-researcher alternative to Weights & Biases), terminal access into every
running job, and a chat channel to the agent in charge.

---

## How to read this spec

This is an **implementation-ready** spec. It is split into the documents below.
Read them in order for a full picture; jump directly if you know what you need.

| # | Document | What it covers |
|---|----------|----------------|
| — | [README.md](./README.md) | This index, the vision, and the glossary |
| 01 | [01-product-overview.md](./01-product-overview.md) | Goals, non-goals, personas, the end-to-end user journey, competitive positioning |
| 02 | [02-architecture.md](./02-architecture.md) | The concrete tech stack, system components, process topology, repo layout, data flow |
| 03 | [03-installation-and-node-setup.md](./03-installation-and-node-setup.md) | `git clone` → `setup.sh` flow, Tailscale, the `claude` user, tmux, the `.env` schema |
| 04 | [04-onboarding-and-agent-bootstrap.md](./04-onboarding-and-agent-bootstrap.md) | The web onboarding form, every field, bulk paste, the passcode, the agent setup prompt |
| 05 | [05-autoresearch-engine.md](./05-autoresearch-engine.md) | The research loop, `program.md`/`train.py`/`ideas.md` generation, the GPU scheduler, the tmux job model |
| 06 | [06-experiment-tracking.md](./06-experiment-tracking.md) | The open-source W&B layer: the `arui` SDK, metric storage, realtime streaming, the mlop vs. minfx decision |
| 07 | [07-dashboard-ui.md](./07-dashboard-ui.md) | Every screen — desktop and mobile: experiments table, EV-ranked queue, experiment reports, live graphs, terminals, agent chat |
| 08 | [08-api-and-data-models.md](./08-api-and-data-models.md) | The full REST + WebSocket API and the SQLite data model |
| 09 | [09-notifications-and-security.md](./09-notifications-and-security.md) | Email alerts/digests, the passcode model, secret handling, the threat model |
| 10 | [10-roadmap-and-milestones.md](./10-roadmap-and-milestones.md) | MVP scope, phased milestones, and open questions |
| 11 | [11-refinements-v0.2.md](./11-refinements-v0.2.md) | **External review (GPT-4o + Gemini) + final v0.2 decisions — authoritative** |

---

## The one-paragraph pitch

A solo ML researcher has an idea ("can a model learn meaningfully from a single
example?"). Today they would rent a GPU node, hand-write a metaprompt, paste it
into Claude Code, and hope. autoresearcherUI productizes that whole ritual. The
researcher rents the node, clones one repo, runs `./setup.sh`, gives it a
Tailscale token, and gets a URL. On that URL — from anywhere — they paste in the
research purpose, their seed ideas, their eval function, their tokens, and hit
**Start**. The system spins up a Claude Code "Principal Researcher" that creates
a new GitHub repo, writes `program.md` / `train.py` / `prepare.py` / `ideas.md`,
runs baseline experiments, and then loops forever: pick the highest-EV idea,
implement it, run it on a free GPU, log results, analyze, generate new ideas.
The researcher watches it all live, gets emailed when something notable happens,
and can drop into any terminal or chat with the agent at any time.

---

## The two repositories

It is important to keep these straight; the rest of the spec depends on it.

1. **`autoresearcherui`** — *this* repository. The product. It is cloned onto
   the GPU node. It contains the setup script, the web backend, the web
   frontend, the orchestrator, the GPU scheduler, the `arui` tracking SDK and
   tracking service, and the agent bootstrap logic. The researcher never edits
   it by hand.

2. **The experiment repo** — created *fresh* by the Principal Researcher agent
   during bootstrap, on the researcher's own GitHub account, with the name they
   choose during onboarding (e.g. `bs1learning`, `arcagi3`). It is structured
   exactly like Karpathy's reference repo: `program.md`, `train.py`,
   `prepare.py`, `ideas.md`, `results.tsv`, `pyproject.toml`. This is where the
   actual research happens. autoresearcherUI observes and orchestrates it.

---

## Glossary

| Term | Meaning |
|------|---------|
| **Node** | The single GPU machine (vast.ai or RunPod instance) running autoresearcherUI. |
| **Principal Researcher** | The long-lived Claude Code agent that runs the research loop. One per node. Also called the "agent in charge". |
| **Consultant** | A secondary model (Gemini / OpenAI) the Principal Researcher can call for a 2nd / 3rd opinion. |
| **Experiment repo** | The fresh research repo created during bootstrap (see above). |
| **`program.md`** | The human-authored instruction file that defines the autonomous research org. The agent reads it; the human edits it. |
| **`ideas.md`** | The agent-maintained backlog of research ideas, each an "idea block" with status and EV. |
| **Idea block** | One structured entry in `ideas.md`: id, description, EV, status, results, analysis, etc. |
| **EV** | Expected Value of improvement — `confidence (0–1) × expected metric gain`. The queue is sorted by EV descending. |
| **Run / Experiment** | One concrete execution of `train.py` with a given config, on one GPU, in one tmux session. |
| **Baseline** | The first run(s): `train.py` executed unmodified, plus any user-specified baseline methods. All later runs are compared to it. |
| **`arui`** | The open-source experiment-tracking SDK shipped by autoresearcherUI. A drop-in `wandb`-style logger (`arui.init / arui.log / arui.finish`). |
| **Tracking service** | The backend component that ingests `arui` metrics and serves them to the dashboard. The "private W&B". |
| **Orchestrator** | The backend component that manages tmux sessions, the agent, the GPU scheduler, and training jobs. |
| **Tailnet** | The user's private Tailscale network. The dashboard is reachable over it by default. |
| **Dashboard** | The web UI served by autoresearcherUI — the cockpit. Responsive: laptop and iPhone. |

---

## Design principles

1. **One node, one researcher, zero ops.** This is not a multi-tenant SaaS. No
   Kubernetes, no ClickHouse, no message broker. SQLite, a single Python
   process, and tmux. It must survive an SSH session dropping.
2. **The human programs in Markdown.** Per Karpathy's design, the researcher's
   leverage is `program.md` and `ideas.md`, never the Python files. The UI makes
   those two files first-class, editable objects.
3. **Never waste a GPU.** The scheduler's prime directive: every GPU should
   always have a run on it. Idle VRAM is a bug.
4. **Observable by default.** Anything the agent does — edit a file, start a
   run, hit an error — is visible live in the dashboard, no SSH required.
5. **Steerable, not just watchable.** The researcher can chat with the agent,
   reprioritize the idea queue, edit `program.md`, kill runs, and open a real
   terminal — all from a phone.
6. **Mobile is a first-class client.** The iPhone view is not an afterthought;
   a researcher should be able to run their lab from a coffee shop.
7. **Fail loud, by email.** If a run crashes, the agent gets stuck, or a GPU
   goes idle, the researcher hears about it on their chosen cadence.
