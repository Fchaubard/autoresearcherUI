# autoresearcherUI v0.3 — Redesign Plan (for external review)

## Context
autoresearcherUI is a self-hosted cockpit for autonomous AI research (Karpathy's
"the human edits program.md, the agent edits train.py" loop, productized). v0.2
shipped a working FastAPI backend, an orchestrator that runs the autonomous
research loop, the `arui` experiment tracker (DuckDB-backed), a green e2e
integration test, and a dashboard. It is deployed on a 10×A40 node and reachable
on a public URL via cloudflared.

Two pieces of feedback from the researcher who will use this:
1. "The UI looks fake — this is not real data." (The dashboard runs in a DEMO
   mode: hand-seeded fake project + a simulator faking live metrics.)
2. "The UI is terrible." Specifically: it has left-nav tabs splitting things
   across pages; there is no Karpathy-style progress plot; the runs/ideas table
   is weak; the chat is a separate page.

## Goal of v0.3
1. **Real data only.** Kill the fake seed + simulator. The dashboard shows only
   actual output of the orchestrator running real experiments.
2. **A single-page, researcher-grade UI.** No left-nav tabs. Everything a
   researcher needs on one page.

## A. Make the data real
- Remove `seed.py` (fake `bs1learning` data) and `sim.py` (fake live metrics)
  from the default path.
- The dashboard renders only what the orchestrator actually produced: real
  runs, real metrics logged via the `arui` SDK into DuckDB, real keep/discard
  decisions from real result comparisons.
- The orchestrator runs a real research project on startup. For a fast,
  hardware-free, genuinely-real loop we expand the example project so it has
  many real hyperparameter experiments — each a real `train.py` subprocess with
  real training, real metrics, real outcomes — so the progress plot is meaty
  (dozens of experiments) like Karpathy's.
- The e2e integration test remains the real merge gate: it runs the orchestrator
  end-to-end and asserts the full pipeline (it already launches real `train.py`
  subprocesses; no fakery).

## B. The single-page UI

Everything on ONE scrolling page. No left-nav tabs. Layout top to bottom:

1. **Header bar** — project name, core metric name, run-loop status, a compact
   per-GPU utilization strip, counts (running / queued / done).

2. **THE main plot (hero) — the Karpathy-style progress chart.**
   - x = experiment number; y = the core validation metric.
   - A step-function "running best" line.
   - Kept improvements = solid dots, each annotated with the idea name.
   - Discarded experiments = faint gray scatter dots.
   - Hover any point → tooltip with run name, metric value, delta vs baseline,
     status. Click → opens that run's detail.
   - Title like "Autoresearch Progress: N experiments, K kept improvements".
   - This is the centerpiece — the first thing the researcher sees.

3. **Stat row** — best metric vs baseline, experiments done, success rate,
   GPUs in use.

4. **Idea queue + runs table (unified).** One clean table of ALL runs and queued
   ideas. Columns: status, idea name, result vs baseline, EV, GPU, duration.
   Filterable by status (running / kept / discarded / queued). Searchable by
   name. Upcoming ideas shown EV-ranked at the top. Click any row → a drawer
   with that run's wandb-style metric plots (train loss, val metric, …), its
   config/HPPs, the `train.py` diff, and the agent's analysis.

5. **Floating chat bubble** — a support-widget-style bubble pinned bottom-right.
   Click → expands to a chat panel overlaying the page. Talk to the Principal
   Researcher agent without leaving the page or losing context.

Design: dark, dense, fast. Realtime updates via SSE. Canvas charts (uPlot-class).

## Questions for the reviewer
1. Is the single-page, scroll layout right for a researcher running 80+
   overnight experiments? What belongs above the fold?
2. The progress plot is the hero — how do we make it maximally useful?
   Annotations without clutter, hover, comparing runs, log scale, zoom?
3. The unified runs table + drill-in drawer — what columns, filters, and
   drill-ins matter most to a working ML researcher?
4. What is missing entirely that such a researcher would want at a glance?
5. Risks of killing the demo data: the empty-state UX before the first real
   experiments complete — how should the page look at t=0?
6. The chat bubble — keep it minimal, or should it surface proactive agent
   updates/alerts?
