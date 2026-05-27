# Analysis v2 review — GPT-5 high

# PERFORMANCE TRAPS

- JSON everywhere will choke at 100+ runs
  - Problem: 100 runs × 4 panels × 500 buckets × 4 arrays (x, y, y_min, y_max) = ~800k numbers per load. JSON parsing and GC of boxed numbers will dominate TTF and jank the UI, even if gzipped.
  - Fix: Switch the metrics payload to a binary format (Arrow IPC or flatbuffers/protobuf with typed arrays). On the client, keep series in Float64Array/Float32Array, not JS arrays. Enable gzip/brotli and HTTP/2.

- N×M queries vs a single grouped scan
  - Problem: The spec implies per-(run,key) queries. That’s 30–1000×4 group-bys, compiling/executing the same plan. It will blow up CPU and IO.
  - Fix: Make /api/metrics/batch do a single query:
    SELECT run_id, key, bucket, avg(value) AS y, min(value) AS y_min, max(value) AS y_max, avg(x) AS x FROM metrics WHERE run_id IN (...) AND key IN (...) AND x_key = ? AND x BETWEEN ? AND ? GROUP BY run_id, key, bucket
    Compute bucket with a projection over x (step/wall_time) inside the query. Return one Arrow table grouped by run_id/key.

- “500 buckets flat” won’t scale and loses detail where users care
  - Problem: Fixed 500 buckets for the full run kills early-epoch detail and forces full-table scans every time. For million-step runs, even a single aggregation scan per interaction is too slow.
  - Fix: Precompute multi-resolution downsample tiles (e.g., levels 64/256/1024/4096) per (run, key, x_key). Update incrementally for streaming runs. Serve the lowest level first for TTF, then swap in higher levels. Add since/until to support zoom without re-bucketing from scratch.

- Bucketing math is underspecified and can be wrong
  - Problem: The SQL shows FLOOR(step / bucket_size) but “buckets: 500” is a count, not a size. For wall_time/tokens, dividing by step is nonsense.
  - Fix: Define bucket as FLOOR((x - x_min) / (x_max - x_min) * bucket_count). Use exact x_min/x_max per (run,key,x_key) or the requested [since,until] window. Include x_key in the cache key.

- Cache thrash for running runs
  - Problem: “Invalidate on any runs_changed” + recompute when max_step moves by >1/bucket_count can recompute everything constantly for many active runs.
  - Fix: Cache per (run,key,x_key,level,[since,until]) with ETag/If-None-Match. For streaming, recompute only the last 1–2 buckets or append new tiles. Throttle recompute per run to e.g., 2 Hz. Don’t flush cache globally on any run change.

- Crosshair redraw cost
  - Problem: Publishing on every mousemove and forcing full canvas redraw on all panels will hitch with 8–12 panels.
  - Fix: Throttle to requestAnimationFrame, and render the crosshair on a lightweight overlay layer; don’t replot the series. Group crosshairs only across panels sharing the same x_key (see UX).

- “View all plots” can nuke the browser
  - Problem: A single run can have 100+ metrics. Fetching “single run, all keys” at 500 buckets each is megabytes and dozens of canvases at once.
  - Fix: Paginate/virtualize, collapse “Other metrics” by default, lazy-load each metric plot as it scrolls into view. Hard-cap initial fetch (e.g., first 20 metrics), with “Load more.” Add a total-points cap per request.

- DuckDB concurrency and table scans
  - Problem: Frequent GROUP BY scans over a large metrics table will contend with writes; a 4-connection pool won’t save you if every interaction scans cold pages.
  - Fix: Partition metrics by run_id and/or time; keep a covering index on (run_id, key, x_key, x). Consider pre-materializing tiles (above). Ensure memory-mapped parquet or write-optimized ingestion, read-optimized tiles.

- Union-of-keys query
  - Problem: DISTINCT over the whole metrics table on every 5s interval is a table scan hotspot.
  - Fix: Maintain a separate metric_keys table updated on ingestion per (run_id,key). For global list, SELECT DISTINCT key FROM metric_keys (which is tiny). Cache longer (e.g., 60s).

- Memory bloat in the browser
  - Problem: Storing multiple copies (raw + EMA) as JS arrays for many runs will balloon memory.
  - Fix: Store one Float32/64 typed array per series; compute EMA into a recycled buffer on the fly. Evict LRU panels/runs after N MB.

# UX ISSUES

- Crosshair “across all panels” but panels can have different x_key
  - Problem: This produces misleading tooltips and jitter. W&B scopes crosshair sharing to panels with the same x domain.
  - Fix: Crosshair groups by x_key. Visualize groups with a subtle colored corner marker. Default: one global group if all panels share x_key; otherwise per-group crosshairs.

- Baseline forced into every panel
  - Problem: In practice, that clutters panels where the baseline has no comparable metric or users don’t care.
  - Fix: Make “Include baseline” a per-panel toggle (default on) and grey it out when the baseline lacks that metric. Do not hard-pin baseline row at the top of the table by default (see below).

- Table default sort and pinning will frustrate
  - Problem: Default started ASC is backwards; pinning baseline regardless of sort is surprising and breaks expectations.
  - Fix: Default sort: started DESC. Add a “Pin baseline to top” toggle that’s off by default.

- Global “selected runs = everything plotted” is brittle
  - Problem: Users often want to compare a few runs in one panel but a different subset in another, or compare many runs briefly without nuking perf.
  - Fix: Two modes:
    - Global selection (current behavior) for simplicity.
    - Per-panel selection override (optional checkbox in panel config).
    - Also hard-cap plotted lines per panel (e.g., 30) with a warning and a “Show all (slow)” affordance.

- “Add panel” flow is clunky for common cases
  - Problem: Picking y_keys via multi-select is slow for the 90% use-case (plot a single metric, step on x).
  - Fix: Quick-add buttons next to metrics in key list (“+ Plot val_loss”) plus a keyboard palette (/) to search-and-add. Remember last x_key per workspace.

- Drawer “View all plots” defaults to noise
  - Problem: Auto-rendering “every additional key” creates noise and perf problems.
  - Fix: Only render the configured defaults; below that, show a searchable list of remaining keys with “tap to plot” chips. Lazy-load when opened and when scrolled.

- EMA slider defaults and persistence
  - Problem: 0.9 EMA default is arbitrary and often wrong. Users expect per-panel persistence and easy reset.
  - Fix: Default EMA off (0). Persist per-panel. Add a “Reset” button and a numeric input.

# ARCHITECTURAL GAPS

- Bucketing definition and inclusive ranges
  - Missing: Exact definition of bucket_size vs bucket_count, handling of [since,until], handling of empty buckets, interpolation for tooltip values, and alignment across runs.
  - Fix: Specify bucket_count, compute per requested [since, until] and x_key; do not skip empty buckets—emit NaN to preserve alignment by index.

- X-axis heterogeneity and reindexing
  - Missing: How to align runs when some lack the chosen x_key, or when x ranges vastly differ.
  - Fix: If a run lacks x_key, omit it with a clear message per panel. If x ranges differ, normalize to requested [since,until]. No auto-reindexing magic; keep semantics explicit.

- Caching keys
  - Missing: x_key and [since,until] are not included in cache key; neither is min/max band flag or downsample level.
  - Fix: Include (run_id, key, x_key, level, since, until, band_mode) in cache key.

- Charting library choice
  - Missing: You’re implicitly writing your own high-perf charting. That’s months of edge cases.
  - Fix: Pick a proven canvas-first lib that supports millions of points and overlays (uPlot is a strong choice; visx+canvas if you need React-y). Plan for plugin to draw min/max band and baseline.

- SSE contract
  - Missing: “runs_changed” is too coarse. There’s no contract for metric appended events, nor backoff logic.
  - Fix: Add metrics_changed(run_id) SSE and per-run backoff. The client coalesces updates and refetches only changed tiles.

- Indexing/partitioning strategy
  - Missing: How metrics are laid out on disk to support fast grouped scans, and how long-term retention works.
  - Fix: Partition metrics by run_id and date; maintain a separate tiles table keyed by (run_id, key, x_key, level, tile_index). Add retention/TTL for raw metrics if storage matters.

# SIMPLER ALTERNATIVES

- Don’t build your own virtualization and hover plumbing
  - Use react-window/react-virtualized for the runs table. It’s a solved problem and will handle thousands of rows smoothly.

- Don’t auto-plot “Other metrics”
  - Replace with a searchable list and click-to-plot. You’ll save a ton of complexity and crashes.

- Skip min/max band toggle in v2
  - If you adopt LTTB or a “avg + min/max envelope” tile, draw the band only when zoomed or on hover as a detail. Default it off and de-prioritize the setting.

- One global “screen width → buckets” heuristic is enough for v2
  - If you can’t precompute tiles yet, do progressive fetching: first request 100 buckets for TTF, then 500 in the background. Much simpler and gets you most of the perceived speed.

# CONCRETE FIXES

- Transport and client data model
  - Change: /api/metrics/batch returns Arrow IPC with columns [run_id, key, x, y, y_min, y_max, bucket]. Client deserializes into typed arrays.

- Batch query
  - Change: Implement a single DuckDB query grouping by (run_id, key, bucket), with bucket computed from (x - since) / (until - since) * bucket_count.

- Multi-resolution tiles
  - Change: Add a background job to maintain downsampled tiles at levels [64,256,1024,4096] per (run,key,x_key). Endpoint gains params level and [since,until]. On first paint, request lowest level, then swap in higher.

- Cache keys and invalidation
  - Change: Cache key = (run_id, key, x_key, level, since, until). Add ETag and serve 304 when unchanged. For running runs, only refresh trailing tiles, max 2 Hz per run.

- Crosshair groups
  - Change: Crosshair only synchronizes among panels sharing x_key. Throttle to rAF and draw on overlay; don’t re-render series.

- Runs table virtualization
  - Change: Replace custom IntersectionObserver logic with react-window. Keep regex filtering client-side but cap to 10k rows before you require server-side filtering.

- Drawer “view all plots”
  - Change: Show only drawer_default_plots as collapsed list of mini-panels, lazy-loaded per panel. Below that, a searchable list of remaining keys; tap to render. Paginate. Cap total points per drawer view (e.g., 200k).

- Panel selection model
  - Change: Keep global selection but add “Use custom selection” per panel. Add a soft cap (e.g., 30 series) with a warning.

- Defaults and toggles
  - Change: Default sort started DESC. Make “Pin baseline to top” a toggle (off). EMA default off; persist per-panel; add reset.

- Key listing
  - Change: Maintain a small metric_keys table updated on write. /api/metrics/keys queries that instead of DISTINCT over metrics.

- Baseline rendering
  - Change: Allow per-panel “Include baseline” toggle. If included, render with a distinct style only if the metric exists; otherwise, show a small “baseline metric missing” badge in the legend.

- Specify bucketing semantics
  - Change: Document precise formula for bucket index, NaN handling for empty buckets, and interpolation policy for tooltip values (e.g., nearest neighbor).

# TL;DR — Top 3 changes I’d make

1) Switch metrics transport to Arrow + typed arrays and do a single grouped batch query. JSON will kill you at 100+ runs; N×M queries will kill the server.

2) Add multi-resolution downsample tiles (64/256/1024/4096) per (run,key,x_key). Serve low-res first, then swap in higher. This removes full scans on every interaction and fixes million-step runs.

3) Scope shared crosshair to panels with the same x_key, and use a proven charting lib (uPlot) + react-window for the runs table. Don’t build custom virtualization/plotting—you’ll spend weeks chasing perf and edge cases.