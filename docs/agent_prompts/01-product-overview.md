# 01 — Product Overview

## 1.1 The problem

Autonomous research agents work. Karpathy's `zero_order_diffusion_autoresearcher`
demonstrated the loop: hand an AI agent a small-but-real training setup, let it
edit `train.py`, run a time-boxed experiment, keep-or-discard, repeat — and wake
up to a log of progress. Solo researchers are already doing this in production:
they rent a GPU node, hand-craft an enormous metaprompt (see the two examples in
the project brief), paste it into Claude Code with `--dangerously-skip-permissions`,
and let it run overnight.

But the experience is **raw**. Today the researcher has to:

- SSH into the box and babysit tmux by hand.
- Re-paste the same wall of setup text for every new node and every new project.
- Read `results.tsv` and `ideas.md` over SSH to know what happened.
- Hand-roll Weights & Biases logging, or do without plots entirely.
- Have no idea a run crashed until they check.
- Have no way to glance at progress from their phone.

There is no cockpit. The agent is flying the plane and the researcher is reading
the instruments through a keyhole.

## 1.2 The solution

autoresearcherUI is that cockpit. It is a self-hosted web application that
installs onto the GPU node alongside the research and gives the researcher:

- **A guided onboarding** that replaces the hand-pasted metaprompt with a form
  (and still supports one-shot bulk paste for power users setting up many nodes).
- **A live dashboard** of every experiment — done, running, and queued — sorted
  by expected value.
- **An open-source, private experiment tracker** — W&B-style live plots, but
  single-node and zero-ops.
- **Terminal and tmux access** to every job, in the browser.
- **A chat channel** to the Principal Researcher agent.
- **Email digests and alerts** on a cadence the researcher chooses.
- **A first-class mobile view** so the lab fits in a pocket.

The researcher's job shrinks to what it should be: define the *purpose*, seed
the *ideas*, and steer.

## 1.3 Goals

| Goal | Definition of done |
|------|--------------------|
| **G1. One-command node setup** | From a bare rented GPU box: `git clone` + `./setup.sh` + paste a Tailscale token → a working dashboard URL, in under 5 minutes. |
| **G2. Zero-SSH operation** | After setup, the researcher never needs SSH again. Terminals, logs, file edits, and chat are all in the UI. |
| **G3. Bulk onboarding** | All onboarding fields can be filled by pasting a single block of text, so setting up 10 nodes is not 10× the misery. |
| **G4. Autonomous research loop** | After **Start**, the agent creates the repo, writes the code, runs baselines, and loops on ideas with no human input required. |
| **G5. Live observability** | Every run streams metrics; the dashboard plots them in realtime against baseline and prior runs. |
| **G6. Full GPU utilization** | The scheduler keeps every GPU occupied; idle GPU time is surfaced as an alert. |
| **G7. Mobile parity** | Every core view (experiments, detail, graphs, chat, terminal) is usable on an iPhone. |
| **G8. Proactive comms** | The researcher gets emails — digests on a cadence, plus immediate alerts for crashes, stalls, and breakthroughs. |

## 1.4 Non-goals

- **Not a multi-tenant SaaS.** One node, one researcher. No accounts, no orgs,
  no billing. (The passcode is a lock on the door, not a user system.)
- **Not a hyperscale tracker.** It is explicitly *not* trying to be W&B or
  Neptune at OpenAI scale. It targets one researcher's handful of GPUs.
- **Not a multi-node cluster manager.** v1 manages exactly one node. Multi-node
  is a roadmap item (see [10-roadmap](./10-roadmap-and-milestones.md)), not v1.
- **Not a model-serving / inference platform.** It runs *research*, not
  production endpoints.
- **Not a replacement for the agent's judgment.** autoresearcherUI orchestrates
  and observes; the science is the agent's, governed by `program.md`.
- **Not opinionated about the research domain.** Diffusion, ARC-AGI, continual
  learning — the system is domain-agnostic; the domain lives in `program.md`,
  `train.py`, and the eval function.

## 1.5 Personas

**Primary — "The solo researcher" (Francois).** An experienced ML researcher
running independent research. Comfortable with the command line, GPUs, and
Claude Code. Rents compute on vast.ai / RunPod. Often runs *several* research
projects on *several* nodes at once. Wants to set up fast, then mostly watch and
steer from a laptop or phone. Cares about: not wasting GPU dollars, catching
crashes early, comparing runs cleanly, and not re-typing setup.

**Secondary — "The collaborator".** Someone the primary researcher shares a
dashboard URL with (read-mostly). They open the link, look at the plots and the
experiment table, maybe read a report. They authenticate with the same passcode
or none. They do not do onboarding.

**The agent — "The Principal Researcher".** Not a human, but a first-class
"user" of the system. It reads `program.md`, edits `train.py`, launches runs,
logs metrics via `arui`, and writes to `ideas.md`. The product is as much an
interface *for the agent to report through* as it is an interface for the human.

## 1.6 End-to-end user journey

The journey has three phases. Each later document expands one of them.

### Phase A — Node setup (CLI, ~5 min) → [doc 03](./03-installation-and-node-setup.md)

1. Researcher rents a GPU node on vast.ai or RunPod (e.g. 10× A40).
2. `git clone https://github.com/<user>/autoresearcherui && cd autoresearcherui`
3. `./setup.sh`
4. The script asks for the **Tailscale auth key** (and nothing else it can
   avoid — everything else is done in the browser).
5. The script installs dependencies, joins the tailnet, starts the backend,
   and prints a **dashboard URL** plus a **passcode**.

### Phase B — Onboarding & bootstrap (browser, ~3 min + agent work) → [doc 04](./04-onboarding-and-agent-bootstrap.md)

6. Researcher opens the URL on a phone or laptop, enters the passcode.
7. They see the **Onboarding form**. They either fill the fields, or paste one
   bulk block and let it auto-populate every field.
8. Fields include: their email, GitHub token/identity, the new repo name, the
   Claude token, Gemini/OpenAI tokens, the research **purpose**, **seed ideas**,
   the **eval function**, the **validation metric**, the **baseline methods**,
   the alert email cadence, the dangerously-skip-permissions checkbox, and the
   dashboard passcode.
9. Researcher hits **Start**. The dashboard switches to a live **Bootstrap**
   view.
10. Behind the scenes: the orchestrator writes a `.env`, creates the `claude`
    user, opens a tmux session, launches Claude Code, and feeds it the
    generated **setup prompt**. The agent creates the GitHub repo, writes
    `program.md` / `train.py` / `prepare.py` / `ideas.md`, and runs the
    baseline. The UI shows each file being written and each step completing.

### Phase C — Ongoing research (browser, indefinite) → [docs 05–07](./05-autoresearch-engine.md)

11. The dashboard's home becomes the **Experiments** view: a table of what was
    tried (succeeded / failed) and what is upcoming, rank-sorted by EV.
12. Live runs stream metrics into **realtime graphs** overlaid on baseline and
    prior runs.
13. The researcher clicks any row to open a full **experiment report**: config,
    diff of `train.py`, run logs, graphs vs. baseline, the agent's analysis.
14. They can open a **terminal** into any tmux session, **chat** with the
    Principal Researcher, edit `program.md`/`ideas.md`, and reprioritize ideas.
15. They get **emails** — digests on their cadence, alerts on crashes / stalls /
    breakthroughs.
16. The loop runs until the researcher stops it or the node is torn down.

## 1.7 Competitive positioning

| Tool | What it is | Why autoresearcherUI is different |
|------|-----------|-----------------------------------|
| **Weights & Biases** | Hosted, hyperscale experiment tracking. | autoresearcherUI is self-hosted, single-node, free, and bundles the *agent orchestration*, not just logging. |
| **mlop** (`mlop-ai/mlop`) | Open-source, self-hostable MLOps tracker (Apache-2.0). | autoresearcherUI borrows mlop's KISS, high-throughput logging design but ships a lighter SQLite/Parquet tracker — and again adds orchestration. mlop's SDK API shape is intentionally mirrored so a researcher *could* swap to mlop. |
| **minfx** | Commercial, closed-source Neptune replacement. | Not open-source, so not forkable. autoresearcherUI takes inspiration (URL-encoded shareable UI state, fast canvas charts) but builds its own. |
| **Karpathy's `autoresearcher`** | The bare research loop: 3 files, one agent, run by hand over SSH. | autoresearcherUI *is* this loop, wrapped in a cockpit: UI, tracking, multi-GPU scheduling, email, mobile, chat. It is strictly a superset. |
| **AlphaEvolve / agentic research frameworks** | Heavyweight, often closed, infra-bound. | autoresearcherUI is deliberately small, self-hostable, and for one person. |

The honest one-liner: **"open-source W&B + an agent orchestrator + a phone-sized
cockpit, for a single researcher's GPU box."**

## 1.8 What success looks like

- A researcher sets up a new node and project in under 10 minutes, start to
  first baseline run.
- They never SSH into the box for the lifetime of the project.
- They can tell, from their phone, in 10 seconds, whether research is going
  well.
- No GPU sits idle for more than a couple of minutes without the researcher
  being alerted.
- Setting up the 5th node is as fast as the 1st, because of bulk paste.
