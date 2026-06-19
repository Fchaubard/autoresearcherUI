# 02 — Architecture

## 2.1 Overview

autoresearcherUI runs entirely on **one GPU node**. There is no external
control plane, no cloud dependency beyond the model APIs the agent calls and the
researcher's email relay. Everything below — backend, frontend, tracking, the
agent, the training jobs — lives on that single box.

```
┌──────────────────────────── GPU NODE (vast.ai / RunPod) ──────────────────────────┐
│                                                                                    │
│  ┌────────────────────────────────────────────────────────────────────────────┐   │
│  │                    autoresearcherui  (the product repo)                     │   │
│  │                                                                             │   │
│  │   ┌─────────────────────────┐         ┌──────────────────────────────────┐  │   │
│  │   │   Backend (FastAPI)     │         │   Frontend (React, static build) │  │   │
│  │   │                         │  serves │                                  │  │   │
│  │   │  • REST API  /api/*     │ ──────► │  • Dashboard SPA                 │  │   │
│  │   │  • WebSockets /ws/*     │         │  • Onboarding / Bootstrap        │  │   │
│  │   │  • Tracking ingest      │         │  • xterm.js terminals            │  │   │
│  │   │  • Orchestrator         │         │  • uPlot live charts             │  │   │
│  │   │  • GPU scheduler        │         └──────────────────────────────────┘  │   │
│  │   │  • Email worker         │                                               │   │
│  │   │  • Auth (passcode)      │         ┌──────────────────────────────────┐  │   │
│  │   └──────────┬──────────────┘         │   SQLite (autoresearch.db, WAL)  │  │   │
│  │              │                        │   + per-run Parquet metric files │  │   │
│  │              │                        │   + artifacts/ (plots, files)    │  │   │
│  │              │                        └──────────────────────────────────┘  │   │
│  │              │ libtmux / PTY                                                 │   │
│  │              ▼                                                               │   │
│  │   ┌────────────────────── tmux server ──────────────────────────────────┐   │   │
│  │   │                                                                      │   │   │
│  │   │  session: agent          → Claude Code "Principal Researcher"        │   │   │
│  │   │                            (runs as unix user `claude`)              │   │   │
│  │   │  session: train-gpu0     → uv run train.py  (GPU 0)                   │   │   │
│  │   │  session: train-gpu1     → uv run train.py  (GPU 1)                   │   │   │
│  │   │  session: train-gpuN     → ...                                       │   │   │
│  │   │  session: term-<uuid>    → ad-hoc researcher shells                   │   │   │
│  │   └──────────────────────────────────────────────────────────────────────┘   │   │
│  │                                                                             │   │
│  │   ┌──────────────────── experiment repo (cloned by agent) ──────────────┐   │   │
│  │   │  program.md  train.py  prepare.py  ideas.md  results.tsv  *.toml     │   │   │
│  │   │  + arui logging calls inside train.py                               │   │   │
│  │   └──────────────────────────────────────────────────────────────────────┘   │   │
│  └────────────────────────────────────────────────────────────────────────────┘   │
│                                                                                    │
│  Tailscale daemon ── joins the researcher's tailnet ── exposes the dashboard port  │
└────────────────────────────────────────────────────────────────────────────────────┘
        │                                   │                         │
        ▼                                   ▼                         ▼
  Researcher's laptop              Researcher's iPhone          model APIs (Claude,
  (over tailnet)                   (over tailnet)               Gemini, OpenAI) + SMTP
```

## 2.2 Technology stack

The stack is chosen for **one constraint above all: a single researcher must be
able to run, debug, and trust this on one box.** That rules out anything that
needs a cluster, a broker, or a heavyweight datastore.

### Backend

| Concern | Choice | Why |
|---------|--------|-----|
| Language | **Python 3.11+** | The whole ML/agent world is Python; the orchestrator must speak the same language as `train.py`, `pynvml`, etc. |
| Web framework | **FastAPI** + **Uvicorn** | First-class async, native WebSockets, Pydantic models, OpenAPI for free. |
| Data validation | **Pydantic v2** | Shared request/response/config models; `.env` parsing. |
| Database | **SQLite** (WAL mode) via **SQLAlchemy 2.x** | Zero-ops, single file, survives restarts. Correct weight class for one node. **Alembic** for migrations. |
| Metric store | **per-run Parquet**, queried by an embedded **DuckDB** (see [doc 06](./06-experiment-tracking.md), [doc 11 D2](./11-refinements-v0.2.md)) | Parquet *is* the metric database; DuckDB does range scans / downsampling / multi-run overlays as plain SQL over Parquet. No metric table, no extra service. |
| tmux control | **libtmux** | Create/list/kill sessions, send keys, capture panes from Python. |
| Browser terminals | **`ttyd`** (one per tmux session) — [doc 11 D5](./11-refinements-v0.2.md) | A tiny, battle-tested binary that serves a full xterm.js terminal over WS; deletes the custom PTY bridge. `ttyd -W tmux attach -t <session>`. |
| Realtime out | **Server-Sent Events** (one-way: metrics, logs, events, GPU, chat output) — [doc 11 D1](./11-refinements-v0.2.md) | Plain HTTP; `EventSource` auto-reconnects with `Last-Event-ID`; no second protocol. WebSocket is used **only** for the terminal. |
| GPU telemetry | **`pynvml`** (nvidia-ml-py), `nvidia-smi` fallback | Per-GPU utilization, VRAM, process list — drives the scheduler and idle alerts. |
| Periodic jobs | **APScheduler** (in-process) | Email digests, GPU polling, idea-queue re-ranking. |
| Email | **`aiosmtplib`** (SMTP) with a **Resend** HTTP API adapter as an alternative | SMTP works with any relay incl. Gmail app passwords; Resend for those who prefer an API key. |
| Process mgmt of the server itself | **systemd** unit *or* a detached **tmux** session created by `setup.sh` | Survives SSH disconnect; auto-restart on crash. |
| Python env | **`uv`** | Matches the reference repo; fast, reproducible. |

### Frontend

| Concern | Choice | Why |
|---------|--------|-----|
| Framework | **React 18** + **TypeScript** | Mature, mobile-friendly, huge ecosystem. |
| Build | **Vite** | Fast builds; emits static assets FastAPI serves directly. |
| Styling | **TailwindCSS** + **shadcn/ui** (Radix primitives) | Responsive utility classes make laptop↔iPhone parity cheap; accessible components. |
| Data fetching | **TanStack Query** | Caching, refetch, optimistic updates over the REST API. |
| Realtime | **`EventSource`** (SSE) for streams; one **WebSocket** for the terminal | SSE auto-reconnects for free; the terminal is the only bidirectional channel. |
| Charts | **uPlot** (with a small React wrapper) + synced cursor/brush | Canvas-rendered, handles 100k+ points at 60fps — essential for W&B-style live overlays. `cursor.sync` links all charts ([doc 11 D10](./11-refinements-v0.2.md)). |
| Tables | **TanStack Table** + **TanStack Virtual** | All filtering/sorting/search client-side for zero-latency exploration ([doc 11 D3](./11-refinements-v0.2.md)); virtualization keeps big tables/logs smooth. |
| Motion | **framer-motion** | Springy route/tab transitions, animated reordering, count-ups, skeletons — the micro-interaction layer ([doc 11 D6](./11-refinements-v0.2.md)). |
| Onboarding tour | **react-joyride** / **onborda** | The dismissible first-run guided tour ([doc 11 D7](./11-refinements-v0.2.md)). |
| Terminal | embedded **`ttyd`** | Served by the backend per session; the dashboard frames it in a styled pane. |
| Markdown | **react-markdown** + **remark-gfm**, syntax highlight via **Shiki** | Renders `program.md`, `ideas.md`, agent reports. |
| Diffs | **react-diff-viewer-continued** | Shows per-experiment `train.py` diffs in reports. |
| Routing | **React Router** | Standard SPA routing; deep-linkable views. |
| State | React Query for server state + **Zustand** for thin UI state | Avoids Redux ceremony. |

### Networking / access

| Concern | Choice | Why |
|---------|--------|-----|
| Remote access | **Tailscale** | The node joins the researcher's tailnet; the dashboard is reachable at `http://<node>.<tailnet>.ts.net:<port>` from any of their devices. The only token `setup.sh` strictly needs. |
| HTTPS (optional) | **Tailscale Serve** | Terminates TLS with a tailnet cert so the dashboard is `https://`. |
| Public access (opt-in) | **Tailscale Funnel** | If the researcher explicitly wants a public URL (e.g. to share with a collaborator off-tailnet). Off by default. |
| Auth | **Passcode → signed session cookie** (HS256 JWT) | Lightweight gate; see [doc 09](./09-notifications-and-security.md). |

### The experiment-tracking decision (mlop vs. minfx)

The brief asks to "fork one of these two". The verdict, expanded in
[doc 06](./06-experiment-tracking.md):

- **minfx is closed-source and commercial** (a Neptune drop-in, built on Rust
  `egui`). It cannot be forked. Ruled out as a base — but two of its ideas are
  worth stealing: *URL-encoded shareable UI state* and *canvas charts at 60fps*.
- **mlop is Apache-2.0 and self-hostable**, with a clean `mlop.init / log /
  finish` SDK. But its **server** is a separate, heavier deployment
  (docker-compose, a columnar backend) aimed at higher throughput than one
  researcher needs.

**Decision:** do not fork either wholesale. Build a **native, lightweight
tracker** (`arui`) whose **Python SDK is API-compatible with `wandb`/`mlop`**
(`arui.init() / arui.log() / arui.finish() / arui.log_artifact()`), backed by
SQLite + Parquet, rendered by the dashboard's own uPlot charts. Borrow mlop's
KISS, append-only, high-throughput ingestion design. Because the SDK shape
matches mlop, a researcher who outgrows the single node can later point
`train.py` at a real mlop server with a one-line change.

## 2.3 Backend components

The backend is a single Uvicorn process. Internally it is organized into these
modules (all under `autoresearcherui/backend/`):

| Module | Responsibility |
|--------|----------------|
| `app/` | FastAPI app, routing, middleware, auth, static-file serving. |
| `orchestrator/` | Owns tmux. Creates the `claude` user, launches the Principal Researcher, starts/stops training jobs, opens terminal PTYs. The "hands" of the system. |
| `scheduler/` | The GPU scheduler. Polls `pynvml`, maintains the GPU↔run map, dispatches queued ideas onto free GPUs, raises idle alerts. Prime directive: no idle GPU. |
| `agent/` | The interface to the Principal Researcher: sends prompts/chat, tails its tmux pane, parses its progress, exposes the chat channel. |
| `tracking/` | The `arui` ingestion service: receives metric/artifact uploads, writes SQLite + Parquet, fans realtime points out to WebSocket subscribers. |
| `repo/` | Reads/writes the experiment repo: parses `ideas.md` into idea blocks, reads `results.tsv`, computes `train.py` diffs per commit, edits `program.md`/`ideas.md` on the researcher's behalf. |
| `notify/` | Email worker: composes and sends digests + alerts via SMTP/Resend, dedupes, respects cadence. |
| `models/` | SQLAlchemy ORM + Pydantic schemas + Alembic migrations. |
| `bootstrap/` | Builds the `.env`, generates the agent setup prompt, drives the Phase-B bootstrap state machine. |
| `ws/` | WebSocket hubs: metrics, logs, events, terminal, chat. |

These are modules, not services — they share one process and one event loop.
The only sub-processes are tmux, the agent, the training jobs, and terminal
PTYs.

## 2.4 Process & isolation model

- **The backend** runs as the researcher's normal node user (call it
  `researcher`), so it can manage tmux, read GPUs, and talk to GitHub.
- **The Principal Researcher agent** runs as a **dedicated unix user `claude`**,
  created by `setup.sh`. Reason: it runs `claude --dangerously-skip-permissions`,
  which can execute arbitrary commands; confining it to its own user limits the
  blast radius (it owns the experiment repo and its own home, not the backend's
  files or the `.env` secrets directory). See [doc 09](./09-notifications-and-security.md).
- **Each training job** runs in its own tmux session, pinned to one GPU via
  `CUDA_VISIBLE_DEVICES`. One run per GPU is the default.
- **The agent and training jobs share the experiment repo** on disk; the agent
  edits files, the scheduler launches `train.py`.
- If the backend process dies, **tmux sessions survive** — the agent and runs
  keep going. On restart the backend re-attaches by listing tmux sessions and
  reconciling against the DB.

## 2.5 Repository layout (`autoresearcherui`)

```
autoresearcherui/
├── setup.sh                     # the one-command node installer (doc 03)
├── README.md
├── docs/                        # this spec
├── pyproject.toml               # backend deps, managed by uv
├── uv.lock
├── .env.example                 # documents every variable (doc 03)
├── backend/
│   ├── app/                     # FastAPI app, routes, auth, static serving
│   ├── orchestrator/            # tmux, agent launch, job control
│   ├── scheduler/               # GPU scheduler
│   ├── agent/                   # Principal Researcher interface + chat
│   ├── tracking/                # arui ingestion service
│   ├── repo/                    # experiment-repo parsing & editing
│   ├── notify/                  # email worker
│   ├── bootstrap/               # .env build, setup-prompt gen, Phase-B FSM
│   ├── models/                  # SQLAlchemy + Pydantic + Alembic
│   ├── ws/                      # WebSocket hubs
│   └── main.py                  # Uvicorn entrypoint
├── arui/                        # the pip-installable tracking SDK (doc 06)
│   ├── __init__.py              # init() / log() / finish() / log_artifact()
│   └── ...
├── frontend/
│   ├── src/
│   │   ├── routes/              # Onboarding, Bootstrap, Experiments, Detail, ...
│   │   ├── components/          # charts, tables, terminal, chat, markdown
│   │   ├── lib/                 # api client, ws client, types
│   │   └── main.tsx
│   ├── index.html
│   ├── vite.config.ts
│   └── package.json
├── docs/
│   └── agent_prompts/           # design docs + agent prompt templates
│       ├── setup_prompt.md.j2       # Jinja template for the agent bootstrap prompt
│       └── program_template.md.j2   # template the agent adapts into program.md
└── data/                        # created at runtime (gitignored)
    ├── autoresearch.db          # SQLite
    ├── metrics/<run_id>.parquet # per-run metric streams
    ├── artifacts/<run_id>/      # plots, files, checkpoints
    └── secrets/.env             # 0600, owned by researcher (doc 09)
```

The `experiment repo` is cloned **outside** this tree (default `~/experiments/<repo-name>/`)
so the agent's repo and the product repo never collide.

## 2.6 Data flow — three core paths

**1. Metric path (run → dashboard).**
`train.py` calls `arui.log({"val_fid": 1.23}, step=n)` → the `arui` SDK batches
points and POSTs them to `tracking/` ingest → ingest appends to the run's
Parquet file and a SQLite summary row, then publishes the point to the
`metrics` WebSocket hub → every dashboard with that run's chart open redraws via
uPlot. Latency target: sub-second from `log()` to pixel.

**2. Control path (researcher → agent/job).**
Researcher edits the idea queue or sends a chat message in the UI → REST call →
`agent/` or `repo/` acts: re-ranks `ideas.md`, sends keys to the agent's tmux
pane, or signals the scheduler. The change is written to the DB and the
experiment repo, then broadcast on the `events` WebSocket so other open
dashboards stay in sync.

**3. Lifecycle path (idea → experiment → report).**
The scheduler sees a free GPU → pulls the top-EV unstarted idea → asks the agent
to implement it in `train.py` → on commit, launches `uv run train.py` in a fresh
tmux session pinned to that GPU → the run logs via `arui` → on exit, `repo/`
reads `results.tsv` + the agent's `ideas.md` update, the run's status flips, the
report is assembled, and `notify/` may email an alert.

## 2.7 Why not heavier infrastructure?

A reasonable reviewer will ask why there is no Postgres, no Redis, no Celery, no
Docker, no ClickHouse. The answer is the first design principle: **one node, one
researcher, zero ops.**

- **No Postgres** — SQLite in WAL mode handles one writer (the backend) and many
  readers fine at this scale. One file, trivially backed up.
- **No Redis / broker** — realtime fan-out is in-process via `asyncio` and
  WebSocket hubs. There is only one process to coordinate.
- **No Celery** — APScheduler covers the handful of periodic jobs in-process.
- **No Docker requirement** — the node is already a dedicated box; `setup.sh` +
  `uv` is simpler than a container runtime and avoids GPU-passthrough friction.
  (A Dockerfile *may* ship as a convenience, but it is not the default path.)
- **No ClickHouse** — Parquet files per run give columnar range scans without a
  service to operate. This is exactly the line where this product diverges from
  mlop's full server.

Every one of these would add an operational surface the target user does not
want. If the product ever needs to scale past one node, [doc 10](./10-roadmap-and-milestones.md)
covers that — but it is explicitly post-v1.
