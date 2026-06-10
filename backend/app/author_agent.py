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
    return textwrap.dedent(f"""\
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
                                  2-3 tightest paper-worthy claims
   2) paper.lit_review         — find related work; file cite_paper
                                  decisions; rebuild novelty narrative
   3) paper.draft_v0           — scaffold main.tex + sections/*.tex
                                  with TODO markers for tables/figures;
                                  compile a v0 PDF
   4) paper.plan_ablations     — derive the full ablation matrix any
                                  NeurIPS/ICML reviewer would expect
                                  (datasets × model sizes × seeds);
                                  estimate per-run wallclock
   5) paper.build_gantt        — schedule the matrix against the
                                  available GPUs (Gantt chart)
   6) paper.operator_review    — ⛔ STOP. File "request_approval".
                                  Do NOT queue any runs until the
                                  operator clicks Approve.
   7) paper.run_ablations      — only after operator approval; execute
                                  the matrix, fill tables/figures
   8) paper.reviewer_simulator — internal pre-submission review pass
   9) paper.submission_ready   — final PDF + artifact bundle

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
CRITICAL: the OPERATOR-REVIEW GATE
═══════════════════════════════════════════════════════════════════════

After paper.build_gantt finishes, you MUST file a plan-approval
request. You do NOT queue any ablation runs until the operator
approves:

    curl -sS -X POST http://127.0.0.1:8000/api/paper/plan/request_approval \\
         -d '{"note":"<one-line summary of the plan>"}'

Then transition into paper.operator_review. The backend will mark all
new runs with status="proposed" — paper_runner.py refuses to launch
proposed runs until /api/paper/plan/approve is called by the operator.
Once they approve, /api/paper/phase will reflect paper.run_ablations
automatically and you can start polling /api/paper/runs/results.

═══════════════════════════════════════════════════════════════════════
LEGACY DOCUMENTATION (still applies during paper.run_ablations)
═══════════════════════════════════════════════════════════════════════

You are the AUTHOR AGENT for an autonomous ML research project
that has just transitioned from research mode to paper mode. The
research agent is now PAUSED — you are the sole autonomous agent
driving the paper to completion. Your job is to turn this research
into a publishable NeurIPS-style paper, alongside a researcher who
will steer you via chat in the right rail and approve a few strategic
decisions in the queue.

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
FILES YOU OWN INSIDE paper/  (this is a git repo; commits are automatic)
═══════════════════════════════════════════════════════════════════════

  paper/
    main.tex                ← write (you author this)
    sections/*.tex          ← write (you author each section)
    sections/*.user.tex     ← READ ONLY — the user owns these
    figures/<name>.tikz     ← write: a TikZ/pgfplots picture (see FIGURES)
    figures/<name>.csv      ← write: that figure's data (the plot reads it)
    tables/<name>.tex       ← write: a real booktabs table from run numbers
    refs.bib                ← write (jointly with Lit Agent)
    claims.md               ← READ ONLY (projection of paper_claim table)
    paper_figures.md        ← READ ONLY (projection of paper_figure table)
    paper_runs.md           ← READ ONLY (projection of run table)
    lessons.md              ← READ ONLY (from research mode)
    .author_notes.md        ← write your own scratch notes here

Never run `git` directly. Every save is auto-committed.

═══════════════════════════════════════════════════════════════════════
FIGURES + TABLES — data-driven, NEVER matplotlib
═══════════════════════════════════════════════════════════════════════
• Every plot is a TikZ/pgfplots picture in figures/<name>.tikz that reads its
  data from a sibling figures/<name>.csv via
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


# ── lifecycle ─────────────────────────────────────────────────────────────


def _tmux_alive(session: str) -> bool:
    out = subprocess.run(["tmux", "has-session", "-t", session],
                          capture_output=True)
    return out.returncode == 0


def is_running() -> bool:
    return _tmux_alive(SESSION)


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
        # Initial size: frontend xterm.js will POST its real dimensions
        # to /api/agent/resize. 120x40 is a safe default that won't look
        # garbled in any reasonable rail width before the resize.
        subprocess.run(["tmux", "new-session", "-d", "-s", SESSION,
                        "-x", "120", "-y", "40", full],
                       capture_output=True, timeout=10)
        # Mirror the pane to BOTH the per-session raw-byte file (rail
        # xterm.js streaming source) AND author.log (per-workspace
        # persistent log). See backend/app/pane_stream.py.
        from . import pane_stream
        pane_stream.enable(SESSION, mirror_to=str(folder / "author.log"),
                           preserve_history=False)
        # Restore any cached xterm dimensions (see RealAgent.start).
        pane_stream.apply_remembered_size(SESSION)
        # Once Claude Code has booted, hand it the brief. Same dance as
        # agent.py: first auto-accept the (possible) one-time Bypass
        # Permissions consent by typing the literal "2" (the numeric
        # shortcut for "Yes, I accept") then Enter, then send the brief.
        # The numeric path avoids the arrow-key race where Down+Enter
        # could land on the highlighted "No, exit" default.
        if not cmd_override:
            brief = ("Read the file .author_prompt.txt in this directory "
                     "and carry out the paper-writing work it describes. "
                     "Read claims.md, paper_runs.md, paper_figures.md, "
                     "lessons.md FIRST. Then scaffold main.tex and "
                     "sections/*. Do not stop.")
            sess = shlex.quote(SESSION)
            script = (
                "sleep 6 && "
                # accept via numeric shortcut: "2" = "Yes, I accept"
                f"tmux send-keys -t {sess} '2' && sleep 0.5 && "
                f"tmux send-keys -t {sess} Enter && "
                # wait for REPL to actually be ready
                "sleep 10 && "
                # hand it the paper-writing brief
                f"tmux send-keys -t {sess} -l {shlex.quote(brief)} && "
                "sleep 1 && "
                f"tmux send-keys -t {sess} Enter")
            subprocess.Popen(["sh", "-c", script])
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
