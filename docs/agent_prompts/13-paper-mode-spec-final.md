# Paper Mode — final spec (post-review)

Status: **final, ready for sign-off + implementation** after synthesising
reviews from Gemini 2.5 Pro (`external-review-paper-mode-gemini.md`)
and GPT-5 high (`external-review-paper-mode-openai.md`). Both reviewers
overlap heavily; where they disagree this doc picks a side with the
reasoning.

## What changed from v1, and why

| v1 idea | v2 decision | why (which reviewer pushed for it) |
|---|---|---|
| Paper mode turns off research / PI / council. | Engine stays on. We retask it to support the paper. Council per-run review still runs, but its job becomes "is this ablation evidence for a claim?" rather than "what should we try next?". The PI agent's prompt switches to a paper-aware variant. | Gemini: forcing a hard mode-revert when an ablation reveals a new direction is the wrong reflex. The most insightful research happens in the 11th hour of a paper. |
| `paper_runs.md` is the authoritative queue. | **DB tables are the source of truth.** `paper_run`, `paper_figure`, `paper_claim` are real SQL tables with versions + row locks. The markdown files become **projections** the Author Agent reads/writes for human review; backend renders them on save. | Both: markdown-as-database races with concurrent edits and breaks the UI when the agent malforms a table. |
| Author Agent both writes LaTeX AND launches runs. | Author Agent **plans and writes only**. A new **Paper Runner** service launches and statuses runs. Clean separation of concerns. | GPT-5: "the spec never defines who actually launches the ablations." Gemini: agent-as-gatekeeper is infuriating. |
| Hard-code "2-3 claims" in the Author Agent prompt. | Soft target. Author Agent decides; **Claim Coverage** becomes the readiness gate, not claim count. | Both: some papers have 1 claim; some have 5. |
| Gantt chart as the planning artifact. | **Critical Path** view ships first (per-claim ETA with p50/p90 bands + a 3-5 row "submission blockers" card). Full bar-Gantt is v1.1 once the Paper Runner has real throughput stats. The current Gantt is too misleading on day 0. | Both: "Gantt is a lie", "without real queueing and ranges it gives false precision." |
| Read-only LaTeX viewer. | **Read-only main draft + per-section user-override files** (`sections/04_experiments.user.tex` overrides the agent's `04_experiments.tex`). Agent treats `.user.tex` as immutable; user edits there. In-app CodeMirror editor for the override files. **Full main-draft editor: v1.1.** | Gemini: read-only is a non-starter. GPT-5: override-files dodge merge complexity. The split keeps the agent author-of-record while letting the user write. |
| Pre-flip council modal blocks the user on 3 LLM calls. | **Asynchronous Paper Proposal.** Clicking `Paper` immediately persists a "proposal in progress" artifact and returns the user to Research mode. The council runs in the background. When complete, a Summary card lights up: *"Council finished assessing readiness — review (2/3 say proceed)"*. The user opens the proposal modal when they're ready. | Gemini: blocking for minutes is a momentum-killer. GPT-5 same. |
| Reversal hash is "vague". | **Paper Snapshot** is a structured JSON of (claims, figures, run DAG, latex commit SHA, metrics cache hash). Stored in `mode_history`. Re-entering paper mode diffs against this snapshot so the agent doesn't redo finished work. | GPT-5. |
| Single LLM session for everything. | Author Agent runs in its own tmux + its own LLM session. **They share project memory (vector index over lessons + runs + figures), not conversation state.** Author Agent has a **daily budget knob** (`author_agent_budget_per_day`). | Both. Separation of concerns + cost control. |
| Council disagreement on novelty resolved by a tiebreaker model. | **Show deltas, no model-as-judge for novelty.** Novelty is genuinely subjective; collapsing it loses the signal. One-click "ask an external advisor" routes the bundle to a 4th model OR (new) generates a pre-filled email template the user sends to a human collaborator. | GPT-5: "do not add a model-as-judge". |
| No multi-seed / statistical rigor. | First-class: **n_seeds, seeds[], reduce (mean/median), ci (bootstrap/t), alpha, compare_to (run_id OR external baseline)**. Seed bundles render as ONE row in the plan with bands in plots. | GPT-5: single runs won't pass reviewer sniff tests. |
| External baselines are run_ids only. | New **Baseline** entity: `type ∈ {run, external}`, fields `citation_key`, `value`, `variance`, `reproduce_status ∈ {not_started, in_progress, verified, declined}`, `notes`. Figures' `runs[]` can include `baseline_id`. | GPT-5. |
| Resource model = 1 GPU per run. | Per-run **resource request**: `gpus`, `gpu_mem_req`, `cpu_cores`, `ram_gb`, `disk_gb`, `preemptible_ok`. Paper Runner bin-packs. **Data prep tasks** (download/preprocess) are first-class blockers via `task_type ∈ {compute, analysis, infra}`. | GPT-5. |
| No HPC support. | **Runner backend plugin**: `local | slurm | k8s | ray`. v1 ships `local` only; the interface is defined so a SLURM plugin is a follow-up. | GPT-5. |
| `done` is ambiguous (run finished vs integrated). | Split into `run_status ∈ {queued, blocked, running, done, failed, paused}` and `integration_status ∈ {pending, integrated, stale}`. UI shows both pills. | GPT-5. |
| No diff view of paper edits. | Author Agent **commits every change to a real git repo** at `paper/.git`. Summary tab gets a commit-history card with per-section diffs. User overrides are commits on the same branch. | Both. |
| LaTeX compile failures swallowed by the spinner overlay. | A new **build log card** in the right rail: streams `latexmk` output, highlights errors, sticks around when compile fails so the user can debug or click "ask agent to fix." | Gemini. |
| No claim coverage view. | **Claim Coverage** is the **default subview** of the Write-the-paper tab. The Paper Plan / Critical Path / Gantt are sibling tabs. Coverage matrix: one row per claim × columns for evidence types (main, ablations, scaling, cross-dataset, significance, robustness, external baseline). Each cell = status + link. | GPT-5. |
| Round-trip with no friction. | Round-trip stays, with **24h cooldown** between reversals and a **required revert reason** captured in `mode_history`. Visible "Paper mode attempts" history card. | GPT-5. |
| Reproducibility hand-waved. | Author Agent **auto-generates a Reproducibility appendix** from run metadata, env introspection (`pip freeze`, CUDA), and a new **dataset registry** (`name, version_hash, license, preprocessing_hash`). Inflates a NeurIPS reproducibility checklist. | GPT-5. |

## What we explicitly defer to v1.1

- Full in-app editor for the main draft (v1 has override files only).
- Full bar-Gantt visualisation (v1 ships Critical Path + collapsible
  per-run Gantt of the next 24h only).
- SLURM / K8s / Ray runner backend plugins (interface defined in v1,
  only `local` shipped).
- Auto-generated diagrams / figures beyond matplotlib.
- Inline citation search / refs.bib management UI (v1: refs.bib is
  edited by the Author Agent).
- arXiv / OpenReview submission helpers.

## Final architecture

### Modes

```
project_mode  ∈ { research, paper }

research:   engine on, research agent runs ideas.md, council reviews
            research runs, PI agent nudges, no Author Agent.
paper:      engine on (now scoped to paper_runs by default), Paper Runner
            schedules ablations, Author Agent plans + writes, council
            per-run review now assesses 'is this evidence for claim X?',
            PI agent's prompt becomes paper-aware.
```

The engine never stops. The **scope** of what it works on changes.

### Pre-flip flow

1. User clicks `Paper` in the header. Modal asks them to confirm:
   *"Start a Paper Proposal? The council will assess in the background;
   you can keep researching while it works."*
2. On confirm → server creates a `paper_proposal` row, kicks off the
   three reviewers in parallel, returns immediately. UI continues in
   Research mode; a Summary card shows progress.
3. When all reviewers respond (or after a 5-minute timeout if any
   stalls), the Summary card flips to *"Proposal ready — review."*
4. Clicking the card opens the modal with one column per reviewer:
   claims, evidence strength (strong/suggestive/anecdotal), novelty
   assessment, top red flags, recommendation. **No collapsed summary.**
5. User chooses `Keep researching` or `Proceed to Paper mode`.
6. On proceed → mode flip happens (next section). On keep researching →
   proposal is archived but stays viewable in History (the user can
   ask the council to re-assess any time).

### Mode flip (research → paper)

1. `setting.project_mode = "paper"`.
2. **Engine retasked, NOT stopped**: research agent's prompt is
   replaced with a paper-aware variant (focus on supporting claims X,
   Y, Z); ideas.md is paused (queue is preserved but no new pulls); PI
   prompt switched.
3. **Spawn Author Agent** in tmux `author`.
4. **Spawn Paper Runner** background service.
5. Council per-run review prompt switches to evidence-assessor mode.
6. Navigate to Write-the-paper tab.
7. `Event(type='mode_changed', message='entered paper mode',
   proposal_id=…)`.

### Tables (new)

```
paper_proposal
  id, created_at, status (in_progress|ready|accepted|rejected),
  council_responses (JSON: per-reviewer), accepted_at

paper_claim
  id, idx, title, summary, status (active|killed|completed),
  evidence_strength (strong|suggestive|anecdotal),
  novelty (high|medium|low|unclear),
  council_provenance (which reviewer proposed it),
  rationale_md, killed_reason

paper_figure
  id, claim_id, kind (line|bar|table), title, caption_md,
  panels JSON, style_id, status (planned|drafted|done|stale),
  path (e.g. paper/figures/fig3.pdf)

paper_run    ← UNIFIED with the existing run table via context='paper'
  ... existing run fields ...
  context = 'research' | 'paper'
  paper_claim_id, paper_figure_id, role (main|ablation|scaling|cross|baseline)
  task_type (compute|analysis|infra)
  n_seeds, seeds_json, reduce, ci_method, alpha, compare_to (run_id|baseline_id)
  gpus, gpu_mem_req, cpu_cores, ram_gb, disk_gb, preemptible_ok
  integration_status (pending|integrated|stale)
  est_time_sec, est_throughput_steps_per_sec

paper_baseline
  id, type (run|external), citation_key, value, variance,
  reproduce_status (not_started|in_progress|verified|declined),
  notes_md

dataset_registry
  name, version, hash, license, preprocessing_hash, size_bytes,
  download_url, prep_cmd

mode_history
  id, from_mode, to_mode, at, reason_md, snapshot_json
  -- snapshot_json captures: claims, figures, run DAG, latex_commit_sha
```

### Author Agent contract (formal)

**Inputs** (read-only):
- `lessons.md`, frontier runs, project metric direction.
- Council preflip JSON (from `paper_proposal`).
- Dataset registry, metrics schema, throughput table.
- User edits in `sections/*.user.tex` (treated as immutable).

**Outputs** (writes, all via git commits in `paper/.git`):
- `claims.md` (projection of `paper_claim` table).
- `paper_figures.md` (projection of `paper_figure`).
- `paper_runs.md` (projection of paper-context rows of `run`).
- `sections/*.tex` (NEVER `*.user.tex`).
- `figures/*.pdf` regenerated when an underlying run's
  integration_status flips to `stale`.
- `refs.bib`.

**Events** (emit via SSE):
- `claim_added | claim_changed | claim_killed`
- `figure_planned | figure_drafted | figure_done | figure_stale`
- `infra_needed` (the agent identified missing code paths or datasets)
- `paper_compiled | paper_compile_failed`
- `coverage_changed`

**Non-responsibilities:** the Author Agent **never** launches runs.
It only writes rows into the paper_run table; the Paper Runner picks
them up.

### Paper Runner (the new service)

A small daemon (Python thread within the FastAPI process is enough
for `local` backend) that:

1. Watches `paper_run` for `queued` rows where every `depends_on`
   has `run_status='done'`.
2. Allocates GPUs by bin-packing the resource requests against the
   live `gpu` table.
3. Launches each run in its own tmux session (same machinery the
   research agent uses), with the run's command + resources injected
   via env vars.
4. Updates `run_status` atomically as runs progress (via the SDK's
   existing track endpoints).
5. Exposes `WS /api/paper/events` for live updates.
6. Enforces `paper_run_concurrency` (defaults to GPU count).
7. Handles preemption: if `preemptible_ok=true` and a higher-priority
   run wants the GPU, sends SIGTERM with grace period.

Pluggable via `setting.runner_backend ∈ {local, slurm, k8s, ray}`; v1
ships `local` only.

### "Write the paper" tab — final layout

Three sibling sub-tabs (like W&B's workspace tabs):

#### Tab 1: **Claim Coverage** (DEFAULT VIEW)

```
┌─ Claims ────────────────────────────────────────────────────────┐
│ Claim                  │ main │ ablations │ scaling │ cross │ ★ │
│ ───────────────────────┼──────┼───────────┼─────────┼───────┼───│
│ 1. Diffusion ensembles │ ✓    │ 3/5       │ — (q)   │ —     │ □ │
│    beat AR on GSM8K    │      │           │         │       │   │
│ 2. AR-init helps diff  │ ✓    │ 4/4       │ ✓       │ 2/3   │ ✓ │
│ 3. Cosine schedule     │ — (q)│ —         │ —       │ —     │ □ │
└──────────────────────────────────────────────────────────────────┘
```

Each cell is clickable → drills to the runs / figure backing it. ★
= "claim is publication-ready" (all coverage met + significance test
passed). The user controls a per-claim coverage checklist (some
claims don't need scaling experiments, for example).

#### Tab 2: **Paper Plan** (figure-first, with runs nested)

The current v1 spec's figure/run tree. Filters: by claim, dataset,
model, status, integration_status. Seed bundles collapse to one row
with `n=5` in a chip; expand to see per-seed runs.

#### Tab 3: **Critical Path** (replaces Gantt v1)

```
┌─ Submission blockers (per claim, by ETA) ──────────────────────┐
│ Claim 2 — AR-init helps diff                                    │
│   ⏱ p50 1.2 days  /  p90 2.8 days                              │
│   blocked on: imagenet1k preprocessing (infra)                  │
│                                                                  │
│ Claim 1 — Diffusion beats AR on GSM8K                           │
│   ⏱ p50 2.4 days  /  p90 4.1 days                              │
│   blocked on: scaling sweep (5 runs queued, 4 GPUs free)       │
└──────────────────────────────────────────────────────────────────┘

┌─ Next 24h Gantt (collapsible) ▾                                 │
│   [ bars for the runs scheduled to complete in the next 24h ]   │
└──────────────────────────────────────────────────────────────────┘
```

The full historical Gantt lands in v1.1 once we have ≥1 week of
throughput data.

### Paper viewer (HERO)

Above the three sub-tabs, a fixed paper viewer with:

- `[ LaTeX | PDF ]` toggle.
- `⟳ Rebuild` (manual). Background rebuild auto-debounces on file
  change; "PDF up to date" / "PDF stale (built 2m ago)" badge.
- `⤓ Download PDF`.
- `📜 Build log` button → opens the right rail to the build-log tab
  (auto-opens on compile failure).
- LaTeX view = CodeMirror, **read-only** for `sections/*.tex` and
  **editable** for `sections/*.user.tex`. The toggle next to the file
  list lets the user "create override" which clones the agent's
  version into `*.user.tex` and unlocks editing.

PDF rendering: server-side `latexmk -pdf` in a slim Docker image
(texlive layer cached). Client uses `pdf.js` to render the result.

### Right rail (paper mode)

Three tabs:
- **Summary** — claim/figure/run events, commit history with diffs,
  council ready-cards, Author Agent spinner lines (grouped by
  activity with progress %).
- **Author Agent** — live tmux terminal of the `author` session.
- **Build log** — `latexmk` output, errors highlighted, sticky on
  failure.
- **Sessions** — ablation tmux sessions (same as Dashboard).

### Reversal flow (final)

- User-driven: header toggle `Research`. Modal requires a **reason
  text** (1+ sentence). 24h cooldown after the most recent flip;
  before that, the toggle is disabled with a tooltip ("paper mode
  cooldown — flip back in 18h").
- Council-driven suggestion: composite signal
  - 3 of last 7 paper_run.failed (last 24h), OR
  - p90 CI of any claim's main result overlaps baseline across last
    3 completed ablations.
  - Emit Summary card: *"Council recommends reverting (reason: X)."*
    Buttons: `Revert` / `Keep going`. **Never auto-reverts.**
- On revert:
  - Author Agent tmux killed.
  - Paper Runner finishes the run that's mid-flight, marks rest as
    `paused`. Resume tokens stored.
  - Paper Snapshot written to `mode_history.snapshot_json`.
  - Research agent retasked with a prompt that includes the snapshot
    summary + reason: *"you spent N hours in paper mode; here's what
    the ablations showed; focus on X next."*
  - Dashboard.
- Re-entering paper mode later: a new proposal is created. Author
  Agent diffs against the prior snapshot and resumes paused runs.

### Performance targets

| target | how |
|---|---|
| Pre-flip proposal renders within ~5 min | async council, all 3 reviewers in parallel, 5min timeout per reviewer |
| Mode flip user-perceived latency ≤ 2 s | spawn Author Agent + Paper Runner in background; immediate redirect to the tab |
| First paper preview (PDF) ≤ 90 s after flip | seed `main.tex` + one section + matplotlib defaults preloaded; latexmk warm cache |
| Claim Coverage refresh on run completion ≤ 5 s | SSE-driven; only the affected claim row repaints |
| Critical Path ETA recompute ≤ 2 s | per-claim DAG fold, throughput table is precomputed |

## Acceptance tests (v1 done)

1. Click `Paper` with no kept runs → proposal created, council
   responds with `keep_researching` for all three, Summary card
   surfaces, user opens it, picks `Keep researching`, mode unchanged.
2. Click `Paper` with a strong frontier → 2/3 say proceed; user
   proceeds; engine is retasked (NOT killed); Author Agent appears
   in tmux `author` within 10s; Write-the-paper tab loads with the
   Claim Coverage view as default; first PDF preview within 90s.
3. Add an external baseline via the Claim Coverage UI → row appears
   in the Paper Plan; figure regenerates with the baseline number.
4. A queued ablation completes with `headline_metric` worse than
   baseline → `integration_status` stays `pending`; council per-run
   review fires; if 3 of last 7 regress, Summary card recommends
   revert; user dismisses; nothing auto-reverts.
5. User edits `sections/04_experiments.user.tex` in the CodeMirror
   editor → PDF rebuilds; Author Agent's next pass leaves the
   .user.tex untouched.
6. Click `Research` 6h after entering paper mode → cooldown tooltip
   shows. After 24h → revert modal asks for reason; on confirm,
   snapshot is saved, Author Agent killed, research agent respawned
   with the snapshot summary in its startup prompt.
7. Switch to PDF tab with a stale main.tex → background rebuild
   fires, "stale" badge shown, PDF updates within 8s; on syntax
   error, Build log tab auto-opens with the error line highlighted.
8. Bin-packing: queue 2 runs needing 4 GPUs each on an 8-GPU host →
   both run concurrently; queue a 3rd → it blocks until one finishes.

## Open questions (resolved by reviewers — included for reference)

The 8 v1 open questions were all answered in the table above. The
remaining genuine open question for the user:

- Q: Should `Claim Coverage` allow per-claim ★ ("ready") to be
  set by the user manually, or only by the system based on
  significance tests? My recommendation: user toggleable, with a
  "system says not ready" warning when the user overrides it. The
  council's per-run reviews already nudge against premature claims.

## Implementation order (in phases)

**Phase A — plumbing (no UI yet, ~1 day):**
1. DB migrations: `paper_proposal`, `paper_claim`, `paper_figure`,
   `paper_baseline`, `dataset_registry`, `mode_history`; extend `run`.
2. Paper Runner service + `local` backend.
3. Author Agent skeleton (system prompt, file I/O contract, event
   bus).
4. `latexmk` Docker image + compile endpoint.

**Phase B — pre-flip + mode toggle (~0.5 day):**
5. Header toggle + Paper Proposal flow + async council.

**Phase C — Write-the-paper tab (~1.5 days):**
6. Paper viewer with LaTeX/PDF toggle + override files + CodeMirror.
7. Claim Coverage view.
8. Paper Plan tab.
9. Critical Path tab.
10. Right rail: Summary / Author Agent / Build log / Sessions.

**Phase D — reversal + polish (~0.5 day):**
11. Revert modal with reason + cooldown.
12. Paper Snapshot capture/replay.
13. Council reversal-suggestion watcher.

Total: ~3.5 days as planned originally.

## Out of scope (called out so we don't pretend otherwise)

- Multi-author collaboration (single-user instance).
- arXiv / OpenReview submission.
- Image-diagram generation beyond matplotlib.
- Inline citation search.
- Full LaTeX editor on the main draft (v1.1).
- Bar-Gantt over multi-day horizons (v1.1).
- SLURM / K8s / Ray backends (interface in v1, implementations later).
