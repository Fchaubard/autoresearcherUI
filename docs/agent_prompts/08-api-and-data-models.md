# 08 — API & Data Models

The implementation-ready interface contract: the SQLite schema and the REST +
WebSocket API. Names here are authoritative — other docs reference them.

## 8.1 Data model (SQLite)

One SQLite file, `data/autoresearch.db`, WAL mode, migrated with Alembic.
Conventions: `id` is a string ULID primary key; timestamps are UTC ISO-8601;
`json` columns are TEXT holding JSON. One node hosts one project in v1, but the
schema keeps `project` as a row so multi-project is a non-breaking later change.

### `project`

The research project created during onboarding.

| Column | Type | Notes |
|--------|------|-------|
| `id` | str PK | |
| `name` | str | The experiment repo name (`ARUI_NEW_REPO_NAME`). |
| `repo_url` | str | GitHub URL of the experiment repo. |
| `repo_path` | str | Local clone path. |
| `purpose` | text | From `data/config/purpose.md`. |
| `eval_spec` | text | From `data/config/eval_spec.md`. |
| `validation_metric` | str | `perplexity`/`f1`/`accuracy`/`rmse`/`mse`/`fid`/`bpb`/`reward`/`custom`. |
| `metric_name` | str | Display name (esp. for `custom`). |
| `metric_direction` | str | `minimize` \| `maximize`. |
| `time_budget_sec` | int | Per-run budget parsed from `program.md`. |
| `status` | str | `onboarding`/`bootstrapping`/`running`/`paused`/`stopped`. |
| `baseline_run_id` | str FK→run | Set after the first run completes. |
| `created_at` | ts | |

### `idea`

One idea block from `ideas.md`.

| Column | Type | Notes |
|--------|------|-------|
| `id` | str PK | |
| `project_id` | str FK | |
| `idea_id` | str | The agent's run name; join key to `run`. Unique per project. |
| `description` | text | |
| `why` | text | |
| `ev` | float | Expected value of improvement; the queue sort key. |
| `status` | str | `not_implemented`/`implemented`/`running`/`failed`/`success`/`unclear`. |
| `hpps` | json | Hyperparameters/config recorded by the agent. |
| `results_vs_baseline` | text | Headline result string. |
| `analysis` | text | Agent's reasoning trace. |
| `conclusion` | text | |
| `next_ideas` | text | |
| `tracking_url` | str | Deep link to the run's report (the "wandb link"). |
| `manual_priority` | int null | Set when the researcher pins ordering; overrides `ev`. |
| `source` | str | `seed` (from onboarding) \| `agent` \| `human`. |
| `idea_generated_at` | ts | `Time of idea generation` from the block. |
| `started_at` / `ended_at` | ts null | |
| `created_at` / `updated_at` | ts | |

### `run`

One execution of `train.py`.

| Column | Type | Notes |
|--------|------|-------|
| `id` | str PK | The `run_id` used by the `arui` SDK. |
| `project_id` | str FK | |
| `idea_id` | str FK→idea.id | |
| `run_name` | str | == `idea.idea_id`. |
| `status` | str | `queued`/`launching`/`running`/`finishing`/`kept`/`discarded`/`crashed`. |
| `is_baseline` | bool | |
| `gpu_index` | int null | The pinned GPU. |
| `tmux_session` | str | e.g. `train-gpu3`. |
| `git_commit` | str | Short hash of the `train.py` state. |
| `git_branch` | str | e.g. `autoresearch/<tag>`. |
| `config` | json | HPPs from `arui.init(config=…)`. |
| `headline_metric` | float null | Final/best validation metric. |
| `baseline_delta` | float null | Signed improvement vs. baseline. |
| `peak_vram_mb` | float null | |
| `summary` | json | Everything from the `run.log` summary block + `arui.summary`. |
| `log_path` | str | Path to `run.log`. |
| `diff` | text | `train.py` diff vs. baseline/prev-kept. |
| `started_at` / `ended_at` | ts null | |
| `created_at` | ts | |

### metrics — *not a SQLite table*

> **v0.2 ([doc 11 D2](./11-refinements-v0.2.md)):** there is **no `metric_point`
> table.** All metrics are appended to per-run Parquet files
> (`data/metrics/<run_id>.parquet`, columns `key, step, value, wall_time`) and
> queried by an embedded **DuckDB**. Ingestion is idempotent on
> `(run_id, key, step)`. See [doc 06 §6.4](./06-experiment-tracking.md).

### `artifact`

| Column | Type | Notes |
|--------|------|-------|
| `id` | str PK | |
| `run_id` | str FK | |
| `kind` | str | `image`/`plot`/`file`/`checkpoint`. |
| `name` | str | |
| `path` | str | Under `data/artifacts/<run_id>/`. |
| `step` | int null | |
| `meta` | json | Dimensions, size, etc. |
| `created_at` | ts | |

### `gpu`

Snapshot rows refreshed by the scheduler (latest-per-index is the live state;
history is kept for the utilization sparkline).

| Column | Type | Notes |
|--------|------|-------|
| `id` | int PK | |
| `index` | int | GPU index. |
| `model` | str | |
| `total_vram_mb` | int | |
| `util_pct` | float | |
| `vram_used_mb` | float | |
| `temp_c` | float | |
| `current_run_id` | str null FK | |
| `sampled_at` | ts | |

### `tmux_session`

| Column | Type | Notes |
|--------|------|-------|
| `id` | str PK | |
| `name` | str | `agent`/`train-gpuN`/`term-<uuid>`. |
| `kind` | str | `agent`/`train`/`terminal`/`server`. |
| `run_id` | str null FK | |
| `status` | str | `alive`/`dead`. |
| `created_at` | ts | |

### `event`

The timeline/alert feed.

| Column | Type | Notes |
|--------|------|-------|
| `id` | str PK | |
| `type` | str | `run_started`/`run_finished`/`run_crashed`/`gpu_idle`/`agent_down`/`breakthrough`/`idea_added`/`idea_reordered`/`config_changed`/`chat`. |
| `severity` | str | `info`/`warning`/`critical`. |
| `actor` | str | `agent`/`human`/`system`. |
| `message` | text | |
| `run_id` / `idea_id` | str null FK | |
| `meta` | json | |
| `emailed` | bool | Whether it has been included in an email. |
| `created_at` | ts | |

### `chat_message`

| Column | Type | Notes |
|--------|------|-------|
| `id` | str PK | |
| `role` | str | `researcher`/`agent`. |
| `content` | text | |
| `created_at` | ts | |

### `email_log`

| Column | Type | Notes |
|--------|------|-------|
| `id` | str PK | |
| `kind` | str | `digest`/`alert`/`test`/`breakthrough`. |
| `subject` | str | |
| `to` | str | |
| `status` | str | `sent`/`failed`. |
| `error` | text null | |
| `event_ids` | json | Events covered by this email. |
| `sent_at` | ts | |

### `setting`

Key/value for non-secret runtime config (scheduler thresholds, retention,
cadence). Secrets stay in `data/secrets/.env`, never in the DB.

| Column | Type | Notes |
|--------|------|-------|
| `key` | str PK | |
| `value` | json | |
| `updated_at` | ts | |

## 8.2 REST API

Base path `/api`. JSON in/out. Auth: a signed session cookie from the passcode
exchange ([doc 09](./09-notifications-and-security.md)); the `/api/track/*`
ingest routes instead use the `ARUI_TRACKING_INGEST_TOKEN` bearer header.
Standard codes; errors are `{ "error": { "code", "message" } }`.

### Auth & session

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/session` | Current session + whether onboarding/bootstrap is done. |
| `POST` | `/api/session/login` | Body `{ passcode }` → sets the session cookie. No-op if no passcode set. |
| `POST` | `/api/session/logout` | Clears the cookie. |

### Onboarding & bootstrap

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/onboarding` | The current draft config. |
| `PUT` | `/api/onboarding` | Save the draft (auto-save). |
| `POST` | `/api/onboarding/parse-bulk` | Body `{ text }` → parsed field map (bulk paste, [doc 04](./04-onboarding-and-agent-bootstrap.md) §4.4). |
| `POST` | `/api/onboarding/validate` | Run live validation of tokens/repo/SMTP; returns per-field results. |
| `POST` | `/api/onboarding/start` | Commit config and begin the bootstrap state machine. |
| `GET` | `/api/bootstrap` | Bootstrap step states + the experiment-repo file tree. |
| `POST` | `/api/bootstrap/retry` | Body `{ step }` → retry a failed bootstrap step. |

### Project, ideas, runs

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/project` | Project summary, status, baseline, headline metric. |
| `POST` | `/api/project/pause` · `/resume` · `/stop` | Loop control. |
| `GET` | `/api/ideas` | All ideas; `?status=` filter; ordered EV-desc (manual pins first). |
| `GET` | `/api/ideas/{id}` | One idea block, full detail. |
| `POST` | `/api/ideas` | Add a human idea block. |
| `PATCH` | `/api/ideas/{id}` | Edit an idea (description, EV, status…). |
| `POST` | `/api/ideas/reorder` | Body `{ ordered_ids }` → pin manual priority; rewrites `ideas.md`. |
| `GET` | `/api/runs` | All runs; filter by `status`, `idea_id`, `is_baseline`. |
| `GET` | `/api/runs/{id}` | Full run/report payload (config, diff, summary, artifacts). |
| `GET` | `/api/runs/{id}/log` | The `run.log` (range/tail supported). |
| `GET` | `/api/runs/{id}/metrics` | Metric series; `?keys=`, `?downsample=`. |
| `POST` | `/api/runs/{id}/kill` | Kill the run's tmux session. |

### Tracking ingest (`arui` SDK → backend)

Bearer-token auth, not cookie. See [doc 06](./06-experiment-tracking.md).

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/track/run` | Register a run → `{ run_id }`. |
| `POST` | `/api/track/log` | Batch of metric points. |
| `POST` | `/api/track/artifact` | Multipart artifact upload. |
| `POST` | `/api/track/finish` | Finalize + summary. |

### GPUs, tmux, terminals

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/gpus` | Live per-GPU state. |
| `GET` | `/api/gpus/history` | Utilization history for sparklines. |
| `GET` | `/api/sessions` | All tmux sessions. |
| `POST` | `/api/sessions/terminal` | Spawn a new `term-<uuid>` → `{ name }`. |
| `DELETE` | `/api/sessions/{name}` | Kill a tmux session. |

### Agent chat

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/chat` | Chat history. |
| `POST` | `/api/chat` | Send a message to the Principal Researcher. |
| `POST` | `/api/agent/restart` | Restart the Claude Code agent. |

### Files

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/files/{name}` | Contents of `program.md`/`ideas.md`/`train.py`/`prepare.py`/`results.tsv`/`*.toml`. |
| `PUT` | `/api/files/{name}` | Write `program.md`/`ideas.md` (others read-only). |
| `GET` | `/api/files/{name}/history` | Git history of the file. |

### Events & settings

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/events` | The timeline/alert feed; `?severity=`, `?since=`. |
| `GET` | `/api/settings` | Non-secret settings. |
| `PUT` | `/api/settings` | Update settings (scheduler thresholds, cadence, retention). |
| `PUT` | `/api/settings/secrets` | Rotate a token / change passcode / SMTP (writes `.env`). |
| `POST` | `/api/settings/test-email` | Send a test email. |

## 8.3 Streaming API — SSE + one WebSocket

> **v0.2 ([doc 11 D1](./11-refinements-v0.2.md)):** one-way streams use
> **Server-Sent Events**; only the terminal is a WebSocket.

**SSE streams** — base path `/api/stream`. Each is a normal HTTP `GET` consumed
by the browser's `EventSource`. Events are JSON `{ "type", "payload" }` with an
`id:` line so `EventSource` auto-reconnects with `Last-Event-ID`. The server
replays from that id on reconnect, so no events are missed across a blip.

| SSE endpoint | Pushes |
|--------------|--------|
| `/api/stream/metrics?run_id=&keys=` | Each new metric point as it is ingested ([doc 06](./06-experiment-tracking.md) §6.5). History is fetched once via `GET /api/runs/{id}/metrics`. |
| `/api/stream/events` | Timeline events, run status transitions, idea/queue changes — drives live UI sync. |
| `/api/stream/logs?session=` | Live tail of a tmux session's `run.log` / pane (read-only). |
| `/api/stream/chat` | New chat messages from the agent as they are parsed. |
| `/api/stream/gpus` | Per-GPU utilization/VRAM samples (~every 5 s). |

**WebSocket** — the terminal only.

| WS endpoint | Purpose |
|-------------|---------|
| `/ws/terminal?session=` | Bidirectional terminal. Served by an embedded **`ttyd`** process per tmux session ([doc 11 D5](./11-refinements-v0.2.md)); the dashboard frames `ttyd`'s own endpoint. |

## 8.4 Shared types (frontend ↔ backend)

Pydantic models on the backend are the source of truth; the build emits matching
TypeScript types (via `datamodel-code-generator` or an OpenAPI → TS step) so the
React app and FastAPI never drift. Key shared types: `Project`, `Idea`, `Run`,
`MetricPoint`, `Artifact`, `Gpu`, `TmuxSession`, `Event`, `ChatMessage`,
`OnboardingConfig`, `BootstrapState`.

## 8.5 API conventions

- **Pagination** — list endpoints take `?limit=` / `?cursor=`; default limit 100.
- **Time** — all timestamps UTC ISO-8601; the frontend localizes.
- **Idempotency** — `/api/track/log` is idempotent on `(run_id, key, step)`;
  `/api/onboarding/start` is safe to call twice (resumes).
- **Errors** — `4xx` for client problems with a machine `code`; `5xx` for
  server faults; ingest errors never crash a training run (the SDK buffers).
- **Versioning** — the API is internal (one frontend, one backend, shipped
  together) so it is unversioned; breaking changes ship with both halves.
