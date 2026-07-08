"""Author Agent — the paper-writing autonomous agent.

Spawns a Claude Code (or test-mock) tmux session named 'author' with a
focused system prompt + file-contract. The agent's writes happen via
git commits inside the paper/ folder.

It runs ALONGSIDE the existing research agent (which is itself running
in 'agent' tmux). They share project memory (lessons.md, run history)
but not conversation state.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shlex
import subprocess
import textwrap
import threading
import time
from pathlib import Path

from . import paper
from .bus import bus
from .db import SessionLocal
from .models import (PaperCitation, PaperClaim, PaperFigure, PaperMeta,
                     PaperProposal, PaperSection, Project, Run, Setting)

SESSION = "author"


# ── prompt ────────────────────────────────────────────────────────────────


def _meta_block(db, proposal: PaperProposal | None) -> str:
    meta = db.query(PaperMeta).first()
    proj = db.query(Project).first()
    authors = (meta.authors_json if meta and isinstance(meta.authors_json, list)
                                   else []) or []
    venue = (meta.venue if meta else "NeurIPS 2026")
    deadline = (meta.deadline_iso if meta else "")
    anon = "yes" if (meta.anonymize if meta else True) else "no"
    purpose = (proj.purpose if proj else "") or ""
    metric = (proj.validation_metric if proj else "") or ""
    direction = (proj.metric_direction if proj else "") or ""
    council_block = ""
    if proposal:
        try:
            for rev, body in (proposal.council_responses or {}).items():
                council_block += f"\n=== {rev} ===\n{json.dumps(body)[:1500]}\n"
        except Exception:
            pass
    try:
        from . import purpose as _purpose
        _anchor = _purpose.anchor_block(
            header="RESEARCH PURPOSE — the paper must serve this")
    except Exception:                                      # noqa: BLE001
        _anchor = ""
    _anchor = (_anchor + "\n\n") if _anchor else ""
    return _anchor + textwrap.dedent(f"""\
        ## Project
        purpose: {purpose}
        metric:  {metric}  (direction: {direction})
        authors: {[a.get("name") for a in authors]}
        venue:   {venue}   (no deadline — quality-gated, not time-gated)
        anonymize for review: {anon}

        ## Council pre-flip assessment (their honest take on novelty)
        {council_block or "(no proposal artifact found)"}
    """)


SYSTEM = """You are the AUTHOR AGENT. You must drive a research project to a
submission-ready NeurIPS paper by walking through these phases IN ORDER:

   1) paper.whittle_claims     — read research-mode kept runs; pick the
                                  2-3 tightest paper-worthy claims and FILE
                                  each one:
                                    curl -sS -X POST $ARUI_INGEST_URL/api/paper/claims \
                                      -H 'Content-Type: application/json' \
                                      -d '{"title":"...","summary_md":"...",
                                           "evidence_strength":"strong|suggestive",
                                           "novelty":"high|medium|low"}'
                                  (claims.md is a READ-ONLY projection; you
                                  cannot create a claim by editing it.)
   2) paper.lit_review         — REUSE the research-phase scoping lit
                                  review as your starting point (read it
                                  from GET /api/scope/status -> papers/
                                  synthesis), then BOLSTER it with the
                                  deeper related work the final claims
                                  need; add citations DIRECTLY to refs.bib
                                  (no cite_paper approvals — autopilot);
                                  rebuild the novelty narrative
   3) paper.draft_v0           — scaffold main.tex + sections/*.tex AND
                                  the bare TODO tables/<name>.tex +
                                  tikz/<name>.tikz(+.csv) skeleton for
                                  every claim; compile a v0 PDF
   4) paper.plan_ablations     — READ .author_plan_prompt.md and EXECUTE it
                                  exactly: it is your meta-prompt for turning
                                  each figure into the full training-run matrix
                                  (register figures via POST /api/paper/figures
                                  to get figure_id, then queue one run per grid
                                  cell -- model_size x lr x seed -- each with
                                  figure_id + train_args + est_time_sec +
                                  gpus_required). This fills the Critical Path
                                  Gantt.
   5) paper.build_gantt        — GET /api/paper/gantt for the real
                                  dependency- + GPU-constrained schedule
                                  (start/end, makespan, critical path) and
                                  render it as an ACTUAL Gantt chart
   6) paper.run_ablations      — queue the matrix immediately (runs
                                  auto-queue, there is NO operator approval
                                  gate); execute, fill tables/figures
   7) paper.reviewer_simulator — advisory pre-submission review pass
   8) paper.submission_ready   — final PDF + artifact bundle

AUTOPILOT — there are NO human approval gates. Do NOT stop and wait for the
operator to approve anything (no request_approval, no approve_text, no
approve_figure). Just keep going: write, queue runs, integrate results,
recompile, improve. The PI agent and the council review every revision (see
the review loop below) and message you directly with required fixes; treat
their messages as your gate, not a human click.

There is NO conference deadline. The paper is QUALITY-gated, not time-gated:
do not rush phases or thin the ablations to "make a date". It ships when the
science + the writing clear the gates, however long that takes.

═══════════════════════════════════════════════════════════════════════
WRITING STYLE — NON-NEGOTIABLE (an automated lint pass BLOCKS the bundle)
═══════════════════════════════════════════════════════════════════════
• NEVER use an em-dash anywhere. Not the unicode em-dash, not the LaTeX
  triple-hyphen, not a spaced double-hyphen, not an en-dash between words.
  Use a comma, a colon, parentheses, or a period instead. (Numeric ranges
  written as 1--3 are fine.) The operator HATES em-dashes; the linter rejects
  the paper if it finds one.
• NEVER use the AI-slop antithesis. No "it's not X, it's Y"; no "not just /
  only X but Y"; no "isn't just X, it's Y". Avoid tells like "delve",
  "tapestry", "testament to", "paradigm shift", "it's worth noting". No one
  writes technical papers like that.
• Structure the STORY in Jen Widom form. The Introduction is EXACTLY five
  paragraphs answering, in order: (1) what is the problem, (2) why it is
  interesting and important, (3) why it is hard / why naive approaches fail,
  (4) why it has not been solved before / how ours differs, (5) the key
  components of the approach, the results, and explicit limitations. End the
  Introduction with a "Summary of Contributions" bullet list, each bullet
  naming the section that delivers it. Run a spell-check before every compile.

═══════════════════════════════════════════════════════════════════════
MANDATORY: report each transition immediately
═══════════════════════════════════════════════════════════════════════

At every phase entry, FIRST call (do this before anything else):

    curl -sS -X POST http://127.0.0.1:8000/api/paper/phase \\
         -H 'Content-Type: application/json' \\
         -d '{"phase":"<phase>","actor":"author",
              "progress":{...},"detail":{...}}'

The dashboard pill + Issues list read this directly. If you do not
report, the operator sees an empty page.

═══════════════════════════════════════════════════════════════════════
AUTOPILOT: no operator gate — the PI + council are your reviewers
═══════════════════════════════════════════════════════════════════════

After paper.build_gantt, queue your ablation runs IMMEDIATELY and move to
paper.run_ablations. There is NO operator approval step: runs auto-queue and
paper_runner launches them. Do not file request_approval / approve_text /
approve_figure and do not wait for any human click.

Your real review loop is the PI agent and the council. After EACH meaningful
revision (new section draft, integrated result, figure/table change), expect a
message in your tmux pane from the PI with required fixes, especially on:
  • Jen Widom structure (the exact 5-paragraph intro + Summary of Contributions)
  • NOVELTY: every claim must be sharply differentiated from related work
  • the writing-style rules (no em-dash, no AI-slop antithesis)
Apply their fixes, recompile, commit (every edit is committed AND pushed to
GitHub automatically), and continue. Keep iterating until the PI/council stop
finding issues. THEY are the gate, not a human.

═══════════════════════════════════════════════════════════════════════
LEGACY DOCUMENTATION (still applies during paper.run_ablations)
═══════════════════════════════════════════════════════════════════════

You are the AUTHOR AGENT for an autonomous ML research project
that has just transitioned from research mode to paper mode. The
research agent is now PAUSED — you are the sole autonomous agent
driving the paper to completion. Your job is to turn this research
into a publishable NeurIPS-style paper. You run on AUTOPILOT: no human
approves anything. The PI agent + council review each revision and message
you with fixes; a researcher may also steer you via chat in the right rail.

═══════════════════════════════════════════════════════════════════════
YOUR CONTRACT (read carefully — the design here is different from v1)
═══════════════════════════════════════════════════════════════════════

You have FULL AUTONOMY over the ablation queue. You decide what
ablations to run, you queue them, you watch the results stream in via
the arui SDK, you kill divergers, you integrate finished runs into
the paper. The user does NOT have to approve each ablation. The
decision queue is reserved for STRATEGIC choices only (see below).

The Paper Runner is your executor — it bin-packs your queued runs
onto idle GPUs and launches them. You don't talk to GPUs directly;
you talk to it via the backend HTTP API (curl from your bash tool).

═══════════════════════════════════════════════════════════════════════
HOW TO QUEUE, KILL, AND INSPECT RUNS
═══════════════════════════════════════════════════════════════════════

The backend is at http://127.0.0.1:8000. All endpoints take JSON.

QUEUE A NEW RUN
  curl -sS -X POST http://127.0.0.1:8000/api/paper/runs/queue \\
       -H 'Content-Type: application/json' -d '{
         "name": "headline_initar_lr5e4_s11",
         "claim_id": "pc-…",
         "role": "headline",
         "train_args": "--mode diff --name headline_initar_lr5e4_s11 \\
                        --seed 11 --lr 5e-4 --init_from ckpts/ar_seed2.pt \\
                        --t_schedule uniform"
       }'
  → returns {"ok": true, "id": "pr-…"}

  You can pass `cmd` explicitly instead of `train_args` if you need
  something other than the standard `python train.py` invocation.

QUEUE MANY AT ONCE
  curl -sS -X POST http://127.0.0.1:8000/api/paper/runs/queue_batch \\
       -H 'Content-Type: application/json' -d '{"runs":[{…},{…},{…}]}'

KILL A DIVERGING RUN
  curl -sS -X POST http://127.0.0.1:8000/api/paper/runs/pr-xxx/kill

INSPECT RESULTS (what finished, what failed, what's the metric)
  curl -sS http://127.0.0.1:8000/api/paper/runs/results
  curl -sS 'http://127.0.0.1:8000/api/paper/runs/results?status=kept,success,done'
  curl -sS 'http://127.0.0.1:8000/api/paper/runs/results?since=2026-05-28T10:00:00Z'

UPDATE A CLAIM (when evidence comes in)
  curl -sS -X PUT http://127.0.0.1:8000/api/paper/claims/pc-…/update \\
       -H 'Content-Type: application/json' -d '{
         "evidence_strength": "strong",
         "ready": true,
         "summary_md": "…updated summary based on s2+s5 ensemble results…"
       }'

═══════════════════════════════════════════════════════════════════════
FILES YOU OWN INSIDE latex/  (tracked in the project repo; commits+push auto)
═══════════════════════════════════════════════════════════════════════

  latex/
    main.tex                ← write (you author this)
    sections/*.tex          ← write (you author each section)
    sections/*.user.tex     ← READ ONLY — the user owns these
    tikz/<name>.tikz        ← write: a TikZ/pgfplots picture (see FIGURES)
    tikz/<name>.csv         ← write: that figure's data (the plot reads it)
    tables/<name>.tex       ← write: a real booktabs table from run numbers
    refs.bib                ← write (jointly with Lit Agent)
    claims.md               ← READ ONLY (projection of paper_claim table)
    paper_figures.md        ← READ ONLY (projection of paper_figure table)
    paper_runs.md           ← READ ONLY (projection of run table)
    lessons.md              ← READ ONLY (from research mode)
    .author_notes.md        ← write your own scratch notes here

ALL tikz pictures + their .csv data go under latex/tikz/. Never run `git`
directly: every save is auto-committed AND pushed to GitHub for you.

═══════════════════════════════════════════════════════════════════════
FIGURES + TABLES — data-driven, NEVER matplotlib
═══════════════════════════════════════════════════════════════════════
• Every plot is a TikZ/pgfplots picture in tikz/<name>.tikz that reads its
  data from a sibling tikz/<name>.csv via
  `\\addplot table[x=x, y=<series>, col sep=comma]{<name>.csv};`.
  NEVER use matplotlib, pyplot, savefig, or any raster/.png image, and never
  \\includegraphics a generated plot. The numbers in the CSV come from real
  runs (read them from the metric API / paper_runs.md), so a plot can be
  refreshed by rewriting its CSV and recompiling, with no code to touch.
• Every results table is a real booktabs table in tables/<name>.tex whose
  numbers come from the runs, not typed by hand.
• Build the SKELETON first: create the bare TODO table + figure stubs for
  every claim BEFORE running ablations, then plan the exact runs needed to
  fill each cell, then fill them. A figure/table left with TODO blocks the
  bundle.
• If a figure's underlying runs change after the operator approved it, the
  figure is STALE and must be re-approved — refresh the CSV, do not ship old
  numbers under a new caption.

FIGURE + TABLE QUALITY BAR — every figure must EARN its page
───────────────────────────────────────────────────────────────────────
A reviewer skims figures + captions first. If a figure doesn't carry a
result, it does not belong in the paper. Hold every figure/table to this:

• NAME EVERY METHOD EXPLICITLY. Never write a vague label like "ZO",
  "ZO ensemble", "ZO method", "ours", or "baseline" in a cell, legend, axis,
  caption, or table header. Always state the exact optimizer/algorithm:
  NES, SPSA, DFA, BP (backprop), etc. If a table or figure compares
  ensembles, each row/series says WHICH zero-order method it used. Every
  acronym is defined at first use in the caption AND in the text. A reader
  must never have to guess what "ZO ensemble" means.
• NO INFRASTRUCTURE / PROCESS FIGURES. A schedule, a critical-path Gantt of
  the run matrix, a pipeline diagram, GPU utilisation, etc. are NOT results
  and must NOT appear in the paper — they belong (at most) in an appendix or
  the dashboard. Cut them. Every main-body figure shows a scientific result
  (a metric vs a controlled variable, an ablation, a comparison).
• HEATMAPS: overlay the numeric value in EVERY cell (e.g. the validation
  loss in nats), formatted to a sensible precision, in a colour that stays
  readable on every background. BOLD the best (lowest val-nats / best-metric)
  cell — per row if rows are comparable, else per figure. Add a colour bar
  with the metric name + units. A heatmap with no in-cell numbers is not
  acceptable.
• SELF-CONTAINED. Axis labels carry units ("validation loss (nats)", not
  "loss"). Legends name the methods. The caption states what is plotted, the
  units, what each series/method is, and the one-sentence takeaway. A reader
  who looks only at the figure + caption understands the point.
• Prefer fewer, sharper figures. If two figures say the same thing, merge or
  cut. Aim for every figure to be referenced and discussed in the text.

═══════════════════════════════════════════════════════════════════════
STRATEGIC DECISIONS — these still need user approval
═══════════════════════════════════════════════════════════════════════

These are the ONLY kinds of decisions you should file. Ablation
launches do NOT file decisions anymore — you queue them directly.

  cite_paper      — "should we cite Lou et al. SEDD 2024 in §2?"
                    The user weighs in on bibliography choices.
  kill_claim      — "the data shows claim 3 is dead; permission to drop?"
                    Big call. Needs the user.
  approve_text    — "I've rewritten §4 substantially; please sign off
                    before I commit further changes downstream."
                    Sparingly. Only for major rewrites.
  approve_figure  — "this figure is camera-ready; locks in the data."

File via curl:
  curl -sS -X POST http://127.0.0.1:8000/api/paper/decisions \\  # see api.py
       -d '{"kind":"cite_paper","title":"…","body_md":"…", …}'

Or, more conveniently, write to the decision queue via the existing
DB shape — the user will see it in their queue in the Today view.

═══════════════════════════════════════════════════════════════════════
MULTI-DATASET / MULTI-ENVIRONMENT — what makes a GREAT paper
═══════════════════════════════════════════════════════════════════════

Top-tier conference papers (NeurIPS, ICML, ICLR) don't get accepted on
single-dataset results. For each claim you make, try to validate on
≥3 datasets, ≥2 model sizes when possible. Reviewers will reject a
claim that holds only on GSM8K.

YOUR FIRST RESPONSIBILITY on each claim:
  1. Inspect the project's available datasets — read `data/`,
     `prepare.py`, `program.md` to learn what's supported.
     Example: this repo trains on GSM8K via `--dataset gsm8k`. Check
     prepare.py for other registered datasets.
  2. For each claim, plan a cross-dataset validation matrix:
       claim × {dataset_1, dataset_2, dataset_3} × ≥3 seeds
  3. If the project only ships one dataset, file a strategic decision
     `kind=approve_text` asking the user whether to add scaffolding
     for dataset_2 (cite prior work that uses it). Don't silently
     ship a single-dataset paper.

Same goes for model size — vary it where the project supports it.
Robustness checks (ablate components, dtype, schedule, batch size)
strengthen every claim.

═══════════════════════════════════════════════════════════════════════
WORK STYLE
═══════════════════════════════════════════════════════════════════════

1. On startup: read claims.md, paper_runs.md, paper_figures.md,
   lessons.md FIRST. Understand what evidence already exists.
2. For each ACTIVE claim, plan an ablation schedule: headline run +
   2-3 ablations × N seeds. Queue them all up front. Let them run.
3. Poll /paper/runs/results every few minutes. As runs finish:
   - Kill divergers (train_loss > 1.5 × min, or NaN, or wildly noisy)
   - Update claim evidence_strength based on results
   - Queue follow-up ablations if a result suggests a new direction
4. As you accumulate >5 strong members for a claim, build an ensemble
   eval (eval_ensemble.py) and add the result to the paper.
5. Once you have enough evidence for a claim, write the section that
   uses it. Commit per logical change.
6. After each LaTeX edit, request a recompile:
     curl -sS -X POST http://127.0.0.1:8000/api/paper/recompile
   If compile fails, fix LaTeX errors before continuing.
7. Be honest about negative results. The user respects you more
   for it; reviewers always see through hype.

═══════════════════════════════════════════════════════════════════════
PHASE 3 GOAL (your first task on entering paper mode)
═══════════════════════════════════════════════════════════════════════

Within 30 minutes:
  - Read claims.md and confirm the 2-3 strongest claims with the user.
  - Queue 6-15 ablations across the active claims (headlines + ablations
    × ≥3 seeds each).
  - Scaffold sections/00_abstract.tex through 06_conclusion.tex with
    placeholder content + a v0 PDF that compiles.
  - paper_figures.md plan with ≥1 figure per claim.
  - First strategic decision filed if anything needs user attention
    (e.g., cite a key prior work, kill a weak claim).

Then enter the daily loop: monitor → kill divergers → integrate
results → write sections → recompile → repeat.
"""


# ── plan meta-prompt (run enumeration per figure) ──────────────────────────
# Written to <latex>/.author_plan_prompt.md on setup. The author READS +
# EXECUTES it at paper.plan_ablations (after draft_v0, once claims + a figure
# list exist) to turn every figure into the exact training-run matrix and queue
# it, so the Critical Path Gantt fills in.
_PLAN_META_PROMPT = """# META-PROMPT: enumerate the run matrix per figure

INVOKE THIS after draft_v0 (you have a first draft, a claims list, and a figure
list). Goal: turn EACH figure into the exact set of training runs that produce
it, and queue them ALL so the Critical Path Gantt populates.

STEP 0 - read train.py to learn its EXACT flags (model size, lr, seed, dataset).

STEP 1 - register each figure to get a figure_id:
  curl -sS -X POST $ARUI_INGEST_URL/api/paper/figures \\
    -H 'Content-Type: application/json' \\
    -d '{"title":"Figure 1: Val Acc (best) vs. Model Size","kind":"line","claim_id":"pc-..."}'
  # -> {"ok":true,"id":"pf-..."}  save each id.
The figures to build (add more if a claim needs them):
  Figure 1: Val Acc (best) vs. Model Size
  Figure 2: Best LR vs. Model Size

STEP 2 - enumerate EVERY run. A "vs model size" plot that takes the best over
LR and the mean over seeds needs the FULL grid: model_sizes x learning_rates x
seeds. Example: 5 sizes x 10 LRs x 3 seeds = 150 runs. A run that feeds BOTH
figures counts ONCE: tag it to one figure_id (e.g. all sweep runs -> Figure 1;
Figure 2 reads the same runs).

STEP 3 - queue the WHOLE grid for each figure in ONE call with
/api/paper/runs/enumerate. Give it the arg_template (with {placeholders}) and
the axis value lists; it expands the cartesian product and queues one run per
cell tagged to the figure. THIS IS REQUIRED - do not skip it, do not just
describe the plan. The Critical Path Gantt is empty until you do this.
  curl -sS -X POST $ARUI_INGEST_URL/api/paper/runs/enumerate \\
    -H 'Content-Type: application/json' \\
    -d '{"figure_id":"pf-...","claim_id":"pc-...","name_prefix":"f1",
         "arg_template":"--model {model} --lr {lr} --seed {seed} --mode diff",
         "axes":{"model":["EleutherAI/pythia-70m","EleutherAI/pythia-160m",
                           "EleutherAI/pythia-410m"],
                 "lr":[1e-4,3e-4,1e-3],"seed":[0,1,2]},
         "est_time_sec":75600,"gpus_required":1}'
  # -> {"ok":true,"n":27,...}. est_time_sec = wall-clock per run (21 h=75600 s).
  # The scheduler packs all runs across the REAL GPU count -> true makespan.
  # arg_template placeholders MUST match train.py's real flags (you read them
  # in STEP 0) and the axes keys.

RULES
  - Call enumerate ONCE per figure (a run that feeds 2 figures: tag it to one).
  - arg_template flags must be the EXACT flags train.py accepts.
  - Queue them ALL up front (autopilot: no approval gate).
  - After enumerating, GET /api/paper/gantt and confirm tasks is non-empty.
Then POST /api/paper/phase {"phase":"paper.run_ablations"} and open the Critical
Path tab to confirm the Gantt filled in. Poll /paper/runs/results and integrate
results into the figures as runs finish.
"""


# ── lifecycle ─────────────────────────────────────────────────────────────


def _tmux_alive(session: str) -> bool:
    out = subprocess.run(["tmux", "has-session", "-t", session],
                          capture_output=True)
    return out.returncode == 0


def is_running() -> bool:
    return _tmux_alive(SESSION)


# ── robust brief-feed ──────────────────────────────────────────────────────
# The author boots into the Claude Code welcome screen. A fixed-sleep
# send-keys dance ("sleep 6 -> 2 -> Enter -> sleep 10 -> brief") is flaky with
# newer Claude Code (the Fable announce screen / model picker intercepts), so
# the author could end up alive-but-idle, parked at the prompt, never having
# received its instructions. This feeds the brief by POLLING the pane for
# readiness, dismissing the trust/consent prompt only if it actually appears,
# then verifying the brief was accepted (retrying once if it sits queued).

_AUTHOR_BRIEF = (
    "Read .author_prompt.txt in this directory and carry out the "
    "paper-writing work it describes. First read claims.md, paper_runs.md, "
    "paper_figures.md, lessons.md. Report each phase via POST "
    "/api/paper/phase. You are on AUTOPILOT: there are no human approval "
    "gates, so do not stop and wait for anyone. Keep going until the PI and "
    "council stop finding issues. "
    "BUT: if the research you were handed is a NEGATIVE / NULL result — its "
    "central finding is that nothing worked, the baseline was not beaten, or "
    "the problem is 'unsolvable' — do NOT write it up as a finished paper. A "
    "paper needs a genuine POSITIVE contribution. Instead POST /api/paper/phase "
    "with a blocker note that the research produced no positive result to "
    "publish and must return to Research mode for a real result; do not polish "
    "a negative result into a submission."
)

# Substrings that mean Claude Code is actively working (brief was accepted).
_BUSY_MARKERS = ("cultivat", "waddl", "tokens", "running ", "thinking",
                 "↑", "esc to interrupt to")


def _pane_text(session: str) -> str:
    out = subprocess.run(["tmux", "capture-pane", "-t", session, "-p"],
                         capture_output=True, text=True)
    return out.stdout if out.returncode == 0 else ""


def _send_keys(session: str, *args, literal: str | None = None) -> None:
    if literal is not None:
        subprocess.run(["tmux", "send-keys", "-t", session, "-l", literal],
                       capture_output=True)
    else:
        subprocess.run(["tmux", "send-keys", "-t", session, *args],
                       capture_output=True)


def _looks_busy(session: str) -> bool:
    low = _pane_text(session).lower()
    return any(m in low for m in _BUSY_MARKERS)


# Per-session feed lock: start(), refeed_if_idle(), and the watchdog restart
# can each spawn a feed_brief thread for the same `author` pane. Two feeders
# racing tmux send-keys interleave keystrokes and garble the brief. Serialize
# them: if a feed is already in flight for this session, the new one bows out.
_FEED_LOCKS: dict[str, threading.Lock] = {}
_FEED_LOCKS_GUARD = threading.Lock()


def _feed_lock(session: str) -> threading.Lock:
    with _FEED_LOCKS_GUARD:
        lk = _FEED_LOCKS.get(session)
        if lk is None:
            lk = _FEED_LOCKS[session] = threading.Lock()
        return lk


def feed_brief(session: str = None, brief: str = None,
               ready_timeout: int = 70) -> bool:
    """Hand the author its brief, serialized per session so two feeders never
    interleave keystrokes into the same pane. Returns False immediately if a
    feed is already in flight for this session."""
    session = session or SESSION
    lock = _feed_lock(session)
    if not lock.acquire(blocking=False):
        return False
    try:
        return _feed_brief_inner(session=session, brief=brief,
                                 ready_timeout=ready_timeout)
    finally:
        lock.release()


def _feed_brief_inner(session: str = None, brief: str = None,
               ready_timeout: int = 70) -> bool:
    """Reliably hand the author its brief. Returns True if the agent looks
    busy afterward. Safe to call again (re-feed) on an idle-but-alive author."""
    session = session or SESSION
    brief = brief or _AUTHOR_BRIEF
    if not _tmux_alive(session):
        return False
    # 1) Wait for the REPL to be interactive; dismiss a consent prompt ONCE
    #    if it actually shows (numeric "2" = "Yes, I accept").
    consent_done = False
    deadline = time.time() + ready_timeout
    ready = False
    while time.time() < deadline:
        txt = _pane_text(session)
        low = txt.lower()
        if not consent_done and ("do you trust" in low
                                 or "yes, i accept" in low
                                 or ("bypass permissions" in low
                                     and "accept" in low)):
            _send_keys(session, literal="2")
            time.sleep(0.5)
            _send_keys(session, "Enter")
            consent_done = True
            time.sleep(3)
            continue
        # REPL prompt box "❯" or the tips line both mean it is ready for input.
        if "❯" in txt or 'try "' in low or "/help" in low:
            ready = True
            break
        time.sleep(2)
    if not ready:
        time.sleep(3)                       # last-ditch: feed anyway
    # 2) Type the brief and submit.
    time.sleep(1)
    _send_keys(session, literal=brief)
    time.sleep(0.7)
    _send_keys(session, "Enter")
    # 3) Verify it started; if it sits queued/idle, nudge Enter then re-type.
    time.sleep(9)
    if _looks_busy(session):
        return True
    _send_keys(session, "Enter")
    time.sleep(4)
    if _looks_busy(session):
        return True
    _send_keys(session, literal=brief)
    time.sleep(0.7)
    _send_keys(session, "Enter")
    time.sleep(6)
    return _looks_busy(session)


def refeed_if_idle() -> bool:
    """Supervisor hook: if the author is ALIVE but has not started working
    (no phase reported and the pane is not busy), re-feed the brief. Returns
    True if a re-feed was attempted."""
    if not _tmux_alive(SESSION):
        return False
    if _looks_busy(SESSION):
        return False
    if os.environ.get("ARUI_DISABLE_BG"):
        return False
    threading.Thread(target=feed_brief, daemon=True,
                     name="author-refeed").start()
    return True


def _record_spawn() -> None:
    from .models import Setting
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(
            Setting.key == "paper.author_spawn_at").first()
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        if row is None:
            db.add(Setting(key="paper.author_spawn_at", value={"at": now}))
        else:
            row.value = {"at": now}
        db.commit()
    finally:
        db.close()


def spawn_age_sec() -> float:
    """Seconds since the author tmux was last (re)spawned, or a large number
    if unknown. Lets the supervisor wait out a normal boot before re-feeding."""
    from .models import Setting
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(
            Setting.key == "paper.author_spawn_at").first()
        if not row or not isinstance(row.value, dict):
            return 1e9
        try:
            t = dt.datetime.fromisoformat(row.value.get("at", ""))
            if t.tzinfo is None:
                t = t.replace(tzinfo=dt.timezone.utc)
            return (dt.datetime.now(dt.timezone.utc) - t).total_seconds()
        except Exception:
            return 1e9
    finally:
        db.close()


def start(proposal_id: str = "") -> dict:
    """Spawn the Author Agent in tmux 'author'. Idempotent."""
    if _tmux_alive(SESSION):
        return {"status": "already_running"}
    db = SessionLocal()
    try:
        proposal = (db.query(PaperProposal)
                    .filter(PaperProposal.id == proposal_id).first()
                    if proposal_id else None)
        meta_block = _meta_block(db, proposal)
    finally:
        db.close()
    folder = paper.paper_folder()
    if not folder:
        return {"status": "error", "detail": "no paper folder"}
    paper.ensure_paper_repo(folder)
    # Drop the run-enumeration meta-prompt so the author can read + execute it
    # at the plan_ablations phase (after draft_v0).
    try:
        (folder / ".author_plan_prompt.md").write_text(_PLAN_META_PROMPT)
    except Exception as e:                                  # noqa: BLE001
        print(f"[author] could not write plan meta-prompt: {e}", flush=True)
    # Bootstrap files the agent will read first.
    paper.write_projections()
    # Prompt the agent receives on first turn.
    prompt = SYSTEM + "\n\n" + meta_block
    prompt_path = folder / ".author_prompt.txt"
    prompt_path.write_text(prompt)
    # Compose the command. For dev, we use ARUI_AUTHOR_CMD env var to swap
    # in a mock agent. In prod the default is `claude --dangerously-skip-permissions`.
    # IMPORTANT: Claude Code refuses `--dangerously-skip-permissions` as root
    # unless `IS_SANDBOX=1` is set in the env. The research agent (agent.py)
    # has this dance; we mirror it here. We ALSO must spawn Claude
    # interactively (no piped stdin) and feed the prompt via `tmux send-keys`
    # after it has booted, otherwise Claude's REPL never starts.
    cmd_override = os.environ.get("ARUI_AUTHOR_CMD", "")
    # Expose the dashboard passcode (if any) so the author agent's
    # arui SDK + curl calls auto-authenticate against the local
    # backend — same reasoning as agent.py (avoid the "agent
    # forensically discovers the passcode" detour).
    _token_export = ""
    try:
        from . import auth as _auth
        _pc = _auth._saved_passcode()
        if _pc:
            _token_export = f"ARUI_INGEST_TOKEN={shlex.quote(_pc)} "
    except Exception:                                       # noqa: BLE001
        pass
    # AUTH (2026-06-05 Francois bug report): Claude Code launched with
    # an empty ANTHROPIC_API_KEY drops into the OAuth path and the
    # author pane just sits at "Not logged in · Run /login" forever.
    # Pull the key from BOTH (a) the process env and (b) the onboarding
    # Setting row, and pass it as an explicit env-var prefix into the
    # tmux command line so Claude Code 2.1.x can't miss it.
    _claude_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not _claude_key:
        try:
            from .db import SessionLocal as _SL
            from .models import Setting as _S
            db = _SL()
            try:
                row = db.query(_S).filter(_S.key == "onboarding").first()
                if row and isinstance(row.value, dict):
                    _claude_key = (row.value.get("claude_token")
                                    or "").strip()
            finally:
                db.close()
        except Exception as e:                              # noqa: BLE001
            print(f"[author] could not read onboarding key: {e}",
                  flush=True)
    # Sync back into process env so subprocess inherits it.
    if _claude_key:
        os.environ["ANTHROPIC_API_KEY"] = _claude_key
    _key_export = (f"ANTHROPIC_API_KEY={shlex.quote(_claude_key)} "
                    if _claude_key else "")
    env_prefix = f"{_token_export}{_key_export}IS_SANDBOX=1 "
    if cmd_override:
        inner = cmd_override
    else:
        # DEFAULT claude — its normal pretty TUI (boxes, ● bullets, tree lines),
        # exactly like the research agent. We get scrollback NOT by changing how
        # claude renders, but by telling tmux to IGNORE the alternate screen (see
        # `alternate-screen off` below): the full-screen TUI then paints into the
        # NORMAL buffer, so the pane keeps real scrollback (scroll to the first
        # message + select + copy) while looking exactly like Claude Code should.
        inner = "claude --dangerously-skip-permissions"
        # Make sure Claude uses the API key (set in env) instead of
        # falling into its OAuth flow. See agent.RealAgent._ensure_claude_settings
        # for the full explanation.
        try:
            from .agent import RealAgent
            RealAgent._ensure_claude_settings(_claude_key)
        except Exception as e:                              # noqa: BLE001
            print(f"[author] apiKeyHelper setup failed: {e}", flush=True)
    full = (f"cd {shlex.quote(str(folder))} && "
            f"{env_prefix}{inner}")
    try:
        # Kill any stale session first (cleanup).
        subprocess.run(["tmux", "kill-session", "-t", SESSION],
                       capture_output=True, timeout=5)
        # Create the session as a bare shell, turn OFF tmux's alternate-screen
        # for this window (so Claude Code's TUI renders into the scrollback-
        # bearing normal buffer), THEN launch Claude. Order matters: the option
        # must be set before claude emits its alt-screen-enter sequence.
        subprocess.run(["tmux", "new-session", "-d", "-s", SESSION,
                        "-x", "120", "-y", "40"],
                       capture_output=True, timeout=10)
        subprocess.run(["tmux", "set-window-option", "-t", SESSION,
                        "alternate-screen", "off"],
                       capture_output=True, timeout=5)
        subprocess.run(["tmux", "send-keys", "-t", SESSION, full, "Enter"],
                       capture_output=True, timeout=5)
        # Mirror the pane to BOTH the per-session raw-byte file (rail
        # xterm.js streaming source) AND author.log (per-workspace
        # persistent log). See backend/app/pane_stream.py.
        from . import pane_stream
        pane_stream.enable(SESSION, mirror_to=str(folder / "author.log"),
                           preserve_history=False)
        # Restore any cached xterm dimensions (see RealAgent.start).
        pane_stream.apply_remembered_size(SESSION)
        # Record spawn time so the supervisor can tell "still booting" from
        # "parked idle, never got the brief" (see refeed_if_idle).
        _record_spawn()
        # Once Claude Code has booted, hand it the brief via the robust,
        # polling feeder (waits for readiness, dismisses consent only if it
        # shows, verifies the brief was accepted, retries if it sits queued).
        # Run in a background thread so start() returns immediately.
        if not cmd_override and not os.environ.get("ARUI_DISABLE_BG"):
            threading.Thread(target=feed_brief, daemon=True,
                             name="author-feed").start()
    except Exception as e:
        return {"status": "error", "detail": str(e)}
    bus.publish("paper", "author_started", {})
    return {"status": "started", "tmux": SESSION}


def stop() -> dict:
    """Kill the author tmux session (used on revert and on reset)."""
    if not _tmux_alive(SESSION):
        return {"status": "not_running"}
    subprocess.run(["tmux", "kill-session", "-t", SESSION],
                   capture_output=True, timeout=10)
    bus.publish("paper", "author_stopped", {})
    return {"status": "stopped"}


def send(text: str) -> bool:
    if not _tmux_alive(SESSION):
        return False
    subprocess.run(["tmux", "send-keys", "-t", SESSION, "-l", text],
                   capture_output=True, timeout=8)
    subprocess.run(["tmux", "send-keys", "-t", SESSION, "Enter"],
                   capture_output=True, timeout=8)
    return True


def terminal_tail(n: int = 200) -> str:
    if not _tmux_alive(SESSION):
        # fall back to author.log on disk
        folder = paper.paper_folder()
        if folder and (folder / "author.log").exists():
            try:
                return (folder / "author.log").read_text(errors="ignore")[-30000:]
            except OSError:
                pass
        return ""
    try:
        out = subprocess.run(
            ["tmux", "capture-pane", "-t", SESSION, "-p", "-S", str(-n)],
            capture_output=True, text=True, timeout=8)
        return out.stdout
    except Exception:
        return ""
