# 10 — Roadmap & Milestones

This document sequences the build. The ordering principle: get the **autonomous
loop visible end-to-end** as early as possible, then layer on observability,
then polish and steering. Each milestone is independently demoable.

## 10.1 MVP definition

The MVP is the smallest thing that delivers the core promise: *rent a box,
clone, one script, fill a form, and autonomous research runs while you watch
live.* Concretely, the MVP must:

- Install via `git clone` + `./setup.sh` with only a Tailscale key (Phase A).
- Serve a passcode-gated dashboard over the tailnet.
- Run the browser onboarding form, including **bulk paste** (Phase B).
- Bootstrap the agent: create the GitHub repo, write `program.md` / `train.py` /
  `prepare.py` / `ideas.md`, run the baseline.
- Run the autonomous loop with the GPU scheduler keeping every GPU busy.
- Show the EV-sorted Experiments table and clickable Experiment Reports.
- Stream live metric graphs via `arui` + uPlot, overlaid on baseline.
- Give terminal access to every tmux session and a chat to the agent.
- Send email digests + crash/idle alerts on a cadence.
- Work on an iPhone for the core views.

Anything past that line is post-MVP.

## 10.2 Milestones

### M0 — Skeleton & contracts (foundation)

- Repo layout ([doc 02](./02-architecture.md) §2.5), `pyproject.toml`,
  `package.json`, `uv`/Vite toolchains.
- SQLite schema + Alembic migrations ([doc 08](./08-api-and-data-models.md) §8.1).
- FastAPI app shell, static-serving, the passcode/session auth.
- React app shell: routing, the global shell, the WS client wrapper.
- Pydantic → TypeScript type generation wired up.
- **Demo:** an empty dashboard loads over the tailnet behind a passcode.

### M1 — Node setup (Phase A)

- `setup.sh`: preflight, deps, `uv`, Tailscale join, `claude` user, storage
  init, service start, the final URL+passcode print.
- systemd unit + the non-systemd tmux fallback + watchdog.
- Idempotency, `--yes`, `--uninstall`/`--purge`.
- **Demo:** bare rented node → working dashboard URL in <5 min.

### M2 — Onboarding & bootstrap (Phase B)

- The onboarding form, all fields, live validation, draft auto-save.
- **Bulk paste**: parse + "copy current config" round-trip.
- The `.env` / `data/config/` writer; passcode rule (default = repo name,
  blank = open).
- The bootstrap state machine + the agent setup-prompt template.
- `claude` user + `agent` tmux session launch; feed the setup prompt.
- The live Bootstrap stepper view.
- **Demo:** fill the form → watch the agent create the repo and write the files.

### M3 — Engine & scheduler (the loop)

- The orchestrator: tmux job model, run lifecycle, `train-gpu{N}` sessions.
- The GPU scheduler: `pynvml` polling, dispatch loop, idle/stuck detection,
  the "never idle" guarantee.
- `repo/` parsing: `ideas.md` → `idea` rows, `results.tsv` → run outcomes.
- Baseline run, then the autonomous loop running unattended.
- Backend-restart reconciliation.
- **Demo:** baseline + several idea experiments run themselves overnight.

### M4 — Experiment tracking (`arui` + live graphs)

- The `arui` SDK: wandb/mlop-compatible API, batched non-blocking logging, the
  write-ahead reconnect buffer.
- The `tracking/` ingest service; SQLite + per-run Parquet store.
- The `metrics` WebSocket hub + downsampling.
- uPlot live charts, baseline/prior overlays, URL-encoded view state.
- **Demo:** runs stream metrics; charts update live against baseline.

### M5 — Dashboard depth

- The EV-sorted Experiments table (running / upcoming / completed) with
  drag-to-reorder.
- The full Experiment Report (idea block, `train.py` diff, charts, logs,
  analysis).
- The Live Graphs workspace.
- The Files views for `program.md` / `ideas.md` (render + edit).
- **Demo:** click any experiment → a complete report; reorder the queue.

### M6 — Terminals & agent chat

- The PTY ↔ WebSocket bridge + xterm.js; the session list; "new terminal".
- The agent chat panel + the raw `agent` terminal view; quick-action chips.
- **Demo:** open a training terminal and chat with the Principal Researcher
  from a phone.

### M7 — Notifications

- The `notify/` worker: digests on cadence, event-driven alerts, dedup.
- Server-side plot rendering for email attachments.
- SMTP + Resend adapters; the "send test email" flow.
- **Demo:** a digest with attached plots lands in the inbox; a crash alert fires.

### M8 — Mobile polish & hardening

- Audit every core view at iPhone width; bottom tab bar, sheets, touch drag.
- Performance pass (chart fps, first paint), reconnection robustness.
- Security pass ([doc 09](./09-notifications-and-security.md)): isolation,
  secret masking, rate limiting, Funnel warnings.
- **Demo:** run the whole lab from an iPhone; ship MVP.

The MVP ships at the end of M8. M0–M3 are the critical path; M4 can begin in
parallel once M3's run lifecycle exists.

## 10.3 Post-MVP / future

| Theme | Idea |
|-------|------|
| **Multi-node** | One dashboard federating several GPU nodes; the schema already keeps `project` as a row to ease this. |
| **Multi-project per node** | Run more than one research project on one box. |
| **Multi-GPU runs** | Let a single run span several GPUs; relax the one-run-per-GPU scheduler rule. |
| **GPU sharing** | Pack small runs onto one GPU when VRAM allows. |
| **Provider integration** | Drive vast.ai / RunPod APIs to rent/start/stop nodes from the UI; auto-shutdown idle nodes to save money. |
| **mlop server escape hatch** | A documented one-line switch from `arui` to a real mlop server for researchers who outgrow one node. |
| **Cost tracking** | Surface $/hour and cumulative spend; alert on budget. |
| **Idea-quality assist** | Let a consultant model critique the agent's idea blocks and EV estimates. |
| **Run comparison reports** | Auto-generated A/B reports between any two runs. |
| **Plugin program.md library** | Shareable `program.md` templates per research domain. |
| **Snapshots** | Export a project (DB + metrics + repo pointer) as a portable archive. |

## 10.4 Open questions

These need a decision before or during the relevant milestone:

1. **Time budget source.** It is read from `program.md`. Should the UI also let
   the researcher set/override it directly, writing back into `program.md`?
   *(Leaning: yes, as a Settings field that edits `program.md`.)*
2. **Agent re-prompting on config change.** When the researcher edits `purpose`
   or `program.md`, should the agent be interrupted to re-read immediately, or
   only at the next loop boundary? *(Leaning: next loop boundary, with an
   "apply now" option.)*
3. **Chat ↔ tmux parsing reliability.** Parsing the agent's replies out of a
   tmux pane is inherently fuzzy. Is a more structured agent integration (e.g.
   the agent writing replies to a known file, or an SDK-based agent harness)
   worth it for M6? *(Revisit at M6; the tmux approach is the MVP path.)*
4. **Email plot rendering.** Server-side chart→PNG needs a headless renderer.
   Use a lightweight matplotlib path, or headless-browser screenshots of the
   uPlot charts? *(Leaning: matplotlib for digests — fewer moving parts.)*
5. **Consultant invocation.** Do Gemini/OpenAI get called by the agent itself
   (it has the keys) or via a backend service the agent calls? *(Leaning: the
   agent calls them directly; simpler, matches the reference workflow.)*
6. **Multiple baselines.** The brief's baseline-methods field can name several
   methods. Is the "baseline" a single run or the best of several? *(Leaning:
   the first unmodified run is *the* baseline for deltas; other baseline methods
   are normal runs tagged `baseline`.)*

## 10.5 Out of scope (restating non-goals)

Per [doc 01](./01-product-overview.md) §1.4, the following are explicitly **not**
on this roadmap as v1 work: multi-tenant SaaS, hyperscale tracking, model
serving, and any cloud control plane. autoresearcherUI stays a self-hosted,
single-researcher, single-node cockpit — and the roadmap above keeps it that
way.
