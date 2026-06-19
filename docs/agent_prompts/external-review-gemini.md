# Gemini review (model: gemini-2.0-flash)

Okay, here’s my review of `autoresearcherUI`, focusing on your priorities and aiming for concrete, actionable feedback. I’m prioritizing impact and speed to implementation.

**A. SINGLE HIGHEST-IMPACT CHANGE**

**Change:** Prioritize **interactive, *client-side* filtering and sorting in the Experiments table and Live Graphs.**

**Why:** This directly addresses your "slick, beautiful, performant UI" goal. Researchers will spend *most* of their time slicing and dicing experiment data. Baking this into the client dramatically improves the experience compared to server-side filtering and resorting, which introduces lag on every interaction. This means:

*   **Experiments table:** Implement filtering (status, date range, metric range) and sorting (any column) *entirely in the frontend*.  Load all experiments (or a very large chunk) initially.  Use a library like TanStack Table or similar to handle the filtering and sorting logic *efficiently* on the client-side.
*   **Live Graphs:** Similar concept. Load a reasonable history of data and implement interactive filtering of runs and metric selection on the client.

This change shifts complexity to the frontend but delivers a *massive* user experience boost, making exploration fluid and intuitive.  Consider using Web Workers for heavy client-side processing if necessary to avoid blocking the main thread.

**B. ARCHITECTURE & STACK - SIMPLIFICATIONS & SPEEDUPS**

1.  **Remove SQLite entirely for high-rate metrics.**  The Parquet files *are* the database.

    *   **Reason:**  You're trying to optimize for a solo researcher on a single node. SQLite adds overhead and complexity (concurrency, WAL mode, etc.) that's unnecessary for high-rate streams.

    *   **Instead:**  Directly serve the Parquet files via DuckDB (embedded in the backend). DuckDB can query Parquet files incredibly efficiently. The ingest service appends to Parquet; the API directly queries Parquet via DuckDB. Much simpler data flow.  The `run` summary information, however, can remain in SQLite, linked to the Parquet path.

2.  **Ditch `uv` and use `poetry` instead.**

    *   **Reason:** `uv` is very new and still has some rough edges. Poetry is more mature, well-documented, and widely used. It also handles packaging and publishing more smoothly.

    *   **Benefit:** A more reliable and familiar development experience.

3.  **Simplify Realtime - ditch native WebSockets for Server Sent Events (SSE).**

    *   **Reason:** While WebSockets are great for bidirectional communication, for the primary use case of streaming logs, metrics, and events, SSE is much simpler to implement. SSE is HTTP-based, meaning you don't need a separate WS library/connection, simplifies auth (uses the same session cookie), and works more reliably with proxies.
    *   **Benefit:** Significantly reduced backend complexity.
    *   **Caveat:** Limited bi-directional communication if you require it later for functionality.
4.  **Consider a simpler email integration.** If Resend API is sufficient, ditch SMTP entirely. Less configuration for users, less code to maintain.

**C. UI/UX - SLICKNESS BOOSTERS**

1.  **Onboarding - progressive disclosure.** Don't overwhelm the user with *all* fields upfront. Start with the absolute minimum (GitHub token, Claude token, repo name), then progressively reveal more settings as they’re needed (e.g., advanced model options, alert cadence) via an "Advanced Settings" accordion.

2.  **Experiment Table - heatmaps.** Add optional heatmap visualizations directly to numeric columns (e.g., metric result). This immediately highlights patterns and outliers.

3.  **Live Graphs - brushing & linking.** Implement brushing and linking between charts. If the user selects a region on one chart, highlight the corresponding region on all other charts.

4.  **Experiment Report - smart code diff.** Instead of *just* showing the diff vs. baseline, use a more advanced diffing algorithm (e.g., semantic diffing) to highlight *meaningful* changes, even if lines have moved around. Offer the option to switch between "raw" and "smart" diffs.

5.  **Mobile - custom keyboard accessory view.** For editing `program.md` and `ideas.md` on mobile, create a custom keyboard accessory view with commonly used Markdown formatting buttons (bold, italic, lists, headings). This dramatically improves the editing experience on touch screens.

6.  **Color palette.**  Choose a *very* deliberate and restrained color palette.  Less is more.  Use color strategically to highlight key information (e.g., metric improvement, GPU utilization). Avoid gratuitous color. The Radix UI primitives already nudge you in this direction.

7.  **Micro-interactions.** Sprinkle in subtle but delightful micro-interactions (e.g., a smooth transition when sorting the experiment table, a subtle animation when a new metric point arrives). These details make the UI feel polished.

**D. KILLER FEATURES (RANKED)**

1.  **Automated idea scoring.** Train a small model (on-box) to predict the EV of a new idea block *before* the agent even implements it.  This gives the researcher an early signal of potentially promising ideas.

2.  **Interactive `program.md` explorer.** Visualize the `program.md` as a graph of actions, constraints, and goals.  Let the researcher *interactively* edit the program flow by manipulating the graph.  This provides a higher-level way to steer the agent.

3.  **Automated outlier detection in runs.**  Train an on-box model to flag "anomalous" runs based on their metric patterns, even if the headline metric isn’t necessarily *better* than the baseline.  This can surface unexpected discoveries.

**E. PERFORMANCE TECHNIQUES**

1.  **uPlot optimization.**
    *   Use pre-allocated arrays for uPlot data.
    *   Minimize re-renders by only updating the specific parts of the chart that have changed.
    *   Experiment with different downsampling algorithms (LTTB is a good starting point, but there might be better options for specific metric patterns).
2.  **Virtualization for large tables.** Use a virtualization library (e.g., react-window, react-virtualized) to render only the visible rows in the experiment table.
3.  **Terminal performance.**
    *   Throttle the rate at which terminal data is sent to the frontend.
    *   Use a fixed-size buffer for terminal data.
    *   Consider using a binary format for terminal data to reduce bandwidth.
4.  **Code Splitting.**  Use React.lazy and dynamic imports to split the frontend bundle into smaller chunks. Load only the code that is needed for the current view.

**F. IMPLEMENTATION ORDER (LEANEST PATH)**

1.  **M0 - Skeleton + SQLite (but minimize it)** - Focus on the FastAPI shell, React app structure, routing, authentication, and a *very* minimal SQLite setup (just the `run` and `project` tables).
2.  **M1 - Node Setup** - Get the basic `setup.sh` working.
3.  **M2 - Onboarding & Bootstrap** - Prioritize the onboarding form and agent launch.
4.  **M3 - (Modified) Engine & Scheduler** - Focus on getting the basic research loop running *without* worrying about perfect GPU utilization.  Get *one* GPU doing something first.
5.  **M4 - Experiment Tracking (with Parquet + DuckDB and SSE!)** - Implement the `arui` SDK and get metrics streaming to the backend. Use DuckDB to serve the metrics directly from Parquet.
6.  **M5 - Experiments Table, Experiment Report, Live Graphs (with Interactive Filtering Client-Side!)** - Build the core UI elements and get them displaying data. Make heavy use of client-side filtering and sorting.
7.  **M6 - Terminals & Agent Chat** - Add terminal access and agent chat.
8.  **M7 - Notifications** - Add email notifications.
9.  **M8 - Mobile Polish & Hardening** - Polish the mobile UI and improve performance.

**G. RISKS & GOTCHAS**

1.  **Agent unreliability:** Claude Code can be unpredictable.  Handle agent failures gracefully.
2.  **Parquet performance:** Large Parquet files can be slow to query, even with DuckDB.  Experiment with different Parquet compression codecs and partitioning strategies.
3.  **State management complexity:** The frontend will become complex as you add more features.  Use a well-structured state management solution (e.g., Zustand, Recoil).
4.  **Mobile browser limitations:** Mobile browsers have more limited resources than desktop browsers. Be mindful of memory usage and performance.
5.  **Tailscale reliability:** While Tailscale is generally reliable, it can occasionally have connectivity issues.  Handle Tailscale disconnects gracefully.

This should give you a solid foundation for building an excellent `autoresearcherUI`.  Good luck!


