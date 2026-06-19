# 07 — Dashboard UI

This document specifies every screen of the dashboard — the cockpit. It covers
the desktop layout and the iPhone layout for each. The mobile view is *"very
similar in spirit to the laptop version but ready for iPhone usage"* (brief): same
information architecture, same data, re-flowed for a narrow touch screen.

## 7.1 Design language

- **Dense but calm.** A research cockpit shows a lot of numbers; the layout
  should make the *important* ones jump out (current metric vs. baseline, GPU
  utilization, alerts) and let the rest recede.
- **Dark by default**, light theme available. Monospace for code, metrics, and
  logs; a clean sans for everything else.
- **Realtime everywhere.** Anything that can change updates live over WebSocket
  without a refresh; a small connection indicator shows WS health.
- **Built on** TailwindCSS + shadcn/ui, charts in uPlot, terminals in xterm.js
  ([doc 02](./02-architecture.md)).
- **Responsive by breakpoint.** ≥1024px = desktop multi-pane; <1024px = stacked
  single-column mobile. The same React components, re-flowed.

## 7.2 Global shell

**Desktop:** a left **sidebar** with the primary nav, a top **status bar**, and
the routed content area.

- Sidebar: Overview, Experiments, Live Graphs, Terminals, Agent Chat, Files
  (`program.md` / `ideas.md`), Settings.
- Status bar (always visible): project name, run-loop state (running / paused),
  a compact **GPU strip** (one cell per GPU, colored by utilization), count of
  running vs. queued experiments, unread-alerts bell, WS-connection dot.

**Mobile:** the sidebar collapses into a **bottom tab bar** with the five most
used destinations — Overview, Experiments, Graphs, Chat, More (Terminals, Files,
Settings live under More). The status bar becomes a slim header: project name,
GPU strip (scrollable), alerts bell.

## 7.3 Overview (home)

The at-a-glance answer to *"is research going well?"* — the brief's bar of
"tell me in 10 seconds from my phone".

**Desktop layout (top to bottom):**

1. **Headline band** — the current best metric vs. baseline (big number +
   delta + sparkline), number of experiments done, success rate, GPUs in use
   (e.g. "10/10"), loop uptime.
2. **Live graphs row** — 2–3 of the most important metric charts, current runs
   overlaid on baseline ([doc 06](./06-experiment-tracking.md) §6.5).
3. **GPU panel** — per-GPU cards: index, model, current run name, utilization,
   VRAM, temp, a small utilization sparkline. Idle GPUs are flagged amber/red.
4. **Activity timeline** — the most recent `events`: runs started/finished,
   ideas added, keep/discard decisions, alerts, agent messages.
5. **Upcoming queue preview** — the top 5 ideas by EV with a "see all" link.

**Mobile layout:** the same five blocks stacked vertically; the headline band
becomes a swipeable card carousel of key numbers; the live graphs row becomes
one chart with a horizontal pager; GPU panel becomes a horizontally scrolling
strip of compact cards.

## 7.4 Experiments table

The brief's centerpiece: *"Table of experiments of what has been tried:
succeeded, and failed, and also upcoming rank sorted by EV descending."*

One table, three visually distinct groups, in this order:

1. **Running** — currently executing, with a live mini-progress bar (elapsed vs.
   the `program.md` time budget) and the live current metric.
2. **Upcoming** — unstarted ideas, **sorted by EV descending**
   ([doc 05](./05-autoresearch-engine.md) §5.7). Drag-to-reorder to pin manual
   priority. Shows a "ready to launch" badge for ideas whose code is committed.
3. **Completed** — done runs, newest first, split visually by outcome
   (🟢 success / kept, 🔴 failed / discarded, 🟣 unclear, crash). Sortable by
   metric, date, VRAM.

**Columns:** status chip · idea name (`idea_id`) · short description · metric
result vs. baseline (with delta + color) · EV · GPU · duration · peak VRAM ·
git commit. Columns are filterable (by status, by metric range) and the table is
searchable by idea name/description.

A row's **status chip** uses the six-state model from `ideas.md`
(⚪🔵🟡🔴🟢🟣). Clicking **any row** opens the **Experiment Report** (§7.5).

**Mobile:** the table becomes a list of cards. Each card: status chip, idea
name, the metric-vs-baseline delta as the prominent figure, EV, duration. The
three groups are tabs ("Running / Upcoming / Done") so the narrow screen is not
one endless scroll. Reordering the upcoming list uses a long-press drag.

## 7.5 Experiment Report (row detail)

The brief: *"Click on table row to see way more detail … a full report of that
experiment with run logs and graphs compared to baseline."*

A full-page report for one run/idea, with these sections:

1. **Header** — idea name, status, the headline metric vs. baseline, run
   duration, GPU, git commit, links to its tmux session and `arui` run URL.
2. **The idea block** — description, EV, "why", time generated — rendered from
   `ideas.md` ([doc 05](./05-autoresearch-engine.md) §5.3).
3. **Config / HPPs** — the full hyperparameter set used.
4. **Code diff** — the `train.py` diff vs. baseline (or vs. the previous kept
   commit), rendered with a diff viewer. This is how the researcher sees
   *exactly* what the agent changed.
5. **Metric charts** — every logged series, full resolution, overlaid on
   baseline and any pinned prior runs. Realtime if the run is still going.
6. **Images & artifacts** — logged sample grids, plots, downloadable files /
   checkpoints.
7. **Run logs** — the full `run.log`, searchable, with crash stack traces
   highlighted.
8. **Agent analysis** — the `Analysis`, `Conclusion`, and `Next Ideas to Try`
   the agent wrote into the idea block: its reasoning trace for this experiment.
9. **Outcome** — keep/discard, the `results.tsv` line, and which new ideas this
   run spawned (linked).

**Mobile:** the nine sections become an accordion (collapsed by default except
the header and metric charts); charts are full-width and pinch-zoomable; the
code diff renders in a horizontally scrollable monospace pane.

## 7.6 Live Graphs

A dedicated W&B-style workspace for metric exploration ([doc 06](./06-experiment-tracking.md)):

- A **run picker** — multi-select runs to overlay (baseline is pinned by
  default).
- A **metric grid** — one chart per metric key, each overlaying the selected
  runs, all updating live via the `metrics` WebSocket.
- Per-chart controls: log/linear axis, smoothing, x-axis (step / wall-clock /
  relative time), zoom (shared across charts).
- The whole view's state — selected runs, visible keys, zoom — is encoded in the
  URL so it is shareable ([doc 06](./06-experiment-tracking.md) §6.8).

**Mobile:** one chart at a time with a horizontal pager and a compact run-picker
sheet; controls live in a bottom sheet.

## 7.7 Terminals

The brief: *"Able to tmux into sessions and see what's going on"* and *"Launch a
terminal to see what's going on there. Each launch of a training job should be
on a tmux so you can access it and see the logs. You should be able to click
from a list and see them."*

- A **session list** — every tmux session ([doc 05](./05-autoresearch-engine.md) §5.5):
  `agent`, every `train-gpu{N}`, every `term-{uuid}`. Each row shows kind, the
  associated run/idea, GPU, and uptime.
- Clicking a session opens a **full xterm.js terminal** attached to it over the
  terminal WebSocket — a real interactive `tmux attach`, scrollback included.
- A **"New terminal"** button spawns a fresh `term-{uuid}` session for ad-hoc
  work.
- Read-only vs. interactive: training-job and agent sessions are interactive by
  default but can be opened read-only to avoid fat-fingering a live run.

**Mobile:** the terminal is usable but explicitly secondary — xterm.js with a
mobile keyboard accessory row (arrows, Ctrl, Tab, Esc). Most mobile users will
prefer Agent Chat; the terminal is there for emergencies.

## 7.8 Agent Chat

The brief: *"Ability to talk to the researcher in charge."* See
[doc 05](./05-autoresearch-engine.md) §5.9.

- A familiar chat interface with the Principal Researcher. Researcher messages
  are injected into the `agent` tmux session; the agent's replies are parsed
  back out and shown as bubbles. History persists (`chat_message` table).
- Quick-action chips above the composer: "Status update", "What are you working
  on?", "Skip the current idea", "Prioritize idea …", "Pause the loop".
- A toggle to drop from chat into the **raw `agent` terminal** for the same
  session.

**Mobile:** this is the *primary* steering surface on a phone — a full-height
chat view, the quick-action chips horizontally scrollable above the keyboard.

## 7.9 Files — `program.md` & `ideas.md`

Because the human's leverage is Markdown ([doc 01](./01-product-overview.md),
principle #2), these two files are first-class:

- **`program.md`** — rendered Markdown with an **Edit** mode (a Markdown
  editor). Saving writes the file and offers to tell the agent to re-read it
  ([doc 05](./05-autoresearch-engine.md) §5.8).
- **`ideas.md`** — rendered as the structured list of idea blocks (the same data
  as the Experiments table, in document form). The researcher can add a new
  idea block via a guided form (fills the template) or edit raw Markdown.
- Both show a **history** (git log of the file) so the researcher can see how
  the agent has evolved `ideas.md` over time.
- Read-only views of `train.py`, `prepare.py`, the `.toml`, and `results.tsv`
  round out the file browser.

**Mobile:** rendered view by default; editing is supported but the guided
"add idea" form is the recommended path on a phone.

## 7.10 Settings

Edits the post-onboarding config ([doc 04](./04-onboarding-and-agent-bootstrap.md) §4.9):

- **Notifications** — email address, alert/digest cadence, SMTP/Resend config,
  a "send test email" button.
- **Research config** — edit purpose / seed-ideas / eval-spec / baseline-methods
  files; choose whether to have the agent re-read them.
- **Tokens** — rotate GitHub / Claude / Gemini / OpenAI tokens (masked).
- **Access** — change or clear the dashboard passcode (clear = open dashboard),
  toggle Tailscale Funnel (public access) on/off.
- **Scheduler** — idle thresholds, run-timeout grace, concurrency policy,
  metric retention.
- **Danger zone** — pause/resume the loop, restart the agent, stop everything.

**Mobile:** a standard grouped settings list.

## 7.11 Bootstrap & Onboarding views

Covered in [doc 04](./04-onboarding-and-agent-bootstrap.md): the onboarding form
(§4.2) and the live Bootstrap stepper (§4.8). Both are fully responsive; the
onboarding form on mobile is one long single-column scroll with the bulk-paste
panel pinned at the top.

## 7.12 Alerts & notifications surface

- The **bell** in the status bar opens an alerts panel: crashes, idle GPUs,
  agent-down, breakthroughs, with severity and links to the relevant run/report.
- Alerts mirror what is emailed ([doc 09](./09-notifications-and-security.md)) so
  the in-app and email channels never disagree.
- A breakthrough (a run that beats the best metric by a configurable margin)
  gets a celebratory in-app toast as well as an email.

## 7.13 Empty, loading, and error states

- **Pre-onboarding:** every route redirects to onboarding.
- **During bootstrap:** every route shows the Bootstrap view until Step 6.
- **No runs yet** (post-bootstrap, baseline still running): the Experiments
  table shows the baseline as the only "Running" row; graphs show "waiting for
  first metrics".
- **WS disconnected:** a non-blocking banner; views fall back to REST polling
  and reconnect automatically.
- **Agent down:** a prominent banner with a "restart agent" action, mirroring
  the `agent_down` alert.

## 7.14 Accessibility & performance targets

- Keyboard-navigable; shadcn/Radix gives accessible primitives for free.
- First meaningful paint of the Overview in <1.5 s on a phone over the tailnet.
- Charts stay at ~60 fps with 10 overlaid runs of 100k points each (uPlot +
  decimation).
- The whole frontend is a static bundle served by FastAPI — no SSR, no Node
  runtime needed on the node at serve time.
