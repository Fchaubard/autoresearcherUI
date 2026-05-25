# 06 — Experiment Tracking (the private, open-source W&B)

The brief asks for *"logging like wandb but we use open source here … kind of
like an open-source W&B but not meant for high scale … just for private
researchers."* This document specifies that layer: the `arui` SDK the generated
`train.py` imports, the ingestion service, how metrics are stored, how they
stream to the dashboard live, and the reasoning behind not forking mlop or minfx
wholesale.

## 6.1 The mlop vs. minfx decision

The brief says *"We should fork one of these two famous repos for this …
whichever you think is best"* and links **mlop** and **minfx**.

**minfx** (`minfx.ai`) — a **closed-source, commercial** product: a Neptune
drop-in replacement, built on the Rust `egui` toolkit, sold on a usage-based
plan. There is no source repository to fork. It is therefore **not a viable
fork base**. Two of its design choices are still worth borrowing:
- It encodes **full UI state in the URL**, so any view is shareable by link.
- It renders charts on a **canvas at 60 fps**, prioritizing speed over CSS
  polish — the right call for a tracker.

**mlop** (`mlop-ai/mlop`) — **Apache-2.0, open source, self-hostable**. A clean
Python SDK (`mlop.init() / mlop.log() / mlop.finish()`), a "KISS, high stable
throughput" philosophy, and a strong realtime logger. Its weakness for *this*
product is the **server**: it lives in a separate repo and is a heavier deploy
(docker-compose, a columnar/streaming backend) built for throughput well beyond
one researcher's box.

**Decision — build `arui`, a native lightweight tracker, instead of forking
either:**

1. **Do not fork minfx** — impossible (closed source).
2. **Do not adopt mlop's full server** — it violates design principle #1
   ("one node, zero ops"). docker-compose + a columnar service is more
   operational surface than the target user wants.
3. **Build `arui`**, a tracker whose **Python SDK is intentionally
   API-compatible with `wandb` and `mlop`** (`init / log / finish /
   log_artifact / log_image`). Back it with **SQLite + per-run Parquet**, served
   by the dashboard's own uPlot charts.
4. **Borrow, with attribution:** mlop's append-only, batched, high-throughput
   ingestion design (its core good idea); minfx's URL-encoded shareable UI state
   and canvas-first charts.
5. **Keep the escape hatch open:** because the SDK shape matches mlop, a
   researcher who outgrows one node can point `train.py` at a real mlop server
   by changing one import / one env var. autoresearcherUI never locks them in.

The honest summary: the *valuable* part of "fork one of these" is the **SDK
ergonomics and the realtime ingestion design**, and those are small. The heavy
part — a hyperscale server — is exactly what this product should *not* carry.
`arui` is the minimum tracker that does the job for one researcher.

## 6.2 The `arui` SDK

`arui` is a small pip-installable package shipped inside the `autoresearcherui`
repo (`arui/`). The generated `train.py` imports it instead of `wandb`. The
agent is instructed to use it in the setup prompt ([doc 04](./04-onboarding-and-agent-bootstrap.md) §4.7).

### API surface (wandb/mlop-compatible)

```python
import arui

run = arui.init(
    project="bs1learning",        # the experiment repo name
    name="icl-cartridge-v2",      # == idea_id, the join key
    config={"lr": 1e-4, "n_pert": 100, "batch_size": 1024},
    tags=["idea", "baseline"],    # optional
    notes="In-context learning with cartridge compression",
)

for step in range(num_steps):
    ...
    arui.log({"val_fid": 1.23, "train_loss": 0.45}, step=step)

arui.log_image("samples", img_array, step=step)     # plots/visuals
arui.log_artifact("checkpoint", "ckpt/model.pt")    # files
arui.summary["best_val_fid"] = 0.99                 # headline scalars

arui.finish()                                       # flush + close
```

It must also support being a **drop-in for `wandb`**: `import arui as wandb`
should work for the common surface, so existing training scripts need near-zero
changes.

### How the SDK talks to the backend

- On `init()`, the SDK reads `ARUI_INGEST_URL` (default
  `http://127.0.0.1:<port>`) and `ARUI_TRACKING_INGEST_TOKEN` from the
  environment (the orchestrator injects both when it launches a run). It
  registers the run and gets a `run_id`.
- `log()` calls are **non-blocking**: points go into an in-process queue; a
  background thread **batches** them (by count or by a short flush interval) and
  POSTs them to the ingest endpoint. This is the mlop-style high-throughput
  design — training is never slowed by logging, and a slow/restarting backend
  cannot stall `train.py`.
- If the backend is briefly unreachable, the SDK buffers to a local
  write-ahead file and replays on reconnect, so no metrics are lost across a
  backend restart.
- `finish()` flushes the queue and posts a final summary.

The SDK has **zero hard dependency on the backend being up** — a run started
before the dashboard is reachable still works and backfills.

## 6.3 The ingestion service

`tracking/` in the backend. Endpoints are detailed in
[doc 08](./08-api-and-data-models.md); behavior:

- `POST /api/track/run` — register a run; returns `run_id`. Links the run to its
  `idea` by matching `name` → `idea_id`.
- `POST /api/track/log` — a batch of metric points
  `[{key, step, value, timestamp}, ...]`. Authenticated by
  `ARUI_TRACKING_INGEST_TOKEN`.
- `POST /api/track/artifact` — multipart upload of a plot/file/checkpoint.
- `POST /api/track/finish` — finalize + summary.

On each `log` batch the service:
1. **Appends** the points to the run's Parquet file and updates the run's
   `last_step`/`last_value` summary row in SQLite.
2. **Publishes** the points to the `metrics` WebSocket hub, keyed by `run_id`,
   so every dashboard with that chart open updates immediately.

Ingestion is append-only and idempotent on `(run_id, key, step)` so a retried
batch never double-counts.

## 6.4 Metric storage — Parquet + DuckDB

> **v0.2 ([doc 11 D2](./11-refinements-v0.2.md)):** the v0.1 two-tier design (a
> SQLite `metric_point` table *plus* Parquet) was simplified after external
> review. **All metrics now live only in Parquet, queried by an embedded
> DuckDB.** There is no `metric_point` table.

**Metrics → per-run Parquet (`data/metrics/<run_id>.parquet`).**
*Every* logged value — low-rate `val_*` and high-rate per-step/per-perturbation
streams alike — is appended to the run's Parquet file by the ingest service.
Columns: `key, step, value, wall_time`. One file per run = trivially portable
and deletable.

**Queries → embedded DuckDB.**
The backend embeds **DuckDB**. Reading metrics — a single series, a multi-run
overlay, a downsampled range — is one SQL query directly over the Parquet
file(s): `SELECT step, value FROM 'data/metrics/<id>.parquet' WHERE key=…`.
DuckDB does decimation, min/max bucketing, and joins server-side, so the browser
never receives more than a few thousand points. No database service to operate.

**SQLite keeps only relational metadata** — `project`, `idea`, `run` (incl. the
run summary: headline metric, peak VRAM, status, config), `artifact`, `event`,
`chat_message`, `gpu`, `tmux_session`, `setting`. Small, joinable, WAL mode.

**Artifacts (`data/artifacts/<run_id>/`).**
Plots, sample images, files, checkpoints — referenced by an `artifact` row.

**Retention.** A configurable cap (`ARUI_METRIC_RETENTION`, default: keep all
metadata forever, keep raw Parquet/artifacts for the last `K` runs or `G` GB).
When the cap is hit, the oldest discarded runs' raw files are pruned first;
their summaries stay so the experiments table never loses history.

**Why not ClickHouse / a TSDB.** This is the explicit divergence from mlop's
server. One researcher with ≤16 GPUs and hour-long runs generates metrics that
Parquet + SQLite handle comfortably. Adding a columnar database service would
add an ops burden the product exists to avoid. The Parquet layout means that if
scale ever demands it, the migration target (DuckDB, ClickHouse, a real mlop
server) is straightforward — Parquet is already the right on-disk shape.

## 6.5 Realtime streaming to the dashboard

The W&B-like "graphs updating live" experience is the `metrics` **SSE** stream
([doc 11 D1](./11-refinements-v0.2.md)):

- A chart opens an `EventSource` on `/api/stream/metrics?run_id=…&keys=…`.
- It first fetches a **downsampled history** via REST (`/api/runs/{id}/metrics`,
  DuckDB-decimated to ~2–4k points per series so first paint is instant).
- The SSE stream then pushes **each new point** as it is ingested; `EventSource`
  auto-reconnects with `Last-Event-ID` if the connection blips.
- The frontend feeds points into **uPlot**, which redraws on a canvas — smooth
  at high update rates and large point counts.
- Multiple series overlay on one chart: the **current run**, the **baseline
  run**, and any **prior runs** the researcher pins for comparison — exactly the
  W&B overlay the brief asks for.

Downsampling uses a min/max-preserving decimation (LTTB-style) so spikes and
divergence are never hidden by decimation.

## 6.6 What gets tracked per run

| Category | Examples | Source |
|----------|----------|--------|
| Scalars | `val_fid`, `train_loss`, perplexity, F1, reward, peak VRAM, MFU | `arui.log()` |
| Summary | best metric, final metric, total steps, total images, params (M) | `arui.summary` + `run.log` parse |
| Config / HPPs | lr, eps, batch size, n_pert, depth, T schedule, solver | `arui.init(config=…)` |
| Images / plots | sample grids, loss-landscape plots, attention maps | `arui.log_image()` |
| Files | checkpoints, generated artifacts | `arui.log_artifact()` |
| System | per-GPU utilization, VRAM, temperature over the run | scheduler (`pynvml`) |
| Provenance | git commit, branch, `train.py` diff vs. baseline, tmux session | orchestrator |

The system metrics are captured by the scheduler independently of `arui`, so
even a run that logs nothing still gets a GPU-utilization trace — which is what
makes the "no wasteful runs" guarantee visible.

## 6.7 Comparison & the experiment report

Tracking data feeds two dashboard surfaces ([doc 07](./07-dashboard-ui.md)):

- **Live graphs** on the home view — current runs overlaid on baseline + pinned
  priors, updating in realtime.
- **The experiment report** — opened by clicking a row in the experiments table.
  A full per-run page: the idea block (description, EV, why), config/HPPs, the
  `train.py` diff vs. baseline, the full metric charts, logged images, run logs,
  the headline result vs. baseline, and the agent's analysis and conclusion
  pulled from the idea block.

## 6.8 Shareability (borrowed from minfx)

Every dashboard view encodes its state — selected runs, visible metric keys,
zoom range, comparison set — into the **URL query string**. A researcher can
copy the address bar and send a collaborator a link that opens the *exact* same
chart selection. No server-side "saved view" object is needed; the URL is the
saved view. This is minfx's good idea, and it is free here.

## 6.9 Relationship to `results.tsv` and `wandb link`

The reference `program.md` has the agent write a `wandb link` into each idea
block and log to `results.tsv`. Under autoresearcherUI:

- "wandb link" becomes the **`arui` run URL** — a deep link into the dashboard's
  report for that run. The orchestrator can pre-create the run and hand the
  agent the URL so the idea block links somewhere real.
- `results.tsv` is still written by the agent and is still the authoritative
  keep/discard ledger; the orchestrator parses it but never overwrites it.

So nothing in the reference workflow breaks — `arui` simply *is* the "wandb"
the agent's `program.md` refers to, and the link resolves inside this product.
