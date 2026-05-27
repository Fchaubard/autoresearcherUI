# Analysis v2 review — Gemini 2.5 Pro

Excellent spec. It's clear, detailed, and correctly identifies the main performance bottleneck. You've got the right core idea with server-side bucketing. My feedback is focused on hardening this design to prevent future refactors and to align the UX more closely with the power-user workflows that make W&B sticky.

Here is my review.

### PERFORMANCE TRAPS

1.  **Naive Bucketing Aggregation.** The proposed `AVG(value)` and `AVG(step)` in your SQL query will distort the data. `AVG` smooths over important spikes and `AVG(step)` misplaces the data point in time. A run that logged at steps 10, 20, and 90 in a bucket from 0-100 would have its `AVG(step)` be 40, which is not representative. For time series, the *last* point in a bucket is often the most important.

    *   **CONCRETE FIX:** Use a more sophisticated aggregation that preserves the shape of the data. A good, cheap starting point is to select the first, last, min, and max points within each bucket. DuckDB's `ARG_MAX` and `ARG_MIN` are perfect for this.

    ```sql
    -- Revised Bucketing Query
    WITH b AS (
      SELECT
        FLOOR(step / bucket_size) AS bucket_id,
        step,
        value
      FROM metrics
      WHERE run_id = ? AND key = ?
    )
    SELECT
      MIN(step) AS x_min,
      MAX(step) AS x_max,
      (ARG_MAX(value, step)) AS y_last, -- Value at the max step
      (ARG_MIN(value, step)) AS y_first, -- Value at the min step
      MIN(value) AS y_min,
      MAX(value) AS y_max
    FROM b
    GROUP BY bucket_id
    ORDER BY bucket_id
    ```
    This gives the client the data to draw a much more accurate "candlestick" or min/max area band, with the primary line connecting the `y_last` points.

2.  **Re-fetching History for Running Runs.** The cache invalidation logic (`recompute when max_step advances by more than 1/bucket_count`) is a trap. For a run with 1M steps and 500 buckets, you'd re-query and re-aggregate all 1M points every 2000 new steps. This will hammer DuckDB as more runs are active.

    *   **CONCRETE FIX:** Separate historical data from live data.
        1.  On initial load, fetch the bucketed data for a running run as if it were complete.
        2.  Then, poll a new, lightweight endpoint like `GET /api/runs/<rid>/metrics/tail?key=k1&since_step=...` which returns *raw, un-bucketed* points since the last step you've seen.
        3.  The client appends these new points to its high-fidelity series and the downsampled series. This avoids re-querying the entire history and provides a smoother live-updating experience.

3.  **Unbounded Key Listing.** The `GET /api/metrics/keys` endpoint, which does a `DISTINCT` across the entire metrics table, will not scale. With millions of runs and thousands of unique (and sometimes garbage) metric keys, this query will become a bottleneck.

    *   **CONCRETE FIX:** Maintain a separate, smaller table like `project_metric_keys (key_name, last_seen)` that is updated by a trigger or background job. Querying this small table will be instant.

### UX ISSUES

1.  **Global "Baseline Run".** This is a critical flaw. A baseline is context-dependent. What I consider the baseline for my hyperparameter search is not what my colleague considers the baseline for their architecture experiment. A single, global `baseline_run_id` will cause constant conflicts and frustration in a multi-user environment.

    *   **CONCRETE FIX:** Make the baseline a client-side setting stored in `localStorage`. It's simpler, requires zero backend changes, and correctly scopes the "baseline" to the individual user's session. The "Set as baseline" button now just writes the run ID to local storage. The frontend reads from there and fetches the baseline run's data along with the selected runs.

2.  **Drawer "View All Plots".** This feature is a solution in search of a problem. It creates UI clutter and a configuration burden (`drawer_default_plots`). The user journey is rarely "show me 50 tiny, context-free plots for one run." The more common journey is "how does this specific run look compared to others *on my carefully curated dashboard*?"

    *   **CONCRETE FIX:** Drop the "View all plots" drawer entirely. Replace it with a "Solo" or "Focus" button (e.g., an eyeball icon 👁️) on each run in the table. Clicking it de-selects all other runs and selects only that one. This leverages the existing, powerful panel grid to inspect a single run, which is a much better experience. This also removes the need for the `drawer_default_plots` setting.

3.  **EMA Alpha Slider.** Exposing the raw `alpha` parameter from `0` to `0.99` is unintuitive for users. W&B uses a "Smoothing" slider from 0 to 1, where higher means more smoothing. This is an abstraction layer that matters.

    *   **CONCRETE FIX:** Keep the client-side EMA logic, but make the slider go from 0 to 1. Map this value to an alpha internally. A common mapping is `alpha = slider_value`. The label should be "Smoothing", not "EMA".

4.  **Missing Run Grouping.** The spec defers `group_by`, but this is arguably the single most important feature of W&B's analysis view. Without the ability to group 100 runs by `config.learning_rate` and see the average performance of each group, the tool is just a log viewer. Deferring this means the panel and data-fetching architecture might not account for it, leading to a painful refactor.

    *   **CONCRETE FIX:** While you don't need to build the full UI for it in v2, the `POST /api/metrics/batch` endpoint should be designed to support it. It should accept an optional `group_by_key` (a config key). The backend would then return aggregated series (mean, stddev) per group value, not per run. This is a fundamental analysis primitive and should be in the architecture from day one.

### ARCHITECTURAL GAPS

1.  **No Zoom/Pan Support.** The spec mentions this as a `v2.1` mitigation, but it's a v2 architectural necessity. Users will immediately try to zoom into a noisy plot. A fixed `buckets: 500` endpoint is insufficient. Without zoom, you can't inspect divergences or fine-grained behavior.

    *   **CONCRETE FIX:** Add `x_min` and `x_max` parameters to the `POST /api/metrics/batch` endpoint from the start. The bucketing logic should operate *within that range*. This is a core requirement for the data plane and impacts caching strategy. The cache key must now include the `(x_min, x_max)` range.

2.  **State Management & URL.** The spec doesn't define where the complete view state lives (selected runs, filters, panel configs, sort order). If it's all in component state, the user loses their setup on refresh. This is a major frustration.

    *   **CONCRETE FIX:** The UI state (filters, selected runs, panel layout, sort order, etc.) should be serialized into the URL's query parameters. This makes every view bookmarkable and shareable (even if "sharing" is just pasting a link to a colleague in Slack). This is a standard pattern for complex dashboards and should be planned for.

3.  **Inconsistent X-Axes.** The spec notes this as a risk but the proposed solution ("reconciles by reindexing or by warning") is vague. This needs a firm decision. If a user puts `train_loss` (x=step) and `gpu_util` (x=wall_time) on the same panel, what happens? W&B's solution is to let the user choose one "master" x-axis for the whole dashboard/workspace, and it re-samples other metrics onto that axis.

    *   **CONCRETE FIX:** Define a project-level default x-axis (`step` is a good default). The `/api/metrics/batch` endpoint's `x_key` parameter is the right idea. When a panel requests metrics with different native x-axes, the backend should be responsible for resampling them to the requested `x_key` before bucketing. Trying to do this on the client is a nightmare.

### SIMPLER ALTERNATIVES

1.  **Baseline Run.** As mentioned, moving this to `localStorage` is vastly simpler than the proposed backend API and `setting` table row. It's a pure frontend change.

2.  **Runs Table Search.** A simple substring filter is a good start, but a dedicated `regex` toggle is clunky.

    *   **CONCRETE FIX:** Instead of a toggle, implement a simple key-value filter syntax in the search box, like W&B or GitHub (`status:crashed my-run-name`). This is more powerful and discoverable than regex for the common case. You can parse this on the client and apply filters to the in-memory list of runs.

### TL;DR: Top 3 Changes

1.  **Architect for Zoom from Day 1.** Add `x_min` / `x_max` to your `/api/metrics/batch` endpoint now. A static 500 buckets is a dead end. This is the most critical architectural change.
2.  **Fix the Live-Update Strategy.** Drop the complex cache-revalidation for running runs. Implement a "fetch-history-once, then poll-for-tail" model. It's more performant, scalable, and provides a better UX.
3.  **Drop "View All Plots" Drawer, Use `localStorage` for Baseline.** Replace the drawer with a simpler "Solo Run" button on the runs table. This removes a complex, low-value feature. Moving the baseline to `localStorage` removes a backend API and solves the multi-user conflict problem with zero server-side code.