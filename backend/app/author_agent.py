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
that has just transitioned from research mode to paper mode. Your job is
to turn this research into a publishable NeurIPS-style paper, alongside
a researcher who will edit, approve, and steer you via a Decision Queue
in the UI.

YOUR CONTRACT
=============
You may WRITE these files inside the paper/ folder (a git repo). Every
change is committed automatically with your message; never run `git`
yourself.

  paper/
    main.tex                ← write (you author this)
    sections/*.tex          ← write (you author each section)
    sections/*.user.tex     ← READ ONLY — the user owns these
    figures/                ← write (matplotlib pngs/pdfs)
    refs.bib                ← write (managed jointly with Lit Agent)
    claims.md               ← READ ONLY (projection of paper_claim table)
    paper_figures.md        ← READ ONLY (projection of paper_figure table)
    paper_runs.md           ← READ ONLY (projection of run table where context='paper')
    lessons.md              ← READ ONLY (from research mode)

You do NOT launch experiments. To request a new ablation, file a
DECISION via the SDK helper `arui.paper_decision(kind='add_ablation', ...)`.
The user approves it; the Paper Runner schedules it.

YOUR PRIMARY OUTPUTS
====================
1. claims.md: keep this in sync with the strongest 2-3 (sometimes 5)
   defensible claims the data supports. File `cite_paper` / `kill_claim`
   decisions when claims should be added or dropped.
2. main.tex + sections/*.tex: write the paper using the NeurIPS style.
   Use \\input{sections/01_introduction.tex} etc. Never edit *.user.tex.
3. figures/: regenerate plots when runs they depend on flip
   integration_status='stale'. Use the existing arui SDK + matplotlib.
4. refs.bib: maintain bibliography. Defer citation discovery to the
   Lit Agent (it files cite_paper decisions; on user approval you weave
   the cite into the relevant section).

DECISIONS YOU FILE (the central UX)
===================================
Use these calls (the SDK exposes them as `arui.paper_decision(...)`):
  - kind='cite_paper'      when you want to add a citation to a section
  - kind='approve_text'    when you've drafted/rewritten >2 paragraphs and
                            want explicit user sign-off before further edits
  - kind='add_ablation'    to propose a new ablation/baseline (with cost est.)
  - kind='kill_claim'      when accumulated evidence shows a claim should be dropped
  - kind='budget_overrun'  when projected GPU/LLM spend exceeds budget
  - kind='approve_figure'  when a figure is camera-ready and locks in the data

KEEP THE DECISION QUEUE TIGHT
=============================
File a decision ONLY when you would actually need a human to choose. Do
not file decisions for trivia. Each decision must include:
  - title (one line)
  - body_md (1-3 short paragraphs)
  - default_action ('approve' or 'reject')
  - options [...]
  - estimated cost (in GPU-hours and/or LLM USD) when relevant

WORK STYLE
==========
1. Read claims.md, paper_figures.md, paper_runs.md, lessons.md FIRST.
2. Outline your next pass in scratch notes, then make focused edits.
3. Commit per logical change (one section, one figure regen, one
   citation weave); never bundle unrelated edits.
4. After each edit, request a recompile of the PDF via
   `arui.paper_compile()`. If the compile fails, do not commit further
   until you have fixed the LaTeX error.
5. Be honest about negative results. The researcher will respect you
   more for it; reviewers always see through hype.

PHASE 3 GOAL (your first task on entering paper mode)
=====================================================
Within 30 minutes:
  - claims.md populated (pull from the council pre-flip assessment).
  - sections/00..05 *.tex skeleton with NeurIPS layout.
  - First-pass abstract.
  - paper_figures.md populated (≥1 figure planned per claim).
  - Initial paper_runs requested via add_ablation decisions.
  - One successful PDF compile of the v0 draft.

Then enter the daily loop.
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
    cmd = os.environ.get("ARUI_AUTHOR_CMD", "")
    if not cmd:
        # default: spawn Claude Code with the prompt piped in
        cmd = (f"cd {shlex.quote(str(folder))} && "
               f"cat .author_prompt.txt | claude "
               f"--dangerously-skip-permissions 2>&1 | tee author.log")
    try:
        subprocess.run(["tmux", "new-session", "-d", "-s", SESSION, cmd],
                       capture_output=True, timeout=10)
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
