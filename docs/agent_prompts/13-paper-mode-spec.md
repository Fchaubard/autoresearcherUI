# Paper Mode spec — research → ablation/writeup workflow

Status: **draft v1, pre-review.** To be reviewed by Gemini 2.5 Pro and
GPT-5 high before any implementation. No code written yet.

## Problem

Today autoresearcherUI is a one-mode tool: the agent runs experiments
forever, the council reviews each batch, lessons accumulate, the
frontier inches forward. There is no built-in transition from "still
exploring" to "we have something publishable — now prove it rigorously
and write it up." Researchers do this transition by hand: spin up a
LaTeX project, plan ablations, run them on a different schedule, then
write. That's where 90% of the writing-shaped work happens, and it is
exactly the kind of structured, queue-able process the autoresearcher
is good at — if we model it.

## Goals

1. A first-class **mode toggle** at the top of the Dashboard: `Research`
   ↔ `Paper`. Research mode is what we have today. Paper mode is the
   new state where the agent stops exploring and starts proving.
2. **Pre-flip honest council review.** Before letting the user flip
   into Paper mode, every available council member independently assesses
   whether the project has enough of an idea to write up. Their full
   opinions (including dissent) are shown to the user — we do not
   synthesize away disagreement here.
3. A new **Author Agent** running in its own tmux session that:
   - reads the project's lessons / frontier / metrics,
   - generates a NeurIPS-formatted v0 paper (LaTeX) with claims,
   - decomposes each claim into a list of required experiments
     (ablations + baselines + scaling + cross-dataset),
   - keeps a `paper_runs.md` queue with status per run,
   - rebuilds the paper draft as runs complete.
4. A new **"Write the paper"** tab (replacing the current Latex tab)
   with a Dashboard-style layout — paper preview on top, figure/run
   plan below, right-rail with summary / Author Agent terminal / tmux
   sessions.
5. A **PDF / LaTeX toggle** above the paper preview. Switching to PDF
   triggers a recompile if anything changed.
6. A **Gantt chart** estimating time-to-paper from GPU count, time per
   step, max steps per queued run.
7. A **reversal flow** so the user (and the council) can flip back to
   Research mode when ablations aren't panning out — without losing the
   paper draft.

## Non-goals (v1)

- Editing the LaTeX in-app. The user can review & download but edits
  happen via the Author Agent's commits. (v1.1: in-place edits.)
- Conference-specific style files beyond NeurIPS. (Author Agent can
  swap in others later via Settings.)
- Submitting to arXiv / conferences. We just produce the PDF.
- Co-authoring with multiple humans. Single-user instance.

## A single mode is a project-wide property

We add `setting.project_mode = "research" | "paper"` with a small
state machine:

```
              ┌───────────────────────┐
              │      research         │  ← default
              │   (current behavior)  │
              └──────────┬────────────┘
                         │ user clicks toggle
                         │ ↓
                  council novelty
                  assessment (modal)
                         │
            ┌────────────┴────────────┐
        keep researching          proceed
            │                          │
            ↓                          ↓
        (no change)         ┌───────────────────────┐
                            │       paper           │
                            │ (Author Agent + UI)   │
                            └──────────┬────────────┘
                                       │ revert via UI
                                       │ OR auto-trigger
                                       │ from ablation
                                       │ health monitor
                                       ↓
                                 (back to research,
                                  paper draft kept
                                  in /paper/ folder)
```

`research ↔ paper` is round-trippable — every transition is recorded in
a `mode_history` table so we can see "switched to paper 2026-06-03,
back to research 2026-06-10 after 4 failed ablations." The paper
draft is **never deleted** on revert; it's just paused.

We do not currently plan a third "submitted" state — the user can mark
the paper done by archiving the project the normal way.

## Mode toggle UX (Dashboard)

A segmented control in the top bar to the right of the project name:

```
  autoresearcherUI   my-project   [ Research | Paper ]   ↓0.0319
```

Clicking the inactive side fires the appropriate transition modal.
The pill background tints accent-purple in Research mode and a calmer
green in Paper mode so the user always knows which mode they're in
across every screen (header is global).

## Pre-flip novelty assessment (the "are we ready" modal)

Triggered when the user clicks `Paper` from Research mode. Flow:

1. **Confirmation step.** Modal:
   *"You think we're ready to turn this research into a paper?
   I'll consult the council. They'll be honest."*
   Buttons: `Cancel · Analyze`.

2. **Council round-robin.** On `Analyze`, we kick off all available
   council members in parallel (Gemini, OpenAI, Claude). Each gets the
   same context bundle:
   - project purpose, baseline, metric, direction
   - lessons.md (full)
   - frontier of kept runs (every improvement, in order, with the
     run's `what` field if present)
   - aggregate stats (n by status, plateau length, last new best when)
   - a snippet of the agent's `program.md`

   The system prompt asks for an HONEST assessment:
   - What are the strongest 2-3 claims this project could support?
   - For each claim: is the evidence (a) strong / (b) suggestive /
     (c) anecdotal?
   - Novelty: is any of this likely publishable at NeurIPS-tier,
     workshop-tier, or "not yet"? With justification.
   - Top 3 red flags / missing evidence we'd need to address before
     writing.
   - Recommendation: `proceed_to_paper` | `keep_researching` | `pivot`.

3. **Display all opinions, do not collapse them.** The modal grows to
   show one column per reviewer with their assessment + recommendation.
   At the bottom: a count summary (e.g. "2 / 3 say proceed").

4. **User decides.** Buttons: `Keep researching · Proceed to Paper Mode`.
   `Keep researching` closes the modal, no state change. `Proceed to
   Paper Mode` triggers the actual flip (next section).

5. The full transcript of step 2 is persisted in `mode_history` so we
   can later show "the council said X before we entered paper mode".

## The Paper Mode flip

When `Proceed to Paper Mode` is clicked:

1. Set `setting.project_mode = "paper"`.
2. **Pause research.** For every currently-running tmux session that
   matches a research run, send a soft `SIGTERM` (after a brief grace
   we send `SIGKILL`). Status of any in-flight runs is marked
   `paused_for_paper`. Their checkpoints are preserved.
3. **Suppress research-only sub-systems** while paper mode is active:
   - PI agent → off
   - Council per-run review → off
   - Council strategic review → off (we don't want it queueing new
     research ideas)
   - GPU-saturation nudge → off
4. **Spawn the Author Agent** (next section) in a new tmux session.
5. Navigate the UI to the **Write the paper** tab.
6. Add an `Event(type='mode_changed', message='entered paper mode')`.

## The Author Agent

A new tmux session named `author` running a Claude Code (or
configurable) agent with a focused system prompt:

> You are the author of a NeurIPS paper based on this research project.
> The research is at the state captured below. Your job is to:
> 1. Decide the strongest 2-3 claims this project can defend.
> 2. Write a v0 of the paper in LaTeX using the NeurIPS 2025 style.
> 3. Decompose every claim into the specific experiments needed:
>    main results, ablations, scaling curves, cross-dataset checks,
>    baselines you must beat.
> 4. Maintain `paper_runs.md` (analogous to `ideas.md`) with the run
>    queue. Schema: `| status | run_id | figure_id | dataset | model |
>    hpps | num_max_steps | est_time | claim_id |`.
> 5. Maintain `paper_figures.md`: `| fig_id | title | type | runs[] |
>    status | path |` (one row per figure or table in the paper).
> 6. When ablations finish, update the LaTeX with the numbers and
>    re-render the figures. Commit each pass.
> 7. Be ruthless about killing claims the data does not support. If
>    every ablation regresses, say so in the draft's discussion and
>    recommend the user revert to research mode.

The Author Agent writes into `data/workspace/<repo>/paper/` so the
research workspace and the paper workspace are siblings. Files:

```
paper/
├── main.tex                     # NeurIPS template
├── neurips_2025.sty             # bundled with us
├── refs.bib
├── sections/
│   ├── 00_abstract.tex
│   ├── 01_introduction.tex
│   ├── 02_related.tex
│   ├── 03_method.tex
│   ├── 04_experiments.tex
│   └── 05_conclusion.tex
├── figures/                     # output of plot scripts
│   ├── fig1_progress.pdf
│   └── ...
├── paper_runs.md                # the run queue
├── paper_figures.md             # the figure plan
├── build/                       # latexmk output
└── claims.md                    # explicit claim list + evidence pointer
```

A small backend service (extending the existing arui SDK ingest) runs
`latexmk -pdf -interaction=nonstopmode` whenever the user opens the
PDF tab (debounced to ≤1× / 5s). Output: `paper/build/main.pdf`.

## The "Write the paper" tab

Renamed from `Latex`. Same layout shape as the Dashboard:

```
┌──────────────────────────────────────────────────────────────┬───────┐
│  HERO — paper viewer                                          │       │
│  [ LaTeX | PDF ]      ⟳ recompile   ⤓ download                │  RAIL │
│  ┌────────────────────────────────────────────────────────┐  │       │
│  │                                                         │  │ Sum-  │
│  │   live preview of main.pdf or main.tex                  │  │ mary  │
│  │                                                         │  │       │
│  │                                                         │  │ Auth- │
│  └────────────────────────────────────────────────────────┘  │ or    │
│                                                              │ Agent │
│  STATS — claims · figures planned · runs queued · ETA        │       │
│                                                              │ tmux  │
│  PAPER PLAN — table                                          │       │
│  ┌────────────────────────────────────────────────────────┐  │       │
│  │ Fig 1 · main result (table)         · 4 runs · 12h     │  │       │
│  │   ├─ run_a · imagenet · ours        · queued · 3h      │  │       │
│  │   ├─ run_b · imagenet · baseline    · done   · 2.5h    │  │       │
│  │   └─ run_c · cifar    · ours        · running·  …      │  │       │
│  │ Fig 2 · scaling curve (plot)        · 6 runs · 20h     │  │       │
│  │   ├─ ...                                               │  │       │
│  │ Gantt (collapsible) ▾                                  │  │       │
│  └────────────────────────────────────────────────────────┘  │       │
└──────────────────────────────────────────────────────────────┴───────┘
```

### Paper viewer (HERO)

- **`[ LaTeX | PDF ]`** segmented toggle at top-left of the hero.
- LaTeX view: concat of `main.tex` + `sections/*.tex` in canonical
  order, rendered in a read-only code view with syntax highlight.
- PDF view: an `<iframe>` of `paper/build/main.pdf`. Switching from
  LaTeX → PDF triggers a recompile if any `.tex` file has a newer
  mtime than `main.pdf`; spinner overlay while compiling.
- `⟳ recompile` forces a rebuild.
- `⤓ download` serves the latest PDF.

### Stats strip

Same shape as the Dashboard stats. Four cards:
- `Claims` (N) — distinct entries in `claims.md`
- `Figures planned` (M / total)
- `Runs queued / done / running`
- `ETA` (rolled up from `paper_runs.md` × time-per-step × steps ÷ GPUs)

### Paper plan table

Top level: one row per figure/table from `paper_figures.md`.
Expandable to show the rows from `paper_runs.md` whose `figure_id`
match (this is why the schema cross-references — same join as
runs ↔ ideas in research mode).

Columns:
- `status` chip (queued / running / done / failed / blocked)
- name / description
- `dataset` · `model` · `hpps`
- `num_max_steps` · `est_time`
- `headline_metric` (filled when the run completes)

Click a run row → existing run drawer (same code).

### Gantt

Bottom of the paper plan table, collapsible:
- X-axis: hours from now
- Y-axis: each queued run as a bar
- Bars stacked into rows = GPU index (so we can see GPU-utilisation
  per the schedule)
- Color = status (queued/running/done/failed)
- Vertical "now" line; horizontal target-submission line if the user
  sets one in Settings
- "Estimated paper-ready" timestamp at the top

The Gantt is the user's daily heartbeat: "two days till everything
needed for Figure 3 is done."

### Right rail

Same shape as the Dashboard rail, three tabs:
- **Summary** — a chat-feed of Author Agent updates and key events
  (claim added, figure drafted, ablation kept, baseline regressed).
  Reuses the Summary card components from the Dashboard.
- **Author Agent** — live tmux terminal of the `author` session.
  Replaces the "Research agent" tab.
- **Sessions** — every ablation run's tmux session (same as Dashboard
  Sessions tab).

A composer at the bottom lets the user type messages to the Author
Agent, identical to the research chat composer.

## Run-list management

`paper_runs.md` is the authoritative queue, owned by the Author Agent.
Status values:
- `queued` — not started
- `blocked` — waiting on another row (e.g. checkpoint from row X)
- `running`
- `done` — metric logged, integrated into the paper
- `failed` — diverged or crashed; the agent decides whether to retry
- `discarded` — agent or user vetoed it

The arui SDK is extended with a `paper_run_id` field on `init()` so
when an ablation run finishes, the backend can look up its row in
`paper_runs.md` and update the status atomically. Figure rendering
uses the existing matplotlib charts module — we add a `figure_id`
parameter so plots can be saved straight into `paper/figures/`.

## Spinner verbs (UX while the agent thinks)

While the Author Agent is doing long-running things, the rail's
Summary shows a live spinner-line:

- "Distilling claims from the lessons…"
- "Drafting v0 of the paper…"
- "Picking ablations for claim 2…"
- "Estimating time per run…"
- "Compiling main.pdf…"
- "Re-rendering Figure 3 with the new ablation…"
- "Reading TRM ablation results to update §4…"

These come from an existing `chat_message` channel with a new
`role='spinner'` (auto-cleared when the next concrete event lands).

## Reversal — the hard part

Two paths back to Research mode:

### A. User-driven revert

The mode toggle in the header always works. Clicking `Research` from
Paper mode triggers a confirmation modal:

> Going back to Research means the Author Agent stops, the paper
> draft is paused, and the research agent resumes. The paper folder
> stays — you can flip back later.

If they confirm:
1. Set `project_mode = "research"`.
2. Send SIGTERM to the `author` tmux session.
3. Append a `mode_history` record with the current paper state hash.
4. Re-enable PI / council per-run / council strategic.
5. Resume the research agent in a new tmux session, with a startup
   prompt that includes the paper-mode lessons: "you spent N hours in
   paper mode; here's what the ablations showed; the council's
   recommendation is to focus on direction X."
6. Navigate to Dashboard.

### B. Automatic suggestion

A new background watcher fires hourly while in Paper mode. It runs
the council on the LAST window of paper_runs:

- If ≥40% of completed `paper_runs` in the last 24h `failed` OR
  regressed vs baseline → emit a Summary card:
  *"3 of your last 7 ablations regressed. Council recommends reverting
  to research. Two options: [Revert] [Keep going]."*
- If the user ignores 3 such cards in a row, the watcher escalates
  to an email (using the existing notify pipeline) instead of more UI.

Critically: **the watcher never reverts on its own.** Users hate
software that undoes their decisions. We surface the signal and let
them choose.

### What survives a reversal?

- `paper/` folder: kept, untouched.
- `paper_runs.md`: kept; runs in `running` state are marked `paused`.
- `lessons.md`: gets a special block appended: `## Paper mode
  attempt 1 (2026-06-03 → 2026-06-10): what we learned …` written
  by the council before reverting.
- The author's tmux session: killed.
- The agent's tmux session: respawned with full prior context.
- `mode_history`: a new entry with both timestamps and the council's
  reason.

## Data model additions (rough)

New `Setting` rows:
- `project_mode` = "research" | "paper"
- `paper_neurips_year` = "2025" (which sty file to use)
- `paper_target_date` = ISO timestamp or null
- `paper_run_concurrency` = int (max simultaneous ablations; default
  = GPU count)

New table `mode_history`:
- `id`, `from_mode`, `to_mode`, `at`, `reason`, `council_transcript_json`

New endpoints:
- `POST /api/paper/assess` — runs the council novelty review,
  returns one assessment per reviewer (non-streaming for v1; modal
  shows a per-reviewer spinner while we wait).
- `POST /api/paper/enter` — flip to paper mode (kills research,
  spawns Author Agent).
- `POST /api/paper/revert` — flip back.
- `GET /api/paper/runs`, `GET /api/paper/figures`,
  `GET /api/paper/claims` — JSON projections of the markdown files.
- `POST /api/paper/recompile` — kick latexmk.
- `GET /api/paper/pdf` — serve the latest PDF.
- `GET /api/paper/tex` — concat of main.tex + sections for the
  LaTeX viewer.
- `GET /api/paper/gantt` — JSON for the Gantt chart.

## Open questions (for the reviewers)

1. **Should "paper mode" be one-way until completion?** Allowing
   round-trips is friendly but adds state-management complexity. Or
   should we require the user to "Abandon paper" (and zip it as an
   archive) before re-entering research?
2. **Author Agent ↔ research agent: same Claude session or different?**
   Different is cleaner (separation of concerns) but doubles the LLM
   cost. Pros/cons?
3. **Latex viewer: read-only or editable?** Editable means we deal
   with merge conflicts when the Author Agent commits while the user
   is editing. Read-only is simpler but feels limited.
4. **PDF rendering: server-side latexmk vs. client-side something
   like pdf.js?** latexmk is heavy (texlive ≈ 5GB) but produces real
   PDFs. Alternative: render LaTeX → HTML via mathjax + custom CSS
   for previewing, only build the real PDF on download.
5. **Reversal heuristic threshold.** 40% failure rate in 24h — is
   that the right knob? Or should it be a council-only call?
6. **Gantt accuracy.** Time-per-step estimates require historical
   data per (dataset, model). What if we're running an ablation
   on a never-seen-before config? Default to the maximum observed
   time per step? Average? Skip the row in the Gantt?
7. **Cost.** We add a new Claude session per project for the Author
   Agent. With the strategic-review batch already on every wave,
   total LLM spend grows. Worth a single "Author Agent budget" knob
   in Settings?
8. **What if the council disagrees sharply on novelty?** (2 say
   proceed, 1 says nope, all with strong arguments.) Today the UI
   just shows all three. Should we add a tiebreaker (a 4th model,
   or claude as judge as we did for the per-run council)?

## Acceptance tests (v1 done)

1. Toggle to Paper from a project with no kept runs → modal warns
   "no kept improvements yet, council unlikely to recommend proceed";
   council still runs and confirms.
2. Toggle from a project with substantive frontier → council shows
   3 distinct opinions; user proceeds; research stops within 5s;
   Author Agent shows up in tmux `author`; Write-the-paper tab loads
   with a v0 `main.tex` rendered as PDF within 90s.
3. While in paper mode, agent finishes a queued ablation → table
   row flips to `done` within 5s; figure regenerates; PDF
   re-renders on next tab visit.
4. While in paper mode, click `Research` in header → modal warns;
   on confirm, author tmux dies, research tmux respawns, dashboard
   reloads, mode_history shows both transitions.
5. Auto-watcher in paper mode: after 3 consecutive regressed
   ablations, Summary card "council recommends reverting" appears.
6. Gantt updates within 2s of a new run being added to
   `paper_runs.md`.

## Out of scope but worth noting

- Multi-author / multi-tab collaboration.
- Inline LaTeX editing.
- Auto-submission to arXiv.
- Image generation for paper figures (we use the existing matplotlib
  pipeline; later we could add a `figures/auto/` for diagrams).
