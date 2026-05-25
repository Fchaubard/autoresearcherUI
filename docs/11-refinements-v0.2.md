# 11 — Refinements v0.2 (external review + final decisions)

The v0.1 spec ([docs 00–10](./README.md)) was reviewed by two independent
frontier models — **OpenAI GPT-4o** and **Google Gemini 2.0 Flash** — with a
brief that ranked the author's priorities as: (1) ease of use, (2) a slick,
beautiful, performant UI, (3) maximum researcher value, (4) architecture
quality, and explicitly told them to **ignore security** (open-source research,
disposable rented nodes).

This document records what they said, what is **adopted** vs. **rejected** (with
reasons), and the resulting v0.2 architecture. **Where this document conflicts
with docs 02–10, this document wins** — the affected docs have been edited to
match, but doc 11 is the authoritative record of the decision.

## 11.1 Decisions at a glance

| # | Change | Source | Verdict |
|---|--------|--------|---------|
| D1 | SSE for one-way streams; WebSocket only for the terminal | Gemini | ✅ Adopt |
| D2 | DuckDB over Parquet for metrics; drop the SQLite `metric_point` table | Both | ✅ Adopt |
| D3 | Client-side filtering/sorting everywhere (load all, TanStack Table) | Gemini | ✅ Adopt |
| D4 | Progressive-disclosure onboarding (essentials first, advanced collapsed) | Gemini | ✅ Adopt |
| D5 | `ttyd` for the browser terminal instead of a custom PTY bridge | OpenAI | ✅ Adopt |
| D6 | `framer-motion` micro-interactions throughout | Both | ✅ Adopt |
| D7 | First-run guided tour (`react-joyride`/`onborda`) | OpenAI | ✅ Adopt |
| D8 | Resend as the default email path; SMTP demoted to "advanced" | Both | ✅ Adopt |
| D9 | Heatmap-tinted numeric cells in the experiments table | Gemini | ✅ Adopt |
| D10 | Synchronized cursor / brushing-and-linking across live charts | Gemini | ✅ Adopt |
| D11 | Pinnable chart/metric tiles on the Overview | OpenAI | ✅ Adopt |
| D12 | An auto-written **Research Journal** (daily narrative) | OpenAI | ✅ Adopt |
| D13 | A first-class **Run Compare** view (2–4 runs side by side) | author | ✅ Adopt |
| D14 | A defined, restrained color system + virtualization + code-splitting | Both | ✅ Adopt |
| D15 | Keep `uv` (do **not** switch to Poetry) | Gemini (rejected) | ❌ Reject |
| D16 | Keep the "never idle a GPU" guarantee (don't gut the scheduler) | OpenAI (partial) | ⚠️ Partial |
| D17 | Semantic/"smart" code diff; `program.md` graph editor; on-box idea-EV model | Gemini | ⏭️ Defer to roadmap |

## 11.2 Architecture changes (adopted)

### D1 — SSE for streams, WebSocket only for the terminal

v0.1 used WebSockets for everything (`/ws/metrics`, `/ws/events`, `/ws/logs`,
`/ws/chat`, `/ws/gpus`, `/ws/terminal`). Gemini correctly noted that all of
those except the terminal are **one-directional** (server → browser).

**v0.2:** use **Server-Sent Events** for metrics, events, logs, GPU samples, and
agent-chat output. SSE is plain HTTP, the browser's `EventSource` gives
**automatic reconnection with `Last-Event-ID`** for free, and there is no
second protocol/library to manage. The **terminal stays a WebSocket** because it
is genuinely bidirectional. Researcher → agent chat messages are ordinary REST
`POST`s.

Net effect: one streaming abstraction (SSE) for ~90% of realtime, far less
backend code, more robust through proxies. See [doc 08 §8.3](./08-api-and-data-models.md).

### D2 — DuckDB + Parquet, no SQLite metric table

v0.1 had a two-tier metric store (a SQLite `metric_point` table *and* per-run
Parquet). Both reviewers flagged this as needless. **v0.2:**

- **Metrics live only in per-run Parquet** under `data/metrics/`. The `arui`
  ingest service appends to them.
- The backend embeds **DuckDB** to query those Parquet files directly —
  downsampling, range scans, multi-run overlays are all one SQL query over
  Parquet, no service to run.
- **SQLite keeps only the small relational metadata**: `project`, `idea`, `run`,
  `artifact`, `event`, `chat_message`, `gpu`, `tmux_session`, `setting`. The
  `metric_point` table is **deleted**.

One analytical engine (DuckDB) for metrics, one relational store (SQLite) for
metadata, zero extra services. See [doc 06](./06-experiment-tracking.md) and
[doc 08 §8.1](./08-api-and-data-models.md).

### D5 — `ttyd` for the terminal

v0.1 specified a hand-rolled Python `pty` → WebSocket → xterm.js bridge. **v0.2**
uses **`ttyd`** — a tiny, battle-tested binary that serves a full xterm.js
terminal over a WebSocket. The orchestrator launches one `ttyd` per session
(`ttyd -W tmux attach -t <session>`); the dashboard embeds it in a styled pane.
This deletes the entire custom PTY-bridge component while giving a *better*
terminal (resize, scrollback, copy/paste all handled). `setup.sh` installs the
`ttyd` binary.

### D8 — Email: Resend default, SMTP advanced

v0.1 made SMTP and Resend equal. For a solo researcher, **Resend** is the
lower-friction path (one API key, a working test domain, generous free tier).
**v0.2:** Resend is the recommended/default field in onboarding; SMTP moves into
the "Advanced" accordion for those who insist. Less to configure, less to
support.

### D15 — Keep `uv` (rejected Gemini's Poetry suggestion)

Gemini suggested replacing `uv` with Poetry for maturity. **Rejected.** The
reference repo (`zero_order_diffusion_autoresearcher`) uses `uv`, the experiment
repo the agent generates should match it, and by mid-2026 `uv` is mature and
dramatically faster. Consistency with the ecosystem the agent operates in
outweighs Poetry's familiarity. `uv` stays for both the product and the
experiment repo.

### D16 — Keep the "never idle a GPU" guarantee

OpenAI suggested gutting the scheduler to a dumb 1:1 map with no idle detection.
**Partially rejected.** One-run-per-GPU *is already* the v0.1 design — there is
no concurrency complexity to cut. And "never waste a GPU" is a hard requirement
from the brief and a core dollar-saving value prop. **Kept**, but the
*mechanism* is reaffirmed as deliberately simple: when a GPU's run exits, launch
the next ready idea; if none is ready, poke the agent; flag a GPU as idle only
after a short grace period. That is already small. No change beyond making the
simplicity explicit in [doc 05](./05-autoresearch-engine.md).

## 11.3 UI/UX changes (adopted) — the slickness pass

These are folded into [doc 07](./07-dashboard-ui.md). Summary:

- **D3 — Instant client-side data.** A solo researcher accumulates tens to a few
  hundred experiments — trivially small. Load them all once; do **all**
  filtering, sorting, and search on the client with **TanStack Table**. Every
  interaction is zero-latency. Same for Live Graphs: load a generous history,
  let the client slice it. Web Workers handle any heavy decimation off the main
  thread.
- **D4 — Progressive-disclosure onboarding.** The form opens showing only the
  essentials (GitHub token, Claude token, repo name, email, purpose, seed
  ideas). Everything else (consultant tokens, eval details, alert cadence,
  Resend key, passcode) lives in an **"Advanced"** accordion, collapsed. The
  bulk-paste panel still fills *everything* at once and stays pinned at the top.
- **D6 — Micro-interactions.** `framer-motion` everywhere it earns its keep:
  spring transitions on tab/route changes, a soft pulse when a new metric point
  lands, animated row reordering in the idea queue, count-up on headline
  numbers, skeleton shimmers on load. Subtle, never gratuitous.
- **D7 — Guided first run.** A dismissible `react-joyride`/`onborda` tour on
  first dashboard open: points out the GPU strip, the experiment table groups,
  the live graphs, the agent-chat button. ~6 steps. Re-launchable from Settings.
- **D9 — Heatmap cells.** Numeric columns in the experiment table (metric
  result, EV, Δ vs. baseline, peak VRAM) get a subtle background tint scaled to
  value, so outliers and trends pop without reading numbers.
- **D10 — Linked charts.** All live charts share one cursor; hovering one shows
  the crosshair on all. Brushing a range on any chart zooms all charts to that
  range. uPlot's `cursor.sync` does this natively.
- **D11 — Pinnable Overview.** Any chart or metric can be pinned as a tile on
  the Overview, so each researcher curates their own at-a-glance view. Tile
  layout persists (a `setting` row).
- **D14 — A real design system.** A defined, restrained palette (see §11.5),
  route-level **code-splitting** (`React.lazy`), and **list virtualization**
  (TanStack Virtual) for the table, logs, and event feed.
- **Mobile** — a Markdown keyboard-accessory row (bold/italic/list/heading
  buttons) when editing `program.md`/`ideas.md` on a phone.

## 11.4 Killer features added

| Feature | What it is | Why researchers want it |
|---------|-----------|-------------------------|
| **D12 — Research Journal** | An auto-generated, append-only narrative of the project: each day (and each breakthrough) the system writes a plain-language paragraph — "ran 14 experiments, idea X beat baseline by 8%, abandoned direction Y because…", assembled from `ideas.md` analyses and run outcomes. Readable in the UI, included in digests, exportable to Markdown. | Turns a pile of runs into a story. It is the artifact a researcher actually wants when writing a paper or a progress update — and it is nearly free, since the agent already writes per-idea analysis. |
| **D13 — Run Compare** | Select 2–4 runs → a side-by-side view: overlaid metric charts, a config/HPP diff table, the `train.py` diff between them, and headline deltas. | Research *is* comparison. This is the single most-used view in W&B and was only implicit in v0.1. |
| **Fork / re-run** | One click on any run: "fork" it — clone its config, let the researcher tweak a hyperparameter, and queue it as a new idea. | Lets the human inject a quick experiment without writing an idea block by hand. |
| **Granular alert rules** | Beyond the global cadence: simple per-condition rules ("email only if a run beats baseline by >5%", "only if a GPU idle >10 min"). | Keeps email signal high; OpenAI's suggestion, lightly scoped. |

Deferred to the roadmap (interesting, not v1): semantic code diff, an
interactive `program.md` graph editor, and an on-box model that predicts idea EV
or flags anomalous runs (D17).

## 11.5 The v0.2 color & type system

A concrete, restrained system so the build does not bikeshed:

- **Surface:** near-black `#0B0D10` background, raised panels `#14171C`,
  hairline borders `#23272E`.
- **Text:** primary `#E6E8EB`, secondary `#9BA1A8`, muted `#5C636B`.
- **One accent:** electric indigo `#6366F1` for interactive/brand.
- **Semantics, used *only* for meaning:** success/improvement `#22C55E`,
  regression/failure `#EF4444`, running/attention `#F59E0B`, info `#38BDF8`.
- **Heatmaps:** a single-hue ramp (indigo for neutral magnitude; green↔red
  diverging for "vs. baseline" deltas).
- **Type:** Inter (UI), JetBrains Mono (code, metrics, logs, terminal).
- **Motion:** 150–250 ms spring transitions; respect `prefers-reduced-motion`.

This is the palette the scaffold ships with.

## 11.6 Revised, leaner implementation order

Both reviewers converged on roughly the same path. The v0.2 order — optimized
for a **slick demoable build fast**:

1. **M0 — Skeleton.** Repo layout, FastAPI shell serving the static frontend,
   SQLite + SQLAlchemy metadata models, the React app shell with the global
   layout, routing, the design system, and the SSE client. **Ship with a seed
   script that loads realistic demo data** so the UI is buildable and
   demoable before the engine exists.
2. **M1 — Setup script.** `setup.sh` to a running dashboard.
3. **M2 — Onboarding + bootstrap** (progressive disclosure + bulk paste).
4. **M3 — Engine.** The agent loop on **one** GPU first, then the
   keep-all-GPUs-fed scheduler.
5. **M4 — Tracking.** `arui` SDK → ingest → Parquet → DuckDB → SSE.
6. **M5 — The core UI.** Experiments table, Experiment Report, Live Graphs, Run
   Compare — all with client-side filter/sort.
7. **M6 — Terminal (`ttyd`) + agent chat.**
8. **M7 — Notifications + Research Journal.**
9. **M8 — Mobile polish + the guided tour + micro-interaction pass.**

**The very first thing to build:** the M0 skeleton **with the demo-data seed**.
A scaffold that boots into a beautiful, populated dashboard — even on fake data
— de-risks every later milestone and is the thing worth `git clone`-ing on day
one. That scaffold is delivered alongside this spec (see the repo root:
`backend/`, `frontend/`, `arui/`, `setup.sh`, `dev.sh`).

## 11.7 Risks both reviewers flagged

- **Agent unreliability.** Claude Code output is not a stable API. Mitigation:
  the orchestrator treats the experiment repo *files* (`ideas.md`,
  `results.tsv`, `arui` logs) as the source of truth, not parsed tmux text;
  tmux-text parsing is used only for the chat convenience view and is allowed to
  be best-effort.
- **Keeping GPUs fed.** The agent must stay pipelined ahead of the GPUs; if it
  falls behind, GPUs idle. Mitigation: the scheduler explicitly asks the agent
  to pre-implement the next 1–2 ideas (already in [doc 05 §5.4](./05-autoresearch-engine.md)).
- **Chart performance.** Mitigation: uPlot + pre-allocated typed arrays +
  Web-Worker decimation + DuckDB-side downsampling so the browser never receives
  more than ~2–4k points/series.
- **Frontend state sprawl.** Mitigation: TanStack Query for all server state,
  Zustand only for thin UI state, the SSE client as the single realtime source.

## 11.8 What did NOT change

The core remains exactly as in v0.1: the two-repo model, the human-edits-Markdown
philosophy, the EV-ranked idea queue, one run per GPU, tmux-per-job, the
single-process backend, Tailscale access, and the `wandb`/`mlop`-compatible
`arui` SDK. The review sharpened the *implementation*; it did not change the
*product*.
