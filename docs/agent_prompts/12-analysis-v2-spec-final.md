# Analysis v2 — final spec (post-review)

Status: **final, ready for implementation** after synthesizing reviews from
Gemini 2.5 Pro (`external-review-analysis-v2-gemini.md`) and GPT-5 high
(`external-review-analysis-v2-openai.md`). All v1-only ideas that survived
review are kept; design changes the reviewers insisted on are folded in;
nice-to-haves that would balloon scope are deferred to v2.1.

## What changed from v1, and why

| v1 decision | v2 final decision | reason |
|---|---|---|
| Bucketing via `AVG(value), AVG(step)` | `ARG_MAX(value, step) AS y_last, MIN(value) AS y_min, MAX(value) AS y_max, MIN(step) AS x_min, MAX(step) AS x_max` per bucket; NaN for empty buckets (preserve index alignment) | Gemini + GPT: `AVG(step)` misplaces points; spikes vanish; min/max band gives candlestick fidelity |
| `buckets: 500` flat over whole run | `bucket_count` taken over `[x_min, x_max]` window passed by the client; zoom = re-request | GPT: million-step runs scan too much per interaction; spec must support zoom from day 1 |
| Per-(run,key) queries | **One** batched DuckDB query: `GROUP BY run_id, key, bucket WHERE run_id IN (...) AND key IN (...)` | GPT: N×M plan compiles will blow CPU |
| `runs_changed` invalidates cache | SSE `metrics_changed(run_id)` + 2 Hz per-run throttle on recompute | GPT: blanket invalidation thrashes 50+ active runs |
| `baseline_run_id` in `setting` table | Stored in browser `localStorage` (per-tab/per-user) | Gemini: it's a personal-context choice, no need to round-trip the server |
| EMA slider 0→0.99 raw alpha | "Smoothing" slider 0→1, EMA off by default, **persisted per panel** | Both: matches W&B; raw alpha is unintuitive; per-panel persistence is what users expect |
| Shared crosshair across all panels | Crosshair groups **by `x_key`** (panels sharing the same x-axis); `requestAnimationFrame`-throttled; drawn on a separate overlay layer (no series re-draw) | GPT: panels with different x-axes give misleading crosshair; full re-draw on every mousemove hitches |
| `GET /api/metrics/keys` does `DISTINCT` | Maintain a `metric_keys` table on ingestion; query that | Both: DISTINCT over the whole metrics table is a hotspot |
| Drawer "view all plots" auto-renders every key | **Always** renders only the configured `drawer_default_plots` (val_loss, val_acc, lr, train_loss, train_acc, time_per_step, samples_per_sec) plus a **searchable list** of remaining keys with "tap to plot" chips; lazy-rendered as panels scroll into view; total-points cap 200 k | Both: 100+ tiny canvases will crash the browser |
| Default sort started ASC | Default sort started **DESC** (newest first) in the Analysis tab. (The Dashboard runs table keeps the user-requested ASC chat-feed style — Analysis is a different tool.) | GPT: Analysis tab users want newest first; user's earlier ASC ask was about the dashboard feed |
| No grouping support | Endpoint accepts optional `group_by_config_key` (e.g. `lr`) — backend returns `{group_value: series}` instead of `{run_id: series}`, with mean/stddev. UI deferred to v2.1 but architecture supports it | Gemini: this is a fundamental W&B primitive; bolting it on later would force a refactor |
| URL state was not addressed | Selected runs, panel set id, smoothing-per-panel, baseline are mirrored to URL query params via `history.replaceState` | Gemini: bookmarkability + share-by-link without building any sharing infra |
| JSON for everything | JSON for v2; design payload so we can swap in Arrow IPC in v2.1 if we hit perf limits (binary path adds ~1 day; defer until measured) | GPT was right that Arrow is faster; but v2 doesn't need it to hit the target |

## What we explicitly defer to v2.1 (out of scope now)

- Multi-resolution **pre-computed** tiles (we'll bucket on demand with the 2 Hz throttle + LRU cache; revisit if benchmarks miss the 500 ms target)
- Arrow IPC transport (JSON + gzip + typed-array deserialization on the client should be enough; we'll measure)
- Per-panel selection override (global selection only for v2; common case)
- Group-by UI (endpoint supports it, UI doesn't)
- Key-value filter syntax in the search box (we ship regex toggle now; key:value next)
- A proven charting lib (`uPlot`) — staying on hand-rolled canvas for v2 to keep the vanilla-JS no-build constraint; reassess if perf misses target after tile work

## Final architecture

### Backend

**New endpoints**
```
POST /api/metrics/batch
  {
    "run_ids":   ["run_a", "run_b"],
    "keys":      ["train_loss", "val_loss"],
    "x_key":     "step",                // "step" | "wall_time"
    "x_min":     null,                  // optional zoom range
    "x_max":     null,
    "bucket_count": 500,
    "group_by_config_key": null         // optional, e.g. "lr"
  }
  → {
    "x_key": "step",
    "buckets": 500,
    "series": [
      {
        "key": "train_loss",
        "run_id": "run_a",              // OR "group_value": "lr=1e-4"
        "x":     [Number, ...],         // x_min of each bucket
        "y":     [Number|null, ...],    // y_last of each bucket; null = empty
        "y_min": [Number|null, ...],
        "y_max": [Number|null, ...]
      }, ...
    ]
  }

GET /api/metrics/keys
  → {"keys": ["train_loss", "val_loss", ...]}     // from metric_keys table

GET /api/runs/<rid>/metric_keys
  → {"keys": ["train_loss", "val_loss", ...]}     // for this run only
```

**Cache**
- Key: `(run_id, key, x_key, x_min, x_max, bucket_count, group_by)`
- TTL: forever for runs with `status != "running"`
- For running runs: throttle recompute to 1× per 500 ms per run; SSE
  `metrics_changed(run_id)` triggers cache invalidation but the next
  request rebuilds (not the SSE handler)

**Bucketing SQL**
```sql
WITH params AS (SELECT ? AS xmin, ? AS xmax, ? AS nb),
ranged AS (
  SELECT run_id, key, x, value,
         FLOOR((x - xmin) / NULLIF(xmax - xmin, 0) * nb)::INT AS bucket
  FROM metrics, params
  WHERE run_id IN (?) AND key IN (?) AND x BETWEEN xmin AND xmax
)
SELECT run_id, key, bucket,
       MIN(x)                AS x_first,
       MAX(x)                AS x_last,
       ARG_MAX(value, x)     AS y_last,
       MIN(value)            AS y_min,
       MAX(value)            AS y_max
FROM ranged
GROUP BY run_id, key, bucket
ORDER BY run_id, key, bucket;
```
Empty buckets are emitted as `NULL` rows (server reconstructs the dense
`bucket_count`-long arrays so client indices align across all series in
a panel).

**`metric_keys` table**
```sql
CREATE TABLE IF NOT EXISTS metric_keys (
  key   TEXT PRIMARY KEY,
  last_seen_at TEXT
);
```
Populated on every `arui.log()` call: `INSERT OR REPLACE INTO metric_keys
VALUES (?, ?)`.

**SSE**
- Existing: `runs_changed` (kept)
- New: `metrics_changed` with payload `{run_id: "..."}` — emitted by the
  ingest endpoint on every `track/log` (debounced to 1× per 500 ms per
  run_id).

### Frontend

**Layout** (Analysis tab):
```
┌──────────────────────┬────────────────────────────────────────────┐
│ RUNS TABLE           │ PANELS GRID                                │
│  search [regex☐]    │  ┌──────────┐ ┌──────────┐                  │
│  status: [all ▾]    │  │ train_loss│ │ val_loss  │                  │
│ ┌───┬─────┬───────┐ │  │ ⚙ ✕      │ │ ⚙ ✕      │                  │
│ │ ☑ │name │metric │ │  └──────────┘ └──────────┘                  │
│ │ ★ │base │0.045  │ │  ┌──────────┐ ┌──────────┐                  │
│ │ ☑ │a    │0.041  │ │  │ val_acc  │ │ lr       │                  │
│ │ ☑ │b    │0.039  │ │  │ ⚙ ✕      │ │ ⚙ ✕      │                  │
│ └───┴─────┴───────┘ │  └──────────┘ └──────────┘                  │
│  [solo] [clear]     │  [+ Add panel]                              │
└──────────────────────┴────────────────────────────────────────────┘
```

**Runs table** — vanilla JS virtualization (IntersectionObserver pattern):
- Columns: ☐ select, ★ baseline indicator, name, status chip, headline_metric, started (relative ago), GPU
- Click header to sort. Default: started DESC.
- Search box + regex toggle (substring by default; regex on toggle)
- Status filter chips: all / kept / running / crashed / discarded
- "Solo" button: select only the clicked row, deselect all others
- "Set as baseline" / "Remove baseline" in row hover menu (stored in localStorage)
- Multi-select via checkboxes

**Panels grid**:
- 2-column responsive grid (1 col below 900 px viewport)
- Each panel header: title, ⚙ (config: smoothing, log-y, show-band, include-baseline), ✕ (remove)
- Smoothing slider 0→1, EMA off by default, persisted per panel
- Y-log toggle (per panel)
- Crosshair grouping: panels sharing `x_key` share crosshair; others don't
- "+ Add panel" → modal with `Y axis` multi-select, `X axis` single select, title, defaults remembered

**Drawer "View all plots"** button (new):
- Button in the run drawer header next to "Kill run"
- Click → expand a section below "Result"
- Renders one panel per `drawer_default_plots` entry (placeholder "(not logged)" if missing)
- Below that, searchable list of remaining keys; each is a "+ plot" chip
- Lazy: each panel only fetches when scrolled into view (IntersectionObserver)
- Hard cap: total fetched bucketed points ≤ 200 k

**State persistence**
- URL query params: `runs=a,b,c&base=run_x&panels=p1` (panels = id of saved panel-set)
- Panel set CRUD via `GET/PUT /api/analysis/panels` (server-side, so multiple browser tabs share)
- Smoothing-per-panel: stored in the panel JSON server-side
- `baseline_run_id`: `localStorage["arui:baseline"]`

**EMA math** (client-side):
```js
function smoothed(ys, alpha) {
  if (!alpha) return ys;
  const out = new Float64Array(ys.length);
  let s = NaN;
  for (let i = 0; i < ys.length; i++) {
    const v = ys[i];
    if (v == null || Number.isNaN(v)) { out[i] = NaN; continue; }
    s = Number.isNaN(s) ? v : alpha * s + (1 - alpha) * v;
    out[i] = s;
  }
  return out;
}
```

**Shared crosshair bus**:
```js
const cursorBus = {
  x: null, group: null, listeners: new Set(),
  set(x, group) {
    this.x = x; this.group = group;
    requestAnimationFrame(() =>
      this.listeners.forEach(fn => fn(this.x, this.group)));
  },
};
```
Each panel subscribes once; on `mousemove` it publishes `(modelX, x_key)`;
every other panel with the same x_key draws a crosshair at that x. The
crosshair is drawn on a separate 2D context (overlay layer); the series
canvas is not re-rendered.

## Performance targets (unchanged from v1, but now realistic)

| target | current | spec |
|---|---|---|
| TTF, 30 runs / 4 panels | 3-6 s | ≤ 500 ms |
| TTF, 100 runs / 4 panels | (unusable) | ≤ 1.2 s |
| Smoothing slider response | re-fetch | ≤ 4 ms / panel client-side |
| Crosshair latency | n/a | ≤ 1 frame (16 ms) |
| Drawer "view all plots" open | n/a | ≤ 200 ms (defaults visible), lazy thereafter |

## Acceptance tests (v2 done = all green)

1. 100 runs in DB, default panel set, Analysis tab loads with first-paint < 1.2 s (Performance API).
2. Drag smoothing slider on a 100k-step series: no network call, < 16 ms / frame.
3. Hover any panel: same-`x_key` panels show crosshair within 1 frame; different-x_key panels don't.
4. Set a run as baseline. Refresh the page. Baseline state survives.
5. Drawer "view all plots" expands < 200 ms, shows configured defaults + "(not logged)" placeholders, plus searchable extra-keys list.
6. Regex search across 500 rows: instant, no lag.
7. Save a panel set, refresh page, panels restore exactly.
8. Add a panel with x_key=`step` and another with x_key=`wall_time`. Hover on the step one: only the step panel shows a crosshair (not the wall_time one).
9. Zoom on a panel (drag-select an x range): only the visible range is re-fetched at bucket_count=500.

## Implementation order (what I'll build in order)

1. **DuckDB metric_keys table + ingest hook** (`metrics.py` extend `arui.log` ingest)
2. **`POST /api/metrics/batch`** endpoint with the new SQL + cache layer
3. **`metrics_changed` SSE** + debounce
4. **Backend tests** (Python-level): batch endpoint correctness, NaN-empty buckets, group-by stub
5. **Frontend `MultiChart` rewrite** — canvas + overlay + crosshair bus + smoothing
6. **Runs table** rewrite — virtualization, sort, regex, baseline localStorage, solo button
7. **Panels grid** — add/remove/reorder, config modal, persisted panel set via `/api/analysis/panels`
8. **Drawer "View all plots"** — button + lazy section + searchable list
9. **URL state mirroring** — `history.replaceState` on selection/panel/baseline change
10. **End-to-end test pass** against acceptance criteria
