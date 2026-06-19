# Analysis v2 spec — W&B-style multi-run dashboard

Status: **draft v1 — to be reviewed by Gemini 2.5 Pro and GPT-5 high before implementation**.

## Goals (from the user)
1. **Fast plot loading.** W&B feels instant — current Analysis tab takes seconds for 30 runs. Target: time-to-first-render ≤ 500 ms for 100 runs / 4 plots.
2. **EMA smoothing slider per plot** (per W&B).
3. **Runs table** — sortable, filterable by status, regex search, multi-select.
4. **"Baseline run" tag** — one run can be designated baseline; it's always drawn as a reference line in every panel.
5. **Multi-plot panels with shared crosshair** — hover on any panel shows a vertical line + tooltip across **all** panels at the same x-coord.
6. **Drawer "view all plots"** — a button in the run drawer that expands to show every tracked metric for that run, with a user-configurable mandatory list (default: val_loss, val_acc, lr_schedule, train_loss, train_acc, time_per_step).
7. **Configurable panel sets** — like W&B's "Add panel" flow, the user can define which metrics get a plot.

## Non-goals (explicit, to scope the work)
- Server-driven SQL pivot tables (W&B Tables) — out of scope.
- Run-comparer side-by-side text/HP diff — out of scope for v2 (kept for v3).
- Parallel-coordinates / parameter-importance — out of scope.
- Custom expressions / derived metrics — out of scope for v2.
- Sharing / public links — explicitly not needed (user said security is OOS).

## Architecture

### Data plane (backend)

**The performance issue today**: `GET /api/runs/<rid>/metrics` returns full-fidelity arrays. A 12000-step run returns ~12k points per series, JSON-decoded in the browser. With 30 runs × 4 metrics = 120 series × 12k points = 1.4M points to ship, parse, and render. That's the lag.

**Fix: server-side bucketing.** New endpoint:

```
POST /api/metrics/batch
{
  "run_ids": ["run_a", "run_b", ...],
  "keys":    ["train_loss", "val_loss", "val_acc"],
  "buckets": 500,                   // ~screen-width-many points per series
  "x_key":   "step"                 // or "wall_time" / "tokens" / "samples"
}

→ {
  "run_a": {
    "train_loss": {"x": [...], "y": [...], "y_min": [...], "y_max": [...]},
    ...
  },
  ...
}
```

Bucketing is done in DuckDB:
```sql
SELECT
  FLOOR(step / bucket_size) AS bucket,
  AVG(value)  AS y,
  MIN(value)  AS y_min,
  MAX(value)  AS y_max,
  AVG(step)   AS x
FROM metrics
WHERE run_id = ? AND key = ?
GROUP BY bucket
ORDER BY bucket
```

Cost: 100k points → 500 buckets = ~200× compression. 30 runs × 4 keys × 500 buckets = 60k floats = ~600 KB JSON. Fits in one ~50 ms response.

**Cache layer (in-process)**. Bucketed result cached per `(run_id, key, bucket_count, max_step_at_compute)`. For a finished run the result never changes; for a running run we recompute when `max_step` advances by more than `1/bucket_count`. Trivial implementation — no Redis needed.

**Key-listing endpoint**. `GET /api/metrics/keys` returns the union of all metric keys observed across all runs (one DuckDB `DISTINCT` query, cached for 5 s). Powers the "Add panel" dropdown.

**Per-run key listing**. `GET /api/runs/<rid>/metric_keys` for the drawer's "view all plots" — returns keys this run actually has data for.

**Baseline tagging**. A row in `setting` keyed `baseline_run_id`. Endpoints:
- `GET /api/baseline` → `{ "run_id": "..." }`
- `POST /api/baseline {run_id}` (pass `null` to clear)

**Plot configs / panel sets**. A row in `setting` keyed `analysis_panels`. Endpoints:
- `GET /api/analysis/panels`
- `PUT /api/analysis/panels {panels: [...]}`

Each panel:
```
{
  "id": "p1",
  "title": "Validation loss",
  "y_keys": ["val_loss"],
  "x_key": "step",
  "y_log": false,
  "ema": 0.9,
  "group_by": null,         // future
  "width": "half"           // half | full
}
```

If no panels are saved, we render a default set: train_loss, val_loss, val_acc, lr (or lr_schedule).

**Drawer mandatory plots**. A new onboarding/setting field `drawer_default_plots` — comma-separated metric keys. Default: `val_loss,val_acc,lr,train_loss,train_acc,time_per_step,samples_per_sec`. The drawer's "view all plots" section always shows a slot for each; "(not logged)" placeholder when the run has no data for that key.

### Render plane (frontend)

**Single-page Analysis layout**. Two-pane:

```
┌─────────────────────────┬───────────────────────────────────────┐
│  RUNS TABLE             │  PANELS                               │
│  ┌───────────────────┐  │  ┌──────────────┐  ┌──────────────┐  │
│  │ search [regex]    │  │  │   panel 1    │  │   panel 2    │  │
│  ├───┬─────┬────────┤  │  │   train_loss  │  │   val_loss   │  │
│  │   │name │metric │  │  └──────────────┘  └──────────────┘  │
│  │ ☑ │run1 │0.041  │  │  ┌──────────────┐  ┌──────────────┐  │
│  │ ☑ │run2 │0.039  │  │  │   panel 3    │  │   panel 4    │  │
│  │ ★ │base │0.045  │  │  │    val_acc   │  │      lr      │  │
│  └───┴─────┴────────┘  │  └──────────────┘  └──────────────┘  │
│  [+] add baseline       │  [+ add panel]                        │
└─────────────────────────┴───────────────────────────────────────┘
```

**Runs table**:
- Columns: ☐ (select), ★ (baseline tag), name, status chip, headline_metric, started (relative), GPU
- Click column header to sort (default: started ASC; baseline pinned to top regardless)
- Search box with toggle `regex` (off = substring; on = JS RegExp on `name + desc`)
- Status filter chips: all / kept / running / crashed / discarded
- Right-click (or button on row hover): "Set as baseline" / "Remove baseline"
- Multi-select via checkboxes; selected runs are what panels plot
- Virtual scroll when >300 rows (uses an IntersectionObserver pattern, no library)

**Panel grid**:
- Default layout: 2-column responsive grid (single column below 900 px viewport)
- Each panel: title, controls (EMA slider, log-y toggle, edit ✎, remove ✕), canvas
- Drag to reorder panels (HTML5 dragstart/over/drop, same as the existing idea-queue table)
- "+ Add panel" → modal with `Y axis` (multi-select metric keys), `X axis` (single key), and a name field

**EMA smoothing**. Implemented client-side over the bucketed series:
```js
let ema = ys[0];
const out = ys.map(y => (ema = alpha * ema + (1 - alpha) * y));
```
The slider runs `alpha` from 0 (off) to 0.99 (heavy). Reapplied on every slider input — cheap because we operate on 500 points, not 100k.

**Shared crosshair**. A tiny event bus:
```js
const cursorBus = { x: null, listeners: new Set() };
cursorBus.set = x => { cursorBus.x = x; cursorBus.listeners.forEach(f => f(x)); };
```
Each panel subscribes; on mousemove the active panel publishes its model-space x to the bus; every other panel renders a vertical line + tooltip at that x. On mouseleave the bus clears.

**Caching**. A `metricsCache = new Map<runId+key+bucket, series>`. Repeated panel renders, EMA-slider drags, and panel-add operations hit the cache; only fresh runs trigger a network call. Cache invalidates on `runs_changed` SSE for any run with status=running.

**Lazy fetch**. The "Add panel" modal lists available keys but does NOT fetch all series — only fetches the y_keys the user picks. Existing panels are fetched in one batch (`POST /api/metrics/batch`) on panel-set load.

### Drawer "view all plots"

- New button in the run drawer header (next to "Kill run" when running): **"View all plots"**.
- On click, a section below the existing Result/Curves panel renders a grid of mini-plots.
- The grid always shows the configured `drawer_default_plots` (one slot each, with "(not logged)" placeholder if missing).
- Below that, an "Other metrics" section auto-renders every additional key the run logged.
- Series are fetched via the same `/api/metrics/batch` (single run, all keys).

## Performance targets and how we hit them

| target                                  | current             | spec design                                  |
| --------------------------------------- | ------------------- | -------------------------------------------- |
| time-to-first-render, 30 runs / 4 plots | 3-6 s (observed)    | ≤ 500 ms                                     |
| memory per series                       | ~12k points         | 500 points                                   |
| EMA slider responsiveness               | re-fetch on change  | client-side, ≤ 4 ms / panel                  |
| shared crosshair                        | not implemented     | event bus, ≤ 1 ms / panel hover              |
| run-table search                        | not implemented     | substring filter on already-loaded list      |

## Risks and edge cases

- **Bucketing is lossy**. For very spiky losses (sudden divergence), min/max bands matter. We return them but only draw on demand (Settings toggle: "show min/max band").
- **Very long-running runs** with millions of steps. Bucket size cap = 500 means we lose detail near the start. Mitigation: support a `since=step:X` parameter for zooming (v2.1).
- **Different x-axis bases**. Some runs log `step`, others log `wall_time` only. The X axis picker reconciles by reindexing or by warning when keys don't intersect.
- **The cache can get stale.** Running runs' caches are recomputed on `runs_changed`. Edge case: a long pause then a burst of points — refresh anyway every 30 s for running runs.
- **DuckDB read concurrency**. The backend already serializes DuckDB writes; reads are parallel-safe but we should add a connection pool of ~4 read-only connections to avoid head-of-line blocking.

## Acceptance tests

1. Open Analysis tab with 100+ runs in DB, default panel set. Time-to-first-render < 500 ms (measured by Performance API).
2. Drag the EMA slider on a 100k-step series. Render updates < 16 ms per frame, no network call.
3. Hover any panel. All other panels show a crosshair at the same x within 1 frame.
4. Set a run as baseline. Open any panel. The baseline run appears as a dashed grey line labeled "baseline".
5. Open a run drawer, click "View all plots". Section expands < 200 ms; all configured default metrics shown; missing ones show "(not logged)".
6. Regex search in runs table filters as-you-type without lag (300+ runs).
7. Save a panel set, refresh the page, panels restore exactly.

## Out of scope but worth noting

- We won't build a W&B-equivalent **expression editor** (`y = train_loss / val_loss`). Mention only.
- We won't build **parallel coordinates**. The current Analysis tab's hover-tooltip multi-run chart should be retained as a single panel option ("Frontier" panel) for backward compat.
