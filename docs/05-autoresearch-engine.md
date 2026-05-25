# 05 — The Autoresearch Engine

This document specifies **Phase C**'s engine: the autonomous research loop, how
the orchestrator and scheduler keep it running, how training jobs map to tmux
and GPUs, and how `program.md` / `ideas.md` / `results.tsv` are kept in sync
with the dashboard.

## 5.1 Two halves: the agent's loop and the orchestrator's loop

The engine has two cooperating control loops.

**The agent's loop** lives in `program.md` and is run by the Principal
Researcher (Claude Code). It is *unchanged in spirit* from Karpathy's reference
`program.md`: review `ideas.md`, pick the highest-confidence unstarted idea,
implement it in `train.py`, commit, run, read results, analyze, update
`ideas.md`, keep-or-revert, repeat — forever. autoresearcherUI does **not**
replace this loop; the agent still owns the science.

**The orchestrator's loop** lives in the backend. It is the new part. It does
not do science — it does *operations*: it watches the agent and the experiment
repo, schedules runs onto GPUs, ensures no GPU is idle, captures metrics and
logs, builds reports, and surfaces everything to the dashboard and to email.

The contract between them is the **experiment repo on disk**: `program.md`,
`ideas.md`, `results.tsv`, `train.py`, and the `arui` log stream. The agent
writes those files; the orchestrator reads them (and, when the researcher asks,
edits `program.md`/`ideas.md` and tells the agent to re-read).

## 5.2 The research loop, end to end

```
        ┌──────────────────────────────────────────────────────────┐
        │  ideas.md  (the backlog — idea blocks, each with EV)      │
        └───────────────┬──────────────────────────────────────────┘
                        │  scheduler picks top-EV unstarted idea
                        ▼
        ┌──────────────────────────────────────────────────────────┐
        │  agent implements the idea by editing train.py, commits  │
        └───────────────┬──────────────────────────────────────────┘
                        │  scheduler launches on a free GPU
                        ▼
        ┌──────────────────────────────────────────────────────────┐
        │  tmux session train-gpuN:  uv run train.py  (1 hr budget) │
        │  → streams metrics via arui, logs to run.log              │
        └───────────────┬──────────────────────────────────────────┘
                        │  run exits
                        ▼
        ┌──────────────────────────────────────────────────────────┐
        │  agent reads results.tsv + run.log, writes analysis into  │
        │  the idea block, sets status 🟢/🔴/🟣, keeps or reverts   │
        │  the commit, and appends any NEW idea blocks              │
        └───────────────┬──────────────────────────────────────────┘
                        │  orchestrator parses the diff of ideas.md
                        ▼
        ┌──────────────────────────────────────────────────────────┐
        │  DB updated · report built · dashboard refreshed · maybe  │
        │  an email alert · queue re-ranked by EV                   │
        └───────────────┬──────────────────────────────────────────┘
                        │  loop
                        └──────────────► back to top
```

The time budget per run is defined by `program.md` (1 hour in the brief's
examples; the reference repo used 5 minutes). The orchestrator treats the budget
as a fact it reads from `program.md`, not something it sets.

## 5.3 `program.md`, `ideas.md`, `results.tsv` — the shared contract

The orchestrator must parse these reliably. It does so leniently and never
fights the agent for ownership.

### `ideas.md` — parsed into `idea` rows

`program.md` defines the idea-block template. The orchestrator's `repo/` module
parses `ideas.md` into structured `idea` records ([doc 08](./08-api-and-data-models.md)):

| Idea-block field | DB column | Use |
|------------------|-----------|-----|
| `idea_id` (the wandb/arui run name) | `idea_id` | The join key to runs and arui logs. |
| `Description` | `description` | Shown in the table and report. |
| `EV Improvement` | `ev` | The **sort key** for the upcoming queue (descending). |
| `Why` | `why` | Rationale, shown in the report. |
| `Status` (⚪🔵🟡🔴🟢🟣) | `status` | Drives table grouping (done/failed/running/upcoming). |
| `Time of idea generation` | `created_at` | Ordering within `ideas.md`. |
| `HPPs` | `hpps` (json/text) | The config used; shown in the report. |
| `Time of run start/end` | `started_at`,`ended_at` | Timeline. |
| `Results vs. Baseline` | `results_vs_baseline` | The headline result. |
| `wandb link` | `tracking_url` | Deep link to the run's live charts. |
| `Analysis` | `analysis` | The agent's reasoning trace. |
| `Conclusion` | `conclusion` | Final verdict. |
| `Next Ideas to Try` | `next_ideas` | Spawns new idea blocks. |

Parsing is **status-tolerant**: the six status emojis map to a normalized enum
(`not_implemented`, `implemented`, `running`, `failed`, `success`, `unclear`).
If a block is malformed, the orchestrator keeps the last good parse for that
`idea_id` and raises a low-severity event rather than crashing.

The orchestrator **re-parses `ideas.md` on every change** (filesystem watch +
git commit hook), so the dashboard's queue reflects the agent's latest thinking
within seconds.

### `results.tsv` — the keep/discard ledger

The 5-column TSV (`commit`, `val_metric`, `memory_gb`, `status`, `description`)
is parsed into the `run` table's outcome fields. The orchestrator treats
`results.tsv` as authoritative for keep/discard status and the headline metric.

### `program.md` — read-mostly

`program.md` is the human's file. The orchestrator reads it to learn the time
budget, the metric, and the loop rules, and renders it (read-only by default) in
the dashboard. The researcher *can* edit it through the UI (§5.8); when they do,
the orchestrator writes the file and notifies the agent.

## 5.4 The GPU scheduler

The scheduler (`scheduler/`) exists to satisfy design principle #3 — **never
waste a GPU** — and the brief's rule: *"The job should ALWAYS keep all GPUs in
use. No wasteful runs."*

### State it maintains

For each GPU index `0..N-1`: model, total VRAM, current `pynvml` utilization and
VRAM used, the process list, and the `run_id` the orchestrator believes owns it
(or `null`).

### The dispatch loop (every ~5 s)

1. Poll `pynvml` for live utilization/VRAM/processes on every GPU.
2. Reconcile: mark GPUs whose owning run has exited as **free**.
3. For each **free** GPU:
   a. Ask the agent (or read from a prepared queue) for the **highest-EV
      unstarted idea** whose `train.py` change is committed and ready.
   b. If one is ready, launch it on that GPU (§5.5).
   c. If none is ready, signal the agent ("GPU `k` is free — implement the next
      idea") and record the GPU as **idle-waiting**.
4. Detect **idle waste**: a GPU at <`IDLE_UTIL_THRESHOLD` (default 5%)
   utilization for longer than `IDLE_GRACE` (default 5 min) raises an
   `gpu_idle` event → email alert ([doc 09](./09-notifications-and-security.md)).
5. Detect **stuck runs**: a run past the `program.md` time budget +10 min is
   killed, marked `crash`/`discard`, and its idea block flagged for the agent.

### Concurrency policy

- Default: **one run per GPU** (the brief: *"Generally have 1 run per GPU is a
  good idea"*). `CUDA_VISIBLE_DEVICES` pins each job.
- The scheduler does not split a GPU across runs in v1. (Multi-run-per-GPU and
  multi-GPU runs are roadmap items, [doc 10](./10-roadmap-and-milestones.md).)
- VRAM is a *soft* constraint per `program.md`; the scheduler reports peak VRAM
  per run but does not pre-empt on VRAM alone.

### Keeping the agent ahead of the GPUs

To avoid GPUs waiting on the agent to write code, the scheduler keeps the agent
**pipelined**: while N runs execute, the agent is asked to have the next
1–2 highest-EV ideas implemented and committed so a freeing GPU has work
instantly. This is surfaced in the UI as the "ready to launch" portion of the
queue.

## 5.5 The tmux job model

Every long-lived process is a named tmux session, so it survives a backend
restart and is directly inspectable. Naming convention:

| Session name | Contents | Lifetime |
|--------------|----------|----------|
| `agent` | The Principal Researcher (Claude Code) | The whole project |
| `train-gpu{N}` | One `uv run train.py` on GPU N | One run |
| `term-{uuid}` | An ad-hoc researcher shell (opened from the UI) | Until closed |
| `autoresearcherui-server` | The backend itself (only in the non-systemd fallback) | The whole project |

A training job is launched as:

```bash
tmux new-session -d -s train-gpu3 -c /home/claude/experiments/<repo>
tmux send-keys -t train-gpu3 \
  'CUDA_VISIBLE_DEVICES=3 ARUI_RUN_NAME=<idea_id> uv run train.py > run.log 2>&1' Enter
```

The orchestrator records the session in the `tmux_session` and `run` tables,
captures the pane for live log streaming, and detects completion by watching the
process exit and the appearance of the summary block in `run.log` (the
`val_fid:`/`val_metric:` line described in `program.md`'s output format).

Because each run is a real tmux session, the dashboard's **Terminals** view
([doc 07](./07-dashboard-ui.md) §7.7) can attach to any of them — exactly the
brief's "able to tmux into sessions and see what's going on".

## 5.6 Run lifecycle & status model

A `run` moves through:

```
queued ─► launching ─► running ─► finishing ─► (kept | discarded | crashed)
```

| Status | Meaning | Set by |
|--------|---------|--------|
| `queued` | Idea picked, code not yet committed/launched | scheduler |
| `launching` | tmux session created, process starting | orchestrator |
| `running` | `train.py` executing, metrics streaming | orchestrator |
| `finishing` | Process exited, results being parsed | orchestrator |
| `kept` | `results.tsv` status `keep` — improved the metric | `results.tsv` |
| `discarded` | `results.tsv` status `discard` — no improvement | `results.tsv` |
| `crashed` | `results.tsv` status `crash`, or killed for timeout/OOM | orchestrator |

Each transition emits an `events` WebSocket message and may trigger an email.
The `run` row carries the headline metric, peak VRAM, git commit, the `train.py`
diff, the tmux session name, and a link to its `arui` metric stream.

## 5.7 Expected Value (EV) and the upcoming queue

The brief wants the upcoming experiments **"rank sorted by EV descending"**.

- EV is authored by the agent in each idea block (`EV Improvement` /
  `Confidence`), per `program.md`'s template: roughly `confidence (0–1) ×
  expected metric improvement`.
- The orchestrator parses it into the `idea.ev` column and **sorts the upcoming
  queue by `ev` descending**. Ties break by `created_at` ascending.
- The scheduler dispatches in that order.
- The researcher can **manually override** ordering by dragging rows in the UI
  (§5.8); a manual rank takes precedence over EV and is written back into
  `ideas.md` as a `Priority: pinned` annotation the agent respects.

## 5.8 Researcher steering — how the human stays in control

The autoresearcher runs autonomously, but the brief is clear the human must be
able to steer. The orchestrator exposes these levers, all from the UI:

| Lever | Effect |
|-------|--------|
| **Reorder the idea queue** | Drag rows; pins a manual priority into `ideas.md`. |
| **Add an idea** | Write a new idea block; the agent picks it up on the next loop. |
| **Pause / resume an idea or the whole loop** | Scheduler stops dispatching; running jobs finish or can be killed. |
| **Kill a run** | The orchestrator kills the tmux session; run → `crashed`. |
| **Edit `program.md`** | Changes the loop rules; the agent is told to re-read. |
| **Edit `ideas.md`** | Manual edits merge with the agent's; conflicts surface as events. |
| **Chat with the agent** | Send a message into the agent's tmux session (§5.9). |
| **Edit research config** | Update purpose/eval/etc. via Settings; agent re-reads. |

All steering actions are also recorded as timeline `events` so the history of
who-did-what (human vs. agent) is auditable.

## 5.9 Talking to the Principal Researcher

The brief: *"Ability to talk to the researcher in charge or see the Claude Code
session."* Two mechanisms, both backed by the one `agent` tmux session:

1. **Chat panel** — a dashboard chat view. A researcher message is injected into
   the agent's tmux pane; the agent's textual responses are parsed back out of
   the pane and shown as chat bubbles. Messages persist in the `chat_message`
   table. This is the friendly, mobile-first way to ask "how's it going?" or
   "stop chasing idea 4, try the JEPA one next".
2. **Raw session view** — full xterm.js attachment to the `agent` tmux session
   for power users who want to see (and type into) the Claude Code session
   exactly as it is. Identical to `tmux attach -t agent` over SSH, in the
   browser.

## 5.10 Resilience

- **Backend restart:** tmux sessions (agent + runs) survive. On boot the
  orchestrator lists tmux sessions, reconciles them against the `run` and
  `tmux_session` tables, re-attaches log capture, and resumes the scheduler.
- **Agent crash / exit:** detected by the agent pane going dead. The
  orchestrator raises a high-severity `agent_down` event, emails an alert, and
  attempts a bounded number of restarts (re-launch Claude Code, re-issue a
  "resume from `program.md`" prompt). Running training jobs are unaffected.
- **Run crash / OOM:** caught via process exit + `run.log` stack trace; recorded
  as `crashed`; the GPU is freed immediately so it is not wasted.
- **Node reboot:** the systemd unit restarts the backend; tmux does not survive
  a reboot, so the orchestrator detects no `agent` session and re-bootstraps the
  loop from the existing experiment repo (the repo, DB, and `.env` are on disk).
- **Disk pressure:** old per-run Parquet/artifact files are the largest growth;
  a retention setting ([doc 06](./06-experiment-tracking.md)) caps them.
