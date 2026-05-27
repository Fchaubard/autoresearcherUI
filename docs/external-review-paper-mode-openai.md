# Paper Mode review — GPT-5 high

# 1) Workflow gaps

- Who actually launches the ablations?
  - Problem: The spec has the Author Agent maintaining paper_runs.md but never defines the service that reads it and starts runs. In research mode the PI/council handle this; in paper mode they’re off.
  - Fix: Add a Paper Runner (daemon) that:
    - Watches paper_runs.md (or a DB mirror) for queued/blocked rows.
    - Resolves dependencies, assigns resources, launches runs, and updates status atomically.
    - Enforces paper_run_concurrency, GPU packing, and blocked→queued transitions.
    - Is mode-aware and the sole orchestrator in paper mode.

- No support for multi-seed replicates or statistical tests
  - Problem: Single runs per setting won’t pass reviewer sniff tests. You need K seeds, error bars, CI, and delta significance vs baseline(s). Nothing in paper_runs.md/figures supports this.
  - Fix:
    - Extend schema:
      - paper_runs.md: n_seeds, seeds[], reduce=mean|median, ci=bootstrap|t, alpha, compare_to=run_id|external_id, min_effect_size.
      - paper_figures.md: agg_spec per figure/panel, yerr_source, metric, split_by, sweep_params.
    - Author Agent must schedule seed bundles, compute CIs, and decline claims that don’t meet power.

- External baselines are not first-class
  - Problem: Many “baselines” are citations or numbers from third-party repos you won’t re-run. Today everything is a run_id.
  - Fix:
    - Add a Baseline entity with type=run|external, fields: citation_key, value, variance, “reproduce_status” (not_started|in_progress|verified), notes.
    - paper_figures.md runs[] can include baseline_id.
    - Author Agent can either schedule a reproduce task or mark external with justification and CI if available.

- Resource model is too naive (multi-GPU, CPU, RAM, disk, data prep)
  - Problem: paper_run_concurrency = GPU count assumes 1 GPU per job. Many jobs need 4–8 GPUs, big RAM, or long data prep/download.
  - Fix:
    - Add resource fields: gpus, gpu_mem_req, cpu_cores, ram_gb, disk_gb, preemptible_ok, node_affinity.
    - Paper Runner does bin-packing and pre-downloads datasets (queue “data_ready” tasks) before compute tasks.

- Gantt is misleading without real queueing and ranges
  - Problem: Experiments often take days, HPC queues are bursty, and ETAs are ranges. A single bar per run gives false precision.
  - Fix:
    - Drive Gantt off the Paper Runner’s actual schedule and measured throughput; show p50/p90 bands per run; show queue wait time separately.
    - Add a “critical path to submission” view by claim/figure with a single ETA range, not just per-run bars.

- Claims count is hard-coded (2–3)
  - Problem: Some papers have 1 core claim; others 4–5. The tool shouldn’t force 2–3.
  - Fix: Make “target_claim_count” a suggestion. Add a “Claim Coverage” view with checklist items per claim (main result, ablation A/B/C, scaling, cross-dataset, robustness, significance). Gate paper readiness on coverage, not a fixed number.

- Missing reproducibility boilerplate and checklists
  - Problem: Papers need exact configs, seeds, hardware, SW versions, dataset versions, licenses, and limitations. Not in spec.
  - Fix: Author Agent generates a Reproducibility appendix and auto-fills a checklist (NeurIPS/RLHF style). Pull from run metadata, environment introspection, pip freeze, CUDA info, and a dataset registry with pinned versions.

- Figures and tables need panel-level control
  - Problem: Complex multi-panel figures require layout, consistent style, and determinism (fonts, DPI, colors). A single figure_id is not enough.
  - Fix:
    - paper_figures.md supports panels[] with type (line/table/bar), source (runs|external), style tokens.
    - Provide a shared Matplotlib style file + deterministic plot seed. Author Agent regenerates only impacted panels, then assembles.

- Cross-dataset, cross-task can imply new code paths
  - Problem: Cross-dataset may require preprocessing, loaders, or metrics not in the repo.
  - Fix: Author Agent must propose “infra tasks” (PRs) and block the dependent paper runs until merged. Track infra tasks as first-class (statused) in the plan.

- No notion of “analysis-only” tasks
  - Problem: Many paper updates are pure analysis: ablation table rendering, new metric, or slicing existing logs. These should be scheduled but not consume GPUs.
  - Fix: Add task_type=compute|analysis|infra in paper_runs.md and let Paper Runner schedule analysis/intra-node CPU work accordingly.

- Council-to-paper expectations mismatch
  - Problem: Council novelty check produces 2–3 claims; Author Agent may choose different ones. No contract here, and no “red flags” feed-through to the coverage plan.
  - Fix: Pass the council’s claims/red flags into claims.md with provenance. Require the Author Agent to accept/reject each with justification and map them to coverage items.

# 2) UX / visibility issues

- No “claim coverage” visibility
  - Problem: The current table is figure-first. Day to day you think claim-first: what’s missing to lock this claim?
  - Fix: Add a Claim Coverage tab:
    - One row per claim, columns for evidence types (main, ablations, scaling, cross-dataset, significance, robustness, external baseline).
    - Each cell shows status (done/running/queued/missing) and links to underlying runs/figures.

- Critical path not obvious
  - Problem: Gantt shows many bars but not what gates submission.
  - Fix: Display a “Submission blockers” card with the few tasks on the critical path by claim, with ETA ranges.

- PDF rebuilds on tab switch are annoying
  - Problem: Triggering latexmk when switching to PDF can add lag and compile noise.
  - Fix: Background compile on file change, debounce at 5–10s, and show “PDF up to date” vs “Out of date; click Rebuild.” Default to pdf.js to render an already-built PDF.

- No diff visibility for paper edits
  - Problem: Researchers want to see what changed in the draft per commit.
  - Fix: Add a simple commit history with per-section diffs and “inserted/removed” summaries in the Summary tab.

- Run/figure overload in a single table
  - Problem: Figures with 30+ runs become unwieldy.
  - Fix: Add filters: by claim, by dataset/model, by status. Collapse runs into bundles (seeds as one row).

- Status semantics unclear (“done” = integrated?)
  - Problem: “done” is ambiguous: run finished vs integrated into LaTeX.
  - Fix: Split statuses:
    - run_status: running|done|failed
    - integration_status: pending|integrated|stale
    - Make “integrated” visible in the table and Summary.

- Single-LLM spinner lines flood the rail
  - Problem: Long spinner list buries signal.
  - Fix: Group spinners per activity with a single line and a progress percent (e.g., “Drafting v0 (sections 4/6)”).

# 3) Architectural gaps

- Missing Paper Runner orchestrator
  - Fix: Introduce a dedicated service that:
    - Reads paper_runs.md (or DB), schedules tasks, manages resources, writes statuses atomically, and exposes an event stream used by the UI and Gantt.

- Markdown as source of truth is fragile
  - Problem: Concurrent edits (agent, runner, user) on paper_runs.md/paper_figures.md will race.
  - Fix: Store runs/figures/claims in a DB with row-level locks and versioning; generate the markdown projections for human review. Keep commit ids to trace back.

- Author Agent contract too loose
  - Problem: “Write latex, generate runs” is not a contract.
  - Fix: Define the Author Agent API:
    - Inputs: lessons/frontier, council preflip JSON, dataset registry, metrics schema, time-per-step stats.
    - Responsibilities: populate claims.md with rationale and mapping to figures; create/maintain runs with dependencies and resources; update LaTeX only on integrated data; emit structured events (claim_added/changed/killed, figure_updated, infra_needed).
    - Non-responsibilities: starting runs (Paper Runner does) and global scheduling decisions.

- Reversal state capture undefined
  - Problem: “append paper state hash” is vague.
  - Fix: Persist a Paper Snapshot: claims JSON, figures JSON, run DAG (with statuses), latex commit SHA, and metrics cache hash. On re-entry, Author Agent can diff against the snapshot.

- Time-per-step estimation source missing
  - Problem: Gantt requires a model of throughput.
  - Fix: Keep a per-(dataset, model, hparams class) performance table updated from finished runs; fallback to class averages; store p50/p90.

- Resource abstraction for HPC/backends not defined
  - Problem: Some users rely on SLURM/K8s/managed clusters.
  - Fix: Define a Runner plugin interface (local, SLURM, K8s, Ray). Resource fields map to scheduler-specific specs.

# 4) Simpler alternatives

- Replace the full Gantt with a claim-centric “Critical Path” in v1
  - Show per-claim ETA range and a 3–5 item blocker list. Add the full Gantt in v1.1 once Runner/throughput data stabilizes.

- Keep LaTeX read-only, add “suggested edits” instead of full editor
  - A sidecar suggestions.md with per-section diffs and inline “apply” buttons avoids merge complexity in v1.

- One modal for preflip council plus claim coverage scaffold
  - Instead of 3 columns of prose, show their claims mapped into a scaffold that seeds claims.md and coverage items. It both shortens the UI and bootstraps the plan.

- Single endpoint for paper state
  - Serve /api/paper/state that returns claims, figures, runs, throughput, and snapshot in one payload; avoids N small endpoints and sync bugs.

# 5) The hard problems (recommendations)

- Q1 One-way vs round-trip
  - Recommendation: Keep round-trip but add a 24h cooldown and require a revert reason. Show “paper mode attempts” history prominently. It mirrors real workflows and prevents thrash without locking users in.

- Q2 Same vs different LLM session
  - Recommendation: Different sessions. Separation of concerns and prompt hygiene matter. Share a project memory store (vector DB) and artifacts, not the conversation state. Add an Author Agent budget knob (daily token cap) to control cost.

- Q3 LaTeX viewer editable?
  - Recommendation: v1 read-only with “suggest edits” and per-section override files (e.g., sections/04_experiments.user.tex that supersede generated content). Author Agent must merge user overrides verbatim and avoid touching them. Full in-app editor later.

- Q4 PDF rendering: server vs client
  - Recommendation: Server-side latexmk in a slim Docker image with cached texlive layers; render with pdf.js. Build on change, debounce. Only build on-demand on first visit or when stale > 30s. Avoid client-side LaTeX-to-HTML for parity reasons.

- Q5 Reversal heuristic threshold
  - Recommendation: Use a composite signal:
    - 3/last 7 regressions OR p90 CI overlaps baseline across last K=3 completed ablations on a claim → suggest revert for that claim; 2+ claims tripping → suggest global revert.
    - Always include a council summary blurb, but never auto-revert.

- Q6 Gantt accuracy for unseen configs
  - Recommendation: Show “unknown” durations with a wide prior (e.g., multiply class average by 1.5–2x) and a dotted bar. Update once the first 5% of steps complete using online throughput estimation.

- Q7 Cost controls
  - Recommendation: Add an “Author Agent budget” per day and a “max concurrent analysis tasks.” The Author Agent batches updates (e.g., regenerate all figures once per hour or N completed runs).

- Q8 Council disagreement tiebreak
  - Recommendation: Do not add a model-as-judge. Instead, show disagreement deltas clearly and let the user pick. Offer a one-click “ask for external advisor” mode that routes the bundle to a fourth model and to a human preset reviewer template you can email to a collaborator.

# 6) Concrete fixes to the spec

- Add a Paper Runner
  - Spec change: New background service “paper-runner” with responsibilities listed above. New endpoints: POST /api/paper/runs/launch (internal), WS /api/paper/events for scheduling and status updates.

- Formalize the Author Agent contract
  - Spec change: Author Agent emits structured events (JSON) and only edits claims/figures/LaTeX files; it never launches runs. Add a contract doc with input contexts, required outputs, and idempotency rules.

- Database-backed runs/figures/claims with markdown projections
  - Spec change: Introduce tables paper_claims, paper_figures, paper_runs with version column; backend renders paper_runs.md and paper_figures.md as projections. Lock rows on update to avoid race conditions.

- Extend schemas for rigor
  - paper_runs: add fields seeds[], n_seeds, reduce, ci, compare_to, min_effect_size, task_type, resources (gpus, cpu_cores, ram_gb, disk_gb, preemptible_ok).
  - paper_figures: panels[], agg_spec, metric, style_id.
  - baselines: new table with external baseline support and citation fields.

- Add dataset registry and reproducibility appendix
  - Spec change: New dataset_registry table with name, version/hash, license, preprocessing hash. Author Agent auto-generates an appendix and a reproducibility checklist section from env introspection.

- Replace Gantt with Critical Path v1
  - Spec change: Gantt becomes optional/collapsible and defaults to a “Critical Path” widget summarizing claim ETAs with p50/p90. Full bar-Gantt lands after Paper Runner stabilizes.

- Claim Coverage view
  - Spec change: Add a tab in the “Write the paper” screen to show a coverage matrix by claim. Make this the default subview in paper mode.

- Integration status
  - Spec change: Add integration_status to runs/figures. A completed run isn’t “done” until its numbers are in the LaTeX and the figure is recompiled.

- Smarter PDF rebuild
  - Spec change: Build on file change with debounce; show “stale” badge; keep manual Recompile. Use pdf.js in viewer, compile in container.

- Reversal snapshot
  - Spec change: mode_history.reason becomes structured and includes paper_snapshot_id (capturing claims/figures/runs DAG, latex SHA). On revert, mark running runs as paused with resume tokens.

- Cost controls
  - Spec change: settings.author_agent_budget_per_day, settings.max_analysis_concurrency. Author Agent batches figure rebuilds.

- Filters and bundles in Paper Plan
  - Spec change: Add UI filters and bundle seed replicates under one row with an expander. Add “Compare-to” column to show which baseline a run set is targeted against.

- Council feed-through
  - Spec change: Persist council_preflip JSON and render it at the top of claims.md with accept/reject annotations. Author Agent must reconcile and log rationale.

- HPC/backends
  - Spec change: Add runner_backend setting (local|slurm|k8s|ray) with plugin config. Paper Runner maps resource requests appropriately.

TL;DR — Top 3 changes I’d make
1) Introduce a proper Paper Runner and formalize the Author Agent’s contract. The Author Agent plans and writes; the Runner schedules and executes. Move runs/figures/claims to DB with markdown projections to avoid races.
2) Make “claim coverage” the organizing principle. Add multi-seed/statistical rigor, external baseline support, and a Claim Coverage view. Gate readiness on coverage, not a fixed “2–3 claims.”
3) Replace the naive Gantt with a claim-centric critical path and ETA ranges, backed by real throughput stats and explicit resource requests (multi-GPU, CPU/RAM/disk).