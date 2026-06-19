# Paper Mode v3 — holistic workflow spec

Status: **draft v3**, organized around the actual day-to-day of paper
writing rather than around features. Supersedes v2 (`13-paper-mode-spec-final.md`).
v2's architecture (Paper Runner, Author Agent contract, DB-backed runs/
figures/claims, override-files for editing, claim-coverage default
view, async pre-flip council, mode round-trip with snapshot) is
preserved here verbatim — this doc adds the workflow scaffolding
around it that v2 missed.

## The mental model

Paper Mode should feel like **a research partner who lives in your
project** — not a wizard that builds a paper once and then disappears.
The researcher opens it every morning, sees what happened overnight,
makes 2-3 decisions, queues some runs, and gets back to work. The
agent is constantly thinking. The user is the editor-in-chief.

Three rhythms layered on top of each other:
- **Hourly**: ablations finish, the agent integrates them and updates
  the draft, council reviews the new evidence, plots regenerate.
- **Daily**: user reviews decisions, edits text, queues new runs,
  watches budget burn.
- **Weekly**: lit search refresh, reviewer simulation, version pin
  ("v1 — submitted to internal review").

The whole UI is built around making these three rhythms cheap.

## End-to-end journey (the spec follows this order)

```
  ┌──────────────────────────────────────────────────────────────┐
  │  Phase 0 — RESEARCH (existing)                               │
  │  ideas.md, frontier grows, lessons.md accumulates            │
  └─────────────┬────────────────────────────────────────────────┘
                │
                │ user flicks 'Paper' toggle
                ▼
  ┌──────────────────────────────────────────────────────────────┐
  │  Phase 1 — PAPER PROPOSAL  (async)                           │
  │  council asks "is this publishable?" — proposal artifact     │
  └─────────────┬────────────────────────────────────────────────┘
                │ user accepts
                ▼
  ┌──────────────────────────────────────────────────────────────┐
  │  Phase 2 — ONBOARD THE PAPER  (one-time setup)               │
  │  venue, deadline, authors, page limit, anonymization, budget │
  └─────────────┬────────────────────────────────────────────────┘
                │
                ▼
  ┌──────────────────────────────────────────────────────────────┐
  │  Phase 3 — SCAFFOLD  (~hours, async)                         │
  │  Author Agent: claims, lit search, v0 LaTeX, figure plan,    │
  │  paper_runs queue, related-work draft                        │
  └─────────────┬────────────────────────────────────────────────┘
                │
                ▼
  ┌──────────────────────────────────────────────────────────────┐
  │  Phase 4 — DAILY LOOP (the bulk of the time)                 │
  │   ┌────────────────────────────────────────────────────────┐ │
  │   │  open tool → "Today" view → decisions → edits → done   │ │
  │   │      ↑                                          │      │ │
  │   │      └──────────────────────────────────────────┘      │ │
  │   │  meanwhile: Paper Runner schedules, Author Agent       │ │
  │   │  integrates results, council reviews                   │ │
  │   └────────────────────────────────────────────────────────┘ │
  └─────────────┬────────────────────────────────────────────────┘
                │
                ▼
  ┌──────────────────────────────────────────────────────────────┐
  │  Phase 5 — REVIEWER SIMULATION  (1 week before deadline)     │
  │  council reads paper, writes fake review, recommends         │
  │  defensive ablations; user approves additions to queue       │
  └─────────────┬────────────────────────────────────────────────┘
                │
                ▼
  ┌──────────────────────────────────────────────────────────────┐
  │  Phase 6 — SUBMISSION                                        │
  │  anonymize, bundle, checklist, version-pin "v1 submitted"    │
  └─────────────┬────────────────────────────────────────────────┘
                │
                ▼
  ┌──────────────────────────────────────────────────────────────┐
  │  Phase 7 — REBUTTAL  (1-2 weeks, new sub-mode)               │
  │  reviewer comments imported, mapped to new ablations,        │
  │  rebuttal letter drafted alongside paper revisions           │
  └─────────────┬────────────────────────────────────────────────┘
                │
                ▼
  ┌──────────────────────────────────────────────────────────────┐
  │  Phase 8 — CAMERA-READY → ARCHIVE                            │
  └──────────────────────────────────────────────────────────────┘
```

Each phase has its own UI emphasis (different "default" sub-tab) but
the data model is one continuous evolution. The mode toggle moves
you between research and paper; phases inside paper mode are
implicit — driven by the project's state, not a manual switch.

---

## Phase 2 — Onboard the paper

After the user accepts the Paper Proposal but BEFORE the Author Agent
starts writing, a one-time onboarding modal collects what the council
can't infer:

```
┌─ Onboard the paper ─────────────────────────────────────────────┐
│ Target venue       [ NeurIPS 2026 ▾ ]                            │
│ Submission style   [ Long paper (9p + ref) ▾ ]                   │
│ Submission deadline [ 2026-05-22 19:00 UTC ]                     │
│ Anonymization     [✓] for review                                 │
│                                                                  │
│ Authors           Francois Chaubard        (Stanford)            │
│                   ☐ + add co-author                              │
│                                                                  │
│ Compute budget    [ 800 ] GPU-hours total                        │
│ Daily LLM budget  [ $20 ] for Author Agent                       │
│                                                                  │
│ Title preference  [ let agent propose ▾ ]                        │
│ Paper folder      paper/  (in workspace, git-init'd)             │
│                                                                  │
│            [ Save & start ]   [ Cancel ]                         │
└──────────────────────────────────────────────────────────────────┘
```

Persisted to a new `paper_meta` table:
- `venue`, `style_id` (NeurIPS short/long, ICML, CVPR, workshop, …)
- `deadline_iso` — drives the "days till submission" counter everywhere
- `anonymize` — flips the LaTeX template into anonymous mode
- `authors[]` — name + affiliation + ORCID
- `gpu_budget_hours`, `llm_budget_daily_usd`
- `title_preference` — string or "auto"

These show on the header pill: `📝 NeurIPS 2026 · 19d · 142/800 GPU-h`.

---

## Phase 3 — Scaffold

The Author Agent's first pass. Spinner activity (visible in the rail):

1. **Claim distillation** — picks N claims from the council's proposal +
   project lessons. Writes `claims.md` (which projects into the
   `paper_claim` table).
2. **Lit search** — kicks off the new Lit Agent (sub-agent that
   queries arxiv + Google Scholar + Semantic Scholar). Pulls ~30
   relevant papers, writes `related_work_candidates.md`, and asks
   the user (decision queue) "which 12 should we cite?"
3. **v0 LaTeX** — fills in `main.tex` + every `sections/*.tex` using
   the venue's style. Most sections are XX-placeholders.
4. **Figure plan** — populates `paper_figure` rows; each links to
   the claim(s) it supports.
5. **Run queue** — populates `paper_run` rows from the figure plan,
   including baselines, ablations, seed bundles.
6. **Cost + time estimate** — pulls throughput from the existing
   `metrics` history; flags any planned runs over budget.

Output by end of Phase 3 (typically ≤30 minutes):
- A v0 PDF the user can read.
- A populated Claim Coverage matrix with most cells `missing` or
  `queued`.
- A decision queue with the lit-search "which to cite?" question
  on top.

---

## Phase 4 — The Daily Loop (the heart of this spec)

This is where users will spend 95% of their time. The home view is a
new sub-tab: **Today**.

### The "Today" view

A single scrollable column, with these blocks in order:

#### 1. Daily summary card

```
┌─ Today, Wed May 27 ─────────────────────────────────────────────┐
│ 📝 NeurIPS 2026 · 18 days left · 167/800 GPU-h burned (21%)     │
│                                                                  │
│ Overnight: 7 ablations completed, 1 failed, 4 still running.    │
│ Best result: claim 2 sweep n=24 → +1.3% over baseline.          │
│ Council says: claim 2 is now 'strong'; claim 1 still 'suggestive'│
└──────────────────────────────────────────────────────────────────┘
```

#### 2. Decision queue (the most important block)

A first-class table of things waiting on the user. Each row is a
`paper_decision` with status `pending|approved|rejected|deferred`.

Examples of decisions the agent files:

- *"Lit search found Smith 2024 (`smith24diffusion.pdf`). Their §4 is
  close to our claim 1. Cite + differentiate? [Read abstract] [Yes,
  cite] [No, ignore]"*
- *"Council says claim 3 is weak (2/3 ablations regressed). Drop
  the claim or pivot? [Drop claim] [Keep + add 2 more ablations
  (~24 GPU-h)] [Discuss]"*
- *"Author Agent rewrote §4.2. Diff: [view]. [Accept] [Reject and
  revert] [Re-write with note: ___]"*
- *"Reviewer sim flagged: 'no comparison to RetNet'. Add a baseline
  run? Est. 8 GPU-h. [Approve] [Decline]"*
- *"Figure 3 caption was auto-drafted. [Approve as-is] [Edit]"*
- *"Ablation set complete — integrate into Figure 5? [Yes] [Hold,
  awaiting seed 4]"*

Each decision shows: trigger (who/what raised it), context link,
default action highlighted, optional cost estimate. Approved
decisions disappear; rejected ones move to a history pane.

The decision queue is **the central artifact of paper mode**.
Everything that needs a human-in-the-loop choice flows through it.

#### 3. What's running now

Compact strip showing: per-GPU what's running, ETA, claim it backs.

#### 4. Section health

Per-section pills with status: `draft / writing / blocked / ready /
needs review`. Click a section → opens it in the LaTeX viewer with
the agent's notes inline.

```
  Abstract       ready
  Introduction   ready
  Related Work   writing (Lit Agent — 12/12 cites confirmed)
  Method         needs review  (3 paragraphs flagged unclear)
  Experiments    blocked on claim 3 (2 ablations queued)
  Discussion     draft
  Conclusion     not started
```

#### 5. Cost + time trackers

Twin progress bars: GPU-hours and days-to-deadline. If burn rate
predicts overshoot, an inline warning surfaces with the agent's
suggested cuts ("drop seeds=5→3 on the cross-dataset sweep to fit").

#### 6. Recent commits (paper-history)

Top 5 commits from `paper/.git`, with one-line summaries and "view
diff" links. Author = "Author Agent" or user's name.

### The other sub-tabs (Claim Coverage, Paper Plan, Critical Path)

All preserved from v2. Today is the default; the others are for
when the user wants to drill in.

A new sub-tab — **Related Work** — gets added (more on lit search
below).

---

## New first-class entities (additions to v2)

### `paper_decision` — the decision queue

```
id, created_at, source ('lit'|'council'|'agent'|'reviewer_sim'|'system'),
kind ('cite_paper'|'kill_claim'|'add_ablation'|'approve_text'|
      'approve_figure'|'merge_section'|'budget_overrun'|…),
title, body_md, default_action ('approve'|'reject'),
options JSON,                  -- [{label, action, est_cost}, ...]
status ('pending'|'approved'|'rejected'|'deferred'),
resolved_at, resolution_note,
linked_claim_id, linked_figure_id, linked_run_id, linked_commit_sha
```

The Author Agent's primary OUTPUT is filing decisions, not running
runs (Paper Runner does that). Coupled with the existing event bus,
decisions are how the agent collaborates with the user.

### `paper_section` — section health tracking

```
id, slug, title, file_path, status ('draft'|'writing'|'blocked'|'ready'|'needs_review'),
blocked_on_claim_id, blocked_on_run_id,
last_agent_pass_at, last_user_edit_at,
agent_notes_md             -- "I'm uncertain about §3.2 paragraph 3..."
```

The agent maintains this. The user can also set `status='ready'` to
freeze a section (agent won't auto-rewrite).

### `paper_version` — explicit version pins

```
id, label ('v0'|'v1-internal'|'v2-submitted'|'v3-rebuttal'|…),
created_at, latex_commit_sha, snapshot_json,
claims_summary_md, headline_metrics_json, frozen_pdf_path
```

Whenever the user clicks **Pin version** (or auto-triggered by Submit /
Rebuttal / Camera-ready), a snapshot is taken. The Versions tab
shows them side-by-side with diffs. Lets the user compare "what we
claimed in v1 vs what reviewers complained about in v2."

### `paper_citation` — bibliography with provenance

```
key, bibtex_md, source ('arxiv'|'scholar'|'semantic_scholar'|'manual'),
arxiv_id, semantic_scholar_id, doi,
abstract_md, relevance_md,        -- agent's notes on why this is cited
cited_in_sections[], pulled_at, user_approved_at
```

This replaces the spec v2 hand-waved `refs.bib`. The Lit Agent
populates it; user approves entries via decisions.

### `paper_review_sim` — pre-submission fake-review

```
id, version_id, ran_at, model, content_md,
suggested_decisions JSON   -- [{title, body, est_cost, ...}, ...]
```

See Phase 5 below.

### `paper_budget_event` — cost tracking

```
id, at, kind ('gpu'|'llm'), category, run_id, cost_units, cost_usd, note
```

Aggregated into the Today view's cost progress bars.

### `paper_comment` — collaborator threads (v1.1, see below)

---

## Lit search & Related Work (the missing piece)

A new **Lit Agent** runs alongside the Author Agent. Its job:

- Take the project's purpose + claims → generate a search query.
- Pull candidates from arxiv API + Semantic Scholar API.
- Rank by relevance (cosine sim on title+abstract embedding).
- Maintain `related_work_candidates.md` with the top 50.
- For each "must-cite" candidate, file a decision: "Cite Smith 2024
  in §2.1? It claims X." Once approved, add to `paper_citation` and
  weave into `related_work.tex`.
- Maintain a **differentiation matrix** — markdown table showing
  "what's the same / different vs each cited paper" — which becomes
  a key piece of §2 Related Work.

The user has a new sub-tab **Related Work** with:
- The cited list (approved papers).
- A "discover" pane showing new candidates the Lit Agent surfaced
  this week.
- The differentiation matrix.
- A free-text "search arxiv for X" box that triggers the Lit Agent.

This is what makes the agent feel like a co-author rather than a
plotter.

---

## Phase 5 — Reviewer Simulation

Triggered manually OR auto-fires when:
- The user version-pins (`v1-internal`), OR
- All claims hit ★ ready, OR
- The user clicks `Simulate reviewers` (always available).

The reviewer-sim:
1. Snapshots the current PDF + claims + key figures.
2. Council members (gemini, openai, claude) each read the PDF and
   role-play one reviewer apiece. Strict NeurIPS-reviewer persona:
   skeptical, focused on weaknesses, asks for missing experiments.
3. Each produces:
   - A markdown review (strengths / weaknesses / questions / score).
   - 2-5 suggested ablations or experiments that would address their
     weaknesses, with cost estimates.
4. The reviews land in the Reviewer Sim tab. The user can:
   - Approve any suggested ablation → it flows into the decision
     queue and `paper_run` table.
   - Mark a weakness as "won't address" with a note (becomes
     limitations / future work text).
   - Re-run the simulation against a newer paper version.

This is the **single most useful pre-submission tool** the system
offers. Honest reviewer simulation has been shown to dramatically
improve acceptance rate. The council is brutally instructed to
NOT be polite.

---

## Phase 6 — Submission

A **Submit** button in the header (next to the mode toggle) opens the
submission helper:

1. **Anonymization check** — runs over `paper/` looking for author
   names, affiliations, GitHub URLs, anonymization-breaking commit
   messages. Flags problems before the user accidentally submits a
   de-anonymizable PDF.
2. **Reproducibility checklist** — pre-filled from the existing
   `dataset_registry`, `env_introspection`, `run` metadata. User
   reviews and approves.
3. **Page limit** — runs `pdftotext` + line count vs venue's limit.
   If over, suggests cuts.
4. **Bundle** — produces `submission/<paper-id>.zip` with:
   - The anonymized PDF.
   - Source `.tex` files.
   - `supplementary.pdf` (auto-generated from the reproducibility
     appendix + extra figures).
   - A `code-link.txt` with the anonymized code-repo URL (if
     configured).
5. **Pin** as `v2-submitted` automatically.

The user downloads the zip + uploads to OpenReview/CMT manually. We
don't (in v1) automate the upload — too venue-specific.

---

## Phase 7 — Rebuttal sub-mode

After submission, a new state `paper_phase='rebuttal'` unlocks:
- A **Rebuttal** tab where the user pastes each reviewer's review.
- The Author Agent reads them, identifies common concerns, files
  decisions ("Reviewer 2 asks for cross-domain check on dataset Y
  — queue ablation?").
- The Decision queue now mixes in user-driven rebuttal items.
- A `rebuttal.tex` file is auto-drafted as the user approves
  experiments, weaving in their results.
- When the user version-pins `v3-rebuttal`, the bundle helper
  produces the rebuttal package (PDF + new experiments table).

Same engine, different prompts.

---

## Phase 8 — Camera-ready & archive

Trivial state transition: `paper_phase='camera_ready'` → `archived`.
Archive button bundles everything (drafts, versions, all runs, all
figures, all commit history) into a `.tar.gz` that lives next to the
research archive.

---

## Collaborator workflow (v1 minimum, v1.1 full)

A paper is rarely solo. v1 needs:

- **Read-only share link**. A token-gated URL (`/p/<token>`) renders
  the current PDF + Claim Coverage + Decision Queue in read-only
  mode. The advisor can see what's happening without an account.
- **Email digest to authors** (uses existing `notify` pipeline) —
  daily, with: completed ablations, decisions waiting, GPU burn,
  days to deadline.

v1.1 adds:
- Per-section comments (advisor leaves "this paragraph is unclear"
  on §4 ¶3).
- A "@francois" mention syntax in decision-queue replies.
- Per-author seat with separate decision approvals.

The spec captures the v1.1 surface so the v1 schema doesn't paint
us into a corner. `paper_comment` table:

```
id, at, author_name, body_md, target_kind ('section'|'figure'|'claim'|'decision'|'commit'),
target_id, resolved_at, thread_id
```

---

## Decision-queue mechanics (the central UX)

This is so important it gets its own section. The decision queue:

1. **Sources of decisions**:
   - Author Agent (most common): "rewrote §X, approve?"
   - Lit Agent: "cite Smith 2024?"
   - Council (per-run): "this ablation regressed — drop claim?"
   - Reviewer Sim: "weakness flagged — add ablation?"
   - System: "GPU budget at 90% — change plan?"
   - User-filed: "Author Agent, add this experiment because…"

2. **Decision lifetimes**:
   - Some are TIMED (budget warnings auto-resolve when budget recovers).
   - Some are STALE-able (the agent retracts a decision if the
     underlying assumption changed — e.g. lit-cite withdrawn after
     a regression invalidates the comparison).
   - Some are USER-OWNED (the user can defer indefinitely).

3. **Default action highlighting**. Each decision has a recommended
   action with a colored chip (green = recommended approve, red =
   recommended reject, grey = no opinion). The user can act in 1
   click; or open to see the full rationale.

4. **Bulk actions**. Researchers in flow mode want to triage 20
   decisions in 2 minutes. Add: `Approve all lit cites`, `Approve all
   text rewrites in the last hour`, etc. — with a "undo last bulk"
   safety.

5. **Slack-like keyboard shortcuts**: `j/k` move between, `Enter`
   approve, `R` reject, `D` defer.

The decision queue is what makes paper mode feel "live" rather than
"static plan". It's the heartbeat.

---

## Anti-pattern detection (the agent calling out bad habits)

A new background watcher (separate from the Council watcher) fires
hourly. Looks for behavior patterns:

- *"You've changed claim 2 three times this week — settle on a
  framing?"*
- *"30% of paper_runs failed since yesterday — likely infra problem,
  not research problem. [Investigate]"*
- *"You haven't generated figures in 5 days but ran 28 ablations. Plot
  to interpret."*
- *"Reviewer-sim hasn't run on the current draft — recommended
  before any 'ready' state."*
- *"Author Agent rewrote §3 twice today. Section is unstable;
  consider freezing it (`paper_section.status='ready'`)."*
- *"GPU-h burn projects 1200 by deadline, budget is 800. Suggested
  cuts: …"*

Surfaces as low-priority decision-queue items the user can dismiss
without consequence. Just a nudge.

---

## Cost-aware planning

Budgets are first-class:
- `gpu_budget_hours` and `llm_budget_daily_usd` set at onboarding.
- A `paper_budget_event` row created for every GPU-hour consumed and
  every LLM call made (the existing notify/council code already has
  this data; we just need to plumb it).
- The Today view shows twin progress bars. When projected to
  overshoot, the agent auto-files a "budget overrun" decision with
  suggested cuts.
- A **planning sandbox** mode lets the user toggle "what if we drop
  cross-dataset experiments?" and see the projected ETA + cost
  shift, without actually changing the queue. Like a feature flag
  preview.

---

## Versioning + diffs (deeper than v2)

Every commit in `paper/.git` is browsable from a Versions tab:

- Linear list of commits, newest first.
- Per-commit: file diff, author (agent or human), one-line summary.
- Diff between any two versions (e.g. v1-internal vs v2-submitted)
  showing per-section diffs, claim diffs, figure diffs (with
  side-by-side image comparisons).
- "What changed since I last looked?" view shows everything since
  the user's last visit timestamp.

Versions explicitly pinned (v0, v1, v2, v3) appear with bigger
labels and snapshot bundles.

---

## Build/compile reliability (sharpening v2)

v2 mentioned `latexmk` failures. Real life:
- Compile fails on missing `\}` → user wants to know **which line** and
  why.
- Compile fails on missing image → agent fix is "regenerate figure 3"
  (a decision filed automatically).
- Compile fails on bibtex → another agent fix.
- Compile passes but produces an UGLY PDF (overflow boxes, etc.) →
  surface a warning in the build log.

Build log card lives in the right rail; on failure, an action chip:
"Ask agent to fix" → spawns a focused agent run that addresses just
the compile error.

---

## Data model (consolidated, full)

```
paper_meta          (1 row per project)
  venue, style_id, deadline_iso, anonymize, authors_json,
  gpu_budget_hours, llm_budget_daily_usd, title_preference,
  paper_folder, phase ('proposal'|'scaffold'|'daily'|'reviewer_sim'|
                       'submission'|'rebuttal'|'camera_ready'|'archived')

paper_proposal       (1+ rows; archive of every council assessment)
  ... as in v2 ...

paper_claim          (as v2 + status: 'active'|'killed'|'completed'|'parked')

paper_figure         (as v2 + integration_status, last_render_at)

paper_run            (UNIFIED with run table via context='paper', as v2)

paper_baseline       (as v2)

paper_citation       (NEW — bibliography with provenance)

paper_section        (NEW — section health)

paper_version        (NEW — pinned snapshots)

paper_decision       (NEW — the central decision queue)

paper_review_sim     (NEW — reviewer simulation outputs)

paper_budget_event   (NEW — cost ledger)

paper_comment        (v1.1 — collaborator threads)

dataset_registry     (as v2)

mode_history         (as v2)
```

## Agent surface (formal)

Four agents now run in paper mode (research mode unchanged):

1. **Author Agent** — same as v2. Plans, writes LaTeX, files
   decisions, never launches runs.
2. **Paper Runner** — same as v2. Schedules, launches runs, no LLM.
3. **Lit Agent** — NEW. Arxiv + Semantic Scholar queries, citation
   ranking, related-work draft. Cheap LLM (use Haiku/Flash by
   default to keep cost down).
4. **Council** — same as v2 for per-run reviews, plus a new
   **Reviewer Simulator** prompt fired on demand.

Each agent has its own daily LLM budget (subset of the total
`llm_budget_daily_usd`).

## API endpoints (consolidated)

```
GET  /api/paper/state          -- single payload: meta, claims, figures, runs, decisions, sections, versions, budget
GET  /api/paper/today          -- the Today view's content
POST /api/paper/decisions/<id>/resolve {action}
GET  /api/paper/decisions      -- queue, filterable
GET  /api/paper/citations
POST /api/paper/citations/<key>/approve
GET  /api/paper/sections
PUT  /api/paper/sections/<slug>/status
GET  /api/paper/versions
POST /api/paper/versions/pin {label}
GET  /api/paper/versions/<id>/diff?against=<id>
POST /api/paper/reviewer_sim/run
GET  /api/paper/reviewer_sim/latest
POST /api/paper/submit/anonymize_check
POST /api/paper/submit/bundle
POST /api/paper/recompile
GET  /api/paper/build_log
WS   /api/paper/events         -- SSE for live decisions, runs, commits
GET  /api/paper/share/<token>  -- read-only collaborator view
```

## Acceptance tests (v1 done)

In addition to v2's acceptance tests:

1. From a fresh paper-mode flip, Phase 3 scaffold completes within
   30 min: claims populated, v0 PDF rendered, ≥1 lit-cite decision
   filed, ≥1 figure planned, ≥1 paper_run queued.
2. Decision queue: opening Today shows pending decisions in priority
   order; `j/k/Enter/R` keyboard works; bulk-approve last 5 lit cites
   undoable.
3. Cost dashboard: queueing a planned 100-GPU-h sweep with budget
   at 750/800 auto-files a budget-overrun decision with suggested
   cuts.
4. Reviewer sim: clicking "Simulate reviewers" produces 3
   independent reviews within 5 min; each review can produce ≥2
   approvable decisions; approving a decision adds rows to the run
   queue.
5. Submission helper: a paper with the author name in `\author{}`
   fails anonymization-check; bundling skipped until user fixes.
6. Versions: pinning v1-internal then v2-submitted lets the user
   diff them per-section; PDFs side-by-side render.
7. Rebuttal: pasting 3 reviews into the Rebuttal tab files ≥6
   decisions; approving them populates `rebuttal.tex` and queues
   new runs.
8. Lit agent: typing "find papers on diffusion ensembles for
   language" in the Related Work search box produces ≥5 candidates
   within 30s with abstracts.
9. Share link: copying the read-only URL and visiting in incognito
   shows current PDF + Claim Coverage + Decision Queue (read-only).
10. Anti-pattern: forcing 3 claim re-writes in 1 hour surfaces the
    "stop tweaking" nudge as a low-priority decision.

---

## Implementation order (revised, ~5-6 days)

**Phase A — plumbing (1.5 days):**
- All new DB tables.
- Paper Runner + `local` backend.
- Author Agent + Lit Agent + reviewer-sim prompts.
- `latexmk` container.
- Event bus.

**Phase B — mode flip & onboarding (0.5 day):**
- Header toggle.
- Async pre-flip proposal.
- Onboarding modal (Phase 2).

**Phase C — the Daily Loop (2 days):**
- Today view (the heart).
- Decision queue (the central UX).
- Section health + Cost dashboard.
- Versions tab.
- Right rail (Summary / Author Agent / Build log / Sessions).

**Phase D — Lit Agent + Reviewer Sim (1 day):**
- Lit search + Related Work tab.
- Reviewer sim button + tab.

**Phase E — Submission + Rebuttal + Share (0.5-1 day):**
- Anonymization check, bundling, checklist.
- Rebuttal sub-tab.
- Read-only share token + daily email digest.

Total ~5.5 days. The v2 estimate of 3.5d underestimated by missing
~half the surface area. This number is honest.

---

## Out of scope (v2 carryovers + new exclusions)

- Multi-paper portfolio management. (v2.5)
- In-line citation search UI beyond Lit Agent's auto-pull. (v2)
- arXiv / OpenReview API upload automation. (v2)
- Image-diagram generation (mermaid / TikZ from spec). (v1.5)
- Notebook scratchpad for one-off plots. (v1.5)
- Co-author seats with separate approvals. (v1.5)
- Bar-Gantt over multi-day horizons. (v1.1, after Paper Runner has
  enough throughput data)
- SLURM / K8s / Ray runner backends. (v1.5)
- Full main-draft in-app LaTeX editor; v1 ships override files only.

---

## The one big open question for the user

In Phase 4 (Daily Loop), the **decision queue** is the central UX.
This works only if the agent is GOOD at proposing decisions with
correct default actions. If it cries wolf or proposes obviously-wrong
defaults, the user will train themselves to bulk-approve without
reading — which is worse than no decisions at all.

**Mitigation:** start with a small set of decision kinds (the 6
listed in `paper_decision.kind`). Add new kinds only after the
existing ones have a confirmed >70% user-approval rate (we can
measure this from resolutions). This keeps the queue tight and
trustworthy.

Two paths forward — please pick:

- **A. Conservative**: ship v1 with only 3 decision kinds
  (`cite_paper`, `approve_text`, `add_ablation`). Add others after
  measuring approval rate.
- **B. Aggressive**: ship v1 with all 6 kinds. Tune later based on
  user feedback. Risk: queue feels noisy.

My recommendation: **A**, with a Settings toggle to enable the
others for power users.
