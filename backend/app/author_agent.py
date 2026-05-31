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
        venue:   {venue}      deadline: {deadline}
        anonymize for review: {anon}

        ## Council pre-flip assessment (their honest take on novelty)
        {council_block or "(no proposal artifact found)"}
    """)


SYSTEM = """You are the AUTHOR AGENT for an autonomous ML research project
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
    figures/                ← write (matplotlib pngs/pdfs)
    refs.bib                ← write (jointly with Lit Agent)
    claims.md               ← READ ONLY (projection of paper_claim table)
    paper_figures.md        ← READ ONLY (projection of paper_figure table)
    paper_runs.md           ← READ ONLY (projection of run table)
    lessons.md              ← READ ONLY (from research mode)
    .author_notes.md        ← write your own scratch notes here

Never run `git` directly. Every save is auto-committed.

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
    env_prefix = "IS_SANDBOX=1 "  # bypass the root+skip-perms refusal
    if cmd_override:
        inner = cmd_override
    else:
        inner = "claude --dangerously-skip-permissions"
        # Make sure Claude uses the API key (set in env) instead of
        # falling into its OAuth flow. See agent.RealAgent._ensure_claude_settings
        # for the full explanation.
        try:
            from .agent import RealAgent
            RealAgent._ensure_claude_settings()
        except Exception as e:                              # noqa: BLE001
            print(f"[author] apiKeyHelper setup failed: {e}", flush=True)
    full = (f"cd {shlex.quote(str(folder))} && "
            f"{env_prefix}{inner}")
    try:
        # Kill any stale session first (cleanup).
        subprocess.run(["tmux", "kill-session", "-t", SESSION],
                       capture_output=True, timeout=5)
        subprocess.run(["tmux", "new-session", "-d", "-s", SESSION,
                        "-x", "210", "-y", "52", full],
                       capture_output=True, timeout=10)
        # Mirror the pane to author.log for the dashboard tail.
        subprocess.run(["tmux", "pipe-pane", "-t", SESSION, "-o",
                        f"cat >> {shlex.quote(str(folder / 'author.log'))}"],
                       capture_output=True, timeout=5)
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
