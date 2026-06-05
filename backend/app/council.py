"""LLM council — post-experiment deliberation with debate.

After every run finishes, both Gemini and GPT (whichever keys are configured)
independently review the run. They then DEBATE up to N rounds, each round
seeing the other's last position and either revising or holding. If they
agree (matching verdict + same top-3 rerank set + same veto set), the debate
ends and the consensus is applied. Otherwise Claude is asked to break the
tie. All round outputs are persisted on the run so the UI can show the
back-and-forth.

Cost controls (configurable via Settings):
  - run_debate: bool (if false, just one independent review per reviewer,
    no debate)
  - debate_max_rounds: int (default 3)
  - per-model selection: council_gemini_model, council_openai_model,
    council_openai_effort ("low" / "medium" / "high"), council_claude_model

Keys come from .deploy/keys.env (gitignored).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from .config import DATA_DIR, ROOT
from .db import SessionLocal
from .models import ChatMessage, Event, Gpu, Idea, Project, Run, Setting

# ── env / keys ────────────────────────────────────────────────────────────
_KEYS_PATH = ROOT / ".deploy" / "keys.env"


def _load_keys_env() -> None:
    """Best-effort load of .deploy/keys.env so the council sees API keys
    without the user having to source the file before starting the backend."""
    if not _KEYS_PATH.exists():
        return
    try:
        for ln in _KEYS_PATH.read_text().splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#") or "=" not in ln:
                continue
            k, v = ln.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and v and not os.environ.get(k):
                os.environ[k] = v
    except Exception as e:                              # noqa: BLE001
        print(f"[council] could not read keys.env: {e}", flush=True)


_load_keys_env()


# ── settings (live-read from the onboarding Setting row each invocation) ─
DEFAULTS = {
    "council_gemini_model": "gemini-2.5-pro",
    "council_openai_model": "gpt-5",
    "council_openai_effort": "high",
    "council_claude_model": "claude-opus-4-6",
    "run_debate": True,
    "debate_max_rounds": 3,
    # which providers are enabled at all in the council
    "council_enable_gemini": True,
    "council_enable_openai": True,
    "council_enable_claude_tiebreaker": True,
    # Per-run reviews are NOISY (they fire once per finished run and create
    # constant queue churn). Default OFF; the strategic review (below) does
    # the heavy lifting in batches.
    "council_per_run_enabled": False,
    # Strategic review: every N finished runs, take a step back and review
    # the whole BATCH together. N=0 means 'use GPU count' (the agent
    # typically runs one experiment per GPU in parallel, so this reviews
    # one complete wave of parallel runs at a time).
    "strategic_review_enabled": True,
    "strategic_review_batch_n": 0,
}


def _settings() -> dict:
    """Merge defaults with whatever's stored in the onboarding Setting row."""
    out = dict(DEFAULTS)
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(Setting.key == "onboarding").first()
        if row and isinstance(row.value, dict):
            for k in DEFAULTS:
                if k in row.value and row.value[k] not in ("", None):
                    out[k] = row.value[k]
    finally:
        db.close()
    return out


# ── reviewer availability ────────────────────────────────────────────────
def _available_reviewers(cfg: dict) -> list[str]:
    rs = []
    if cfg.get("council_enable_gemini", True) and os.environ.get("GEMINI_API_KEY"):
        rs.append("gemini")
    if cfg.get("council_enable_openai", True) and os.environ.get("OPENAI_API_KEY"):
        rs.append("openai")
    return rs


def _claude_available(cfg: dict) -> bool:
    return bool(cfg.get("council_enable_claude_tiebreaker", True)
                and os.environ.get("ANTHROPIC_API_KEY"))


def is_enabled() -> bool:
    return bool(_available_reviewers(_settings()))


_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT: set[str] = set()
_FILE_LOCK = threading.Lock()           # serialize ideas.md edits

# Global semaphore: never more than this many council reviews in flight at
# once across the whole process. A runaway agent that finishes 300 runs/min
# (it happened once and cost the user $300) cannot DDoS the model endpoints
# or run up an enormous bill — surplus reviews are skipped, not queued.
_GLOBAL_MAX_CONCURRENT = 2
_GLOBAL_SEMAPHORE = threading.Semaphore(_GLOBAL_MAX_CONCURRENT)

# Per-idea cooldown — at most one council review per idea_id every
# COOLDOWN_SEC seconds, even if the agent re-runs the same idea on a new
# GPU. Stops "300 finishes in 5 minutes" from triggering 300 reviews.
_COOLDOWN_SEC = 15 * 60
_LAST_REVIEW_AT: dict[str, float] = {}  # idea_id -> wall-clock seconds
_COOLDOWN_LOCK = threading.Lock()


# ── the prompt ────────────────────────────────────────────────────────────
# The `learning` field is the canonical text that gets appended to lessons.md
# and becomes the project's running scientific memory. It MUST be a piece of
# research content (hypothesis + result + insight), NOT advice about the
# tooling. See `classify_lesson_quality` below for the machine-enforced
# contract.
_LEARNING_CONTRACT = """\
The `learning` field is the only thing that ends up in lessons.md — the
project's permanent scientific notebook. It must be research content, not
process advice. Every `learning` MUST follow this exact bullet structure
(use these literal markers so the parser can find them):

  HYPOTHESIS: <one sentence — the falsifiable claim this run/batch tested,
    e.g. "diff-init from finetuned-AR ckpt outperforms random-init by ≥1pt
    EM on GSM8K">.
  RESULT: <one sentence with the NUMBER and at least one run_id, e.g.
    "diff_n3_seed7 reached 0.0432 EM vs baseline 0.0508 (-0.0076) on
    gsm8k_val; ar_baseline_v2 hit 0.0561 same eval">.
  WHY: <one sentence — the mechanistic / data-driven explanation for the
    result, NOT a restatement of the result>.
  GENERALIZABLE INSIGHT: <one sentence — the method-choice takeaway that
    transfers BEYOND this run (e.g. "in-distribution diff-init is dominated
    by AR-finetuned-init at scales <1B params"). NOT "we should log more
    metrics" or "we should sweep lr next time". Insight, not chore.
  NEXT EXPERIMENT: <one sentence — the single highest-EV next run, with
    concrete HP values where they matter>.

REJECT THESE PATTERNS — do not emit a `learning` that contains any of:
  * Tool / mechanics tips ("always log __METRIC__", "use trusted_eval",
    "remember to seed", "make sure to set bs=…"). Tooling lives in
    program.md, NOT in lessons.md.
  * Vague platitudes ("we should explore more diverse ideas", "more
    runs needed", "the data is inconclusive without saying why").
  * Restatements of the agent's behaviour ("agent launched 5 runs",
    "the council recommended X"). Lessons.md is about what the DATA
    shows, not what the agent did.
  * Process nags ("for the Nth consecutive batch the agent ignored…").
    Those belong in Events, not in the scientific memory.
  * A `learning` with no run_id and no HYPOTHESIS marker — these are
    machine-rejected by the lessons.md sanity check and the entry will
    be DROPPED, wasting the review."""


SYSTEM = """You are the senior scientific advisor on an autonomous ML research
project. An autonomous agent just finished one experiment. Your job is to
decide what the agent should do next so the next experiments are HIGH-VALUE.

GOAL: Maximize EV-per-GPU-hour. Steady measurable progress beats one
ambitious bet that crashes. Stabilizing a working method (lr search,
regularization tuning, careful scaling) IS valuable research — do not
penalise it.

PRINCIPLES:
- A careful HP sweep IS the right next step when the data calls for it.
  Specifically recommend it when:
    * recent runs are CRASHING in correlated ways (e.g. many diverge at
      the same lr, or all crashes share a config token),
    * variance across nearby runs is so high the headline number is not
      yet trustworthy,
    * the agent is in an obviously miscalibrated regime (gradients
      exploding, NaN, divergence in first 100 steps).
  Be explicit about which HPs to sweep and at what values.
- A seed re-run is fine when 1-2 noisy data points are blocking a real
  conclusion. Don't recommend 5 seed re-runs; 2 is usually enough.
- New ideas should still each change ONE substantive thing — architecture,
  objective, data regime, training procedure, evaluation — with a one-line
  hypothesis WHY it might help. Include concrete HP values where they
  matter (lr=1e-4, bf16->fp32) so the agent doesn't fill in bad defaults.
- Penalise repetition: if the prior-runs list already explored a direction
  exhaustively, DROP that direction (veto pending entries on the same
  theme).
- PIVOT WHEN STUCK: If the project has been stagnant for >100 runs (no
  new frontier improvement) and the current best is well short of the
  project's purpose, say so explicitly in `learning` and propose a
  fundamentally different approach in `new_ideas` — or recommend
  revisiting the project's hypothesis itself.

""" + _LEARNING_CONTRACT + """

You return JSON ONLY, no prose around it, no markdown fence, matching:
{
  "verdict": "kept" | "discarded" | "crashed" | "inconclusive",
  "learning": "<follow the HYPOTHESIS / RESULT / WHY / GENERALIZABLE INSIGHT
    / NEXT EXPERIMENT contract above, verbatim markers, with at least one
    run_id cited in RESULT>",
  "rerank_pending": ["<idea_id_best_next>", "<idea_id_2nd>", ...],
  "new_ideas": [
    {"idea_id": "<snake_case_id>", "what": "<one line, concrete — include
       specific HP values if relevant, e.g. 'lr=1e-4, fp32, bs=64'>",
     "why": "<one line: a falsifiable hypothesis>"}
  ],
  "veto": ["<idea_id_to_drop>", ...]
}

- rerank_pending: ONLY existing pending idea_ids from the input, in the
  order you want them tried (best first). Omit ones you'd skip — put them
  in veto.
- new_ideas: 0-3 entries. Quality > quantity. snake_case ids only. Concrete
  HP values where they matter; vague rewrappings of existing ideas are NOT
  acceptable.
- learning: research content per the contract above. Cite run_ids from the
  context. If you cannot fill HYPOTHESIS+RESULT+WHY truthfully from the
  data you were shown, leave `learning` empty — better to drop the entry
  than to fill it with platitudes."""


DEBATE_SYSTEM = SYSTEM + """

DEBATE MODE: another expert reviewer also looked at this run. Their position
will be appended to the user message. Read it carefully. If you AGREE with
their points, adopt their framing (your output should match theirs on
verdict + top-3 rerank + veto set). If you DISAGREE, restate your position
but ALSO address their argument specifically in the "learning" field — say
why their take is wrong or incomplete. The goal is to converge on the
strongest call, not to be stubborn. Keep returning the same JSON schema.

ADVERSARIAL MODE (conditional — only when the user message contains the
header "PRIOR STRATEGIC VERDICT: stagnant" or "PRIOR STRATEGIC VERDICT:
regressing"):
  The project is stuck. Agreement is the failure mode here — both of you
  have been agreeing for batches and nothing has moved. If you are the
  SECOND reviewer (the one with the other reviewer's position appended),
  you MUST role-play as adversarial: argue that the OTHER reviewer's
  continued recommendation IS THE PROBLEM, propose a strictly different
  action — e.g. close the open BLOCKER, pivot the project's hypothesis,
  switch to an ORTHOGONAL direction. At least ONE of the two final
  positions must propose an ORTHOGONAL direction (set verdict and
  new_ideas + directives_upsert accordingly). The tiebreaker (claude)
  reads both and picks. Do NOT shy away from saying the prior advice
  was wrong — that's the entire point of adversarial mode."""


TIEBREAKER_SYSTEM = """You are the tiebreaker on a senior advisory panel for
an autonomous ML research project. Two expert reviewers disagree after 3
rounds of debate. Read both of their final positions and the run context,
then make the final call. Return the same JSON schema as the reviewers
(verdict, learning, rerank_pending, new_ideas, veto).

""" + _LEARNING_CONTRACT + """

In "learning", follow the same HYPOTHESIS / RESULT / WHY / GENERALIZABLE
INSIGHT / NEXT EXPERIMENT contract. Cite at least one run_id in RESULT.
You may add a parenthetical at the end of GENERALIZABLE INSIGHT noting
which reviewer you sided with and why — but only as a parenthetical.
The lessons.md entry must still be research content, not a procedural
note about the debate. Be decisive — no hedging."""


STRATEGIC_SYSTEM = """You are the senior scientific advisor on an autonomous
ML research project. The agent has just finished a BATCH of N parallel
experiments (one per GPU). Your job is not to micro-review each one but
to step back and decide what the AGENT SHOULD DO NEXT — a strategic call
on the whole project trajectory, based on this batch + all prior history.

YOU ARE STATEFUL. The orchestrator injects three deterministic facts
into the user message under the heading "PRIOR STRATEGIC STATE":
  - previous_top_directive_id: the directive id you flagged as top at
    your last review (or "" if there was no prior review)
  - previous_directive_implemented: YES / NO / IN_PROGRESS — was that
    top directive implemented by the agent between the prior review and
    now? (computed deterministically — do not contradict it)
  - consecutive_unimplemented_count: how many reviews in a row that same
    top directive id has been carried over with implemented==NO. This
    is the load-bearing escalation counter.

MANDATORY ESCALATION RULE (do not soften):
  If previous_directive_implemented=="NO" for 3 consecutive reviews on
  the same previous_top_directive_id, you MUST set
  verdict="ESCALATION_HALT", append a HALT directive of priority 9999 in
  directives_upsert, AND the tiebreaker (claude) MUST be invoked even
  on agreement. The agent has demonstrated it ignores prose advice — the
  only remaining channel is a hard HALT that bypasses the agent and
  surfaces to the human PI.

ORTHOGONAL QUOTA (RESEARCH_IMPROVEMENT_PLAN #5):
  Every entry in directives_upsert MUST include an idea_class in
  {INCREMENTAL, ORTHOGONAL, REPRODUCE, INFRA, ABLATION}.
  - ORTHOGONAL means a fundamentally different model class, training
    objective, or data regime — NOT a re-shuffle of the current pool.
  - REPRODUCE means: pull a relevant recent arXiv paper and propose to
    reproduce its core claim on this project's data.
  - The ratio of INCREMENTAL to ORTHOGONAL across all open directives
    must not exceed 3:1.
  - If verdict has been "stagnant" for >= 2*GPU_COUNT consecutive
    reviews, directives_upsert MUST include at least one
    idea_class="ORTHOGONAL" or "REPRODUCE" entry. A validator will
    REJECT your output if it violates this — your call will not be
    persisted.

GOAL: Maximize EV-per-GPU-hour. Boring is fine if it consolidates a
result. Stabilizing a working method (lr search, regularization,
careful scaling) is real research. Spicy ideas that crash are not.

WHAT TO LOOK FOR IN THIS BATCH:
- Is the project's frontier moving, or has the best metric been flat for
  a long time? Look at the all-prior-runs list.
- Are crashes clustering on the same config token (lr=5e-4, bf16,
  ensemble_n=k..)? If yes, the next move is to FIX that token, not to
  abandon the direction.
- Is the agent following the council's prior recommendations, or doing
  its own thing? If the council's last N proposals all crashed, OWN that
  — admit the council steered into a dead end and recalibrate.
- Are there ideas in the queue that are already strictly subsumed by
  finished runs? Veto them.

YOU CAN AND SHOULD RECOMMEND:
- A focused HP sweep (e.g. lr in {1e-5, 3e-5, 1e-4, 3e-4}, fp32) when
  crashes are clearly HP-driven.
- Stopping a dead direction. If after 100+ runs in some direction nothing
  has moved, say so.
- A pivot to a different research direction or a re-examination of the
  project's hypothesis if the data strongly suggests the hypothesis is
  not supported.

""" + _LEARNING_CONTRACT + """

Strategic-review-specific note on `learning`:
- The HYPOTHESIS line for a strategic review is the BATCH-level hypothesis
  this wave of runs was testing collectively (e.g. "5-way diffusion-LM
  ensembles outperform the AR baseline on GSM8K"). It is NOT a project
  status report.
- The RESULT line MUST cite at least 2 specific run_ids from the batch
  AND the project frontier number with its run_id (e.g.
  "diff_n5_a, diff_n5_b, diff_n5_c all landed 0.041-0.044 EM; project
  frontier still ar_baseline_v2 at 0.0561 EM").
- The GENERALIZABLE INSIGHT line is the load-bearing one: what does the
  whole batch teach us about METHOD CHOICE for this class of problem?
  Anything that reads like "the agent should X" or "we need better
  infrastructure for Y" is process advice and DOES NOT belong here —
  put process advice in the pivot field or as a Summary feed event.

JSON ONLY, no markdown, schema:
{
  "previous_top_directive_id": "<echo back the id you were shown, or ''>",
  "previous_directive_implemented": "YES" | "NO" | "IN_PROGRESS",
  "consecutive_unimplemented_count": <int — echo back the deterministic
    value you were shown; do not recompute>,
  "verdict": "progress" | "stagnant" | "regressing" | "ESCALATION_HALT",
  "learning": "<follow the HYPOTHESIS / RESULT / WHY / GENERALIZABLE
    INSIGHT / NEXT EXPERIMENT contract verbatim. Cite run_ids in RESULT.
    If you cannot fill HYPOTHESIS+RESULT+WHY truthfully from the batch
    data, leave this empty; the lessons.md sanity check will drop a
    learning with no run_id or no HYPOTHESIS marker.>",
  "rerank_pending": ["<idea_id_best_next>", ...],
  "new_ideas": [
    {"idea_id": "<snake_case>", "what": "<concrete with HP values>",
     "why": "<falsifiable hypothesis>"}
  ],
  "veto": ["<idea_id_to_drop>", ...],
  "pivot": {"recommend": true|false, "to_what": "<one-line direction>"},
  "directives_upsert": [
    {"id": "<d-XXXX or omit for auto>",
     "type": "BLOCKER_INFRA|BLOCKER_EVAL|SCIENCE|HALT|SEED_REPLICATE",
     "priority": <int>,
     "what": "<one-line description>",
     "acceptance": "<how we know it's done>",
     "idea_class": "INCREMENTAL|ORTHOGONAL|REPRODUCE|INFRA|ABLATION",
     "why": "<optional hypothesis>",
     "blocked_by": ["<other_directive_id>", ...]}
  ],
  "directives_close": ["<directive_id>", ...]
}

- rerank_pending: ONLY existing pending idea_ids, best first.
- new_ideas: 0-5 entries. Concrete HP values where relevant.
- pivot: only set recommend=true if the data strongly supports it (>100
  flat runs, or this project's hypothesis is materially contradicted).
- directives_upsert: 0-5 entries. Drives directives.jsonl (the
  authoritative command queue). New directives without an id get auto-
  generated ids. To update an existing directive (priority change, status
  bump) include its id.
- directives_close: 0-5 ids the council vetoes / marks complete."""


# ── context bundle ────────────────────────────────────────────────────────
def _frontier_ids(runs) -> set[str]:
    out: set[str] = set()
    best = None
    for r in sorted(runs, key=lambda r: r.created_at or ""):
        if r.headline_metric is None:
            continue
        if best is None or r.headline_metric < best:
            best = r.headline_metric
            out.add(r.id)
    return out


def _compact_one_line(r, frontier: set[str], maximize: bool) -> str:
    """One-line digest of a run that fits ~80-120 chars: id, status, metric,
    abbreviated 'what'. Frontier-movers get a star. ~30-40 tokens each."""
    star = " ★" if r.id in frontier else ""
    m = "—" if r.headline_metric is None else f"{r.headline_metric:.4f}"
    cfg = r.config if isinstance(r.config, dict) else {}
    what = (cfg.get("what") or "").strip()
    if not what:
        what = (r.run_name or r.id)
    what = " ".join(what.split())[:70]            # collapse whitespace, clip
    return f"{r.status[:9]:<9} {m:<8} {r.id[:34]:<34}{star}  {what}"


def _aggregate_stats(runs, proj) -> dict:
    """Cheap aggregate stats: counts by status, frontier progression, and a
    naive crash-pattern detector that surfaces words appearing in many
    crashed-run names (typical signal: 'lr=5e-4' in N/M crashes)."""
    by_status: dict[str, int] = {}
    crashed_tokens: dict[str, int] = {}
    metrics_seen = []
    for r in runs:
        by_status[r.status] = by_status.get(r.status, 0) + 1
        if r.status == "crashed":
            name = (r.run_name or r.id).lower()
            for tok in re.split(r"[_\s=]+", name):
                if 2 < len(tok) < 28 and not tok.isdigit():
                    crashed_tokens[tok] = crashed_tokens.get(tok, 0) + 1
        if r.headline_metric is not None and r.status in ("kept", "success"):
            metrics_seen.append((r.created_at or "", r.headline_metric, r.id,
                                  r.run_name))
    # Best 5 crash-pattern tokens, only if they appear in ≥4 crashes
    crash_patterns = sorted(
        [(k, v) for k, v in crashed_tokens.items() if v >= 4],
        key=lambda kv: -kv[1])[:5]
    # Frontier progression (only kept improvements, in chronological order)
    metrics_seen.sort(key=lambda x: x[0])
    maximize = proj.metric_direction == "maximize"
    frontier_progression = []
    best = None
    for _ts, m, rid, name in metrics_seen:
        if best is None or (m > best if maximize else m < best):
            best = m
            frontier_progression.append({"run_id": rid, "name": name,
                                          "metric": m})
    return {
        "by_status": by_status,
        "frontier_progression": frontier_progression[-20:],   # last 20 best
        "crash_patterns": [{"token": k, "n_crashed_runs": v}
                            for k, v in crash_patterns],
    }


def _lessons_path() -> Path | None:
    """workspace/<repo_name>/lessons.md — the council's running notebook."""
    name = _onboarding_repo_name()
    if not name:
        return None
    p = DATA_DIR / "workspace" / name / "lessons.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _read_lessons(max_chars: int = 6000) -> str:
    p = _lessons_path()
    if not p or not p.exists():
        return ""
    try:
        text = p.read_text(errors="ignore")
    except OSError:
        return ""
    # Trim to last max_chars so the file can't blow the budget
    if len(text) > max_chars:
        text = "… (older lessons trimmed)\n\n" + text[-max_chars:]
    return text


# ── lessons.md quality gate ───────────────────────────────────────────────
# Banned phrases that signal tool-mechanics / process-nag / vague-platitude
# content. A `learning` containing any of these is downgraded to "bad" by
# the classifier and dropped from lessons.md (it can still appear in the
# Events feed). Keep this list short and surgical — over-blocking will make
# the council give up and emit nothing.
_LESSON_BAD_PHRASES = (
    "always log",
    "remember to log",
    "make sure to log",
    "we should log",
    "log more metrics",
    "log all metrics",
    "use __metric__",
    "set arui.summary",
    "remember to seed",
    "remember to set",
    "for the nth consecutive",                  # process nag pattern
    "for the n-th consecutive",
    "the agent ignored",
    "the agent has not implemented",
    "the agent should remember",
    "more runs needed",
    "more experiments needed",
    "needs more investigation",
    "inconclusive without further",
    "tbd",
    "tba",
    "n/a",
)

# Patterns that look like a run_id reference. Real run_ids in this codebase
# are slugged like `diff_n3_seed7`, `ar_baseline_v2`, `run-2a4f81e0`,
# `gsm8k_lr1e-4_b`, etc. We accept any token containing an underscore or
# hyphen with at least one digit, length ≥ 4, OR an explicit `run-XXXX`
# style id. We deliberately err on the permissive side — the goal is to
# block lessons that cite NO run_ids at all, not to police naming.
_RUN_ID_RE = re.compile(
    r"\b("
    r"run[-_][a-z0-9]{4,}"                       # run-2a4f81e0 / run_abc123
    r"|[a-z][a-z0-9]*(?:[_-][a-z0-9]+){1,}[a-z0-9]" # diff_n3_seed7 etc
    r")\b",
    re.IGNORECASE,
)

# Hypothesis marker (case-insensitive). The contract requires this literal
# token to appear so the parser can reliably extract the structured fields.
_HYPOTHESIS_RE = re.compile(r"\bHYPOTHESIS\s*[:\-]", re.IGNORECASE)
_RESULT_RE = re.compile(r"\bRESULT\s*[:\-]", re.IGNORECASE)
_INSIGHT_RE = re.compile(
    r"\bGENERAL[IZ]+[A-Z]*\s+INSIGHT\s*[:\-]", re.IGNORECASE)


def classify_lesson_quality(learning: str) -> dict:
    """Sanity-check a `learning` string before appending it to lessons.md.

    Returns a dict with:
      - ok (bool): True if the lesson is research content per the contract.
      - reason (str): empty if ok, else a short machine-readable code such
        as "no_hypothesis_marker", "no_run_id", "tool_mechanics:always log",
        "too_short", "empty". Used by the caller to log a warning and skip
        the append.
      - has_hypothesis, has_result, has_insight, has_run_id (bool): the
        individual checks, exposed so tests + the UI can show partial
        diagnostics if needed.
      - bad_phrase (str | None): the first banned phrase that matched.

    A lesson is `ok` iff:
      1. It is ≥ 40 chars after stripping (we expect a 5-bullet structure).
      2. It contains the literal `HYPOTHESIS:` marker.
      3. It cites at least one run_id-shaped token in the RESULT area
         (or anywhere — we accept either; the agent's run-naming varies).
      4. It does NOT contain any banned tool-mechanics / process-nag phrase.

    The classifier is intentionally cheap and regex-only so it runs on
    every reviewer turn without LLM cost."""
    if not learning or not learning.strip():
        return {
            "ok": False, "reason": "empty",
            "has_hypothesis": False, "has_result": False,
            "has_insight": False, "has_run_id": False,
            "bad_phrase": None,
        }
    text = learning.strip()
    if len(text) < 40:
        return {
            "ok": False, "reason": "too_short",
            "has_hypothesis": False, "has_result": False,
            "has_insight": False, "has_run_id": False,
            "bad_phrase": None,
        }
    low = text.lower()
    bad = next((p for p in _LESSON_BAD_PHRASES if p in low), None)
    has_hyp = bool(_HYPOTHESIS_RE.search(text))
    has_res = bool(_RESULT_RE.search(text))
    has_ins = bool(_INSIGHT_RE.search(text))
    has_rid = bool(_RUN_ID_RE.search(text))
    if bad is not None:
        return {
            "ok": False, "reason": f"tool_mechanics:{bad}",
            "has_hypothesis": has_hyp, "has_result": has_res,
            "has_insight": has_ins, "has_run_id": has_rid,
            "bad_phrase": bad,
        }
    if not has_hyp:
        return {
            "ok": False, "reason": "no_hypothesis_marker",
            "has_hypothesis": False, "has_result": has_res,
            "has_insight": has_ins, "has_run_id": has_rid,
            "bad_phrase": None,
        }
    if not has_rid:
        return {
            "ok": False, "reason": "no_run_id",
            "has_hypothesis": has_hyp, "has_result": has_res,
            "has_insight": has_ins, "has_run_id": False,
            "bad_phrase": None,
        }
    return {
        "ok": True, "reason": "",
        "has_hypothesis": has_hyp, "has_result": has_res,
        "has_insight": has_ins, "has_run_id": has_rid,
        "bad_phrase": None,
    }


def _append_lesson(reviewer: str, run_name: str, learning: str) -> None:
    """Append the council's takeaway from this run to lessons.md, dedup'd
    against the previous N entries so identical / near-identical lessons
    don't clutter the file.

    Now also gated by `classify_lesson_quality`: lessons that fail the
    sanity check (no HYPOTHESIS marker, no run_id, banned tool-mechanics
    phrase, etc.) are DROPPED with a warning to stderr instead of being
    written. This is what stops the file from filling up with
    "always log metrics" platitudes."""
    p = _lessons_path()
    if not p or not learning or not learning.strip():
        return
    ll = learning.strip()
    if len(ll) < 12:
        return
    # Quality gate — reject vague / tool-mechanics / no-run-id lessons.
    q = classify_lesson_quality(ll)
    if not q["ok"]:
        print(
            f"[council] dropping low-quality lesson from {reviewer} "
            f"on {run_name}: reason={q['reason']} "
            f"(has_hyp={q['has_hypothesis']} has_rid={q['has_run_id']}). "
            f"First 160 chars: {ll[:160]!r}",
            flush=True,
        )
        return
    cur = _read_lessons(max_chars=20000)
    # naive fuzzy dedup: skip if a recent line shares ≥80% of words
    new_words = set(re.findall(r"\w+", ll.lower()))
    if new_words:
        for line in cur.splitlines()[-30:]:
            old_words = set(re.findall(r"\w+", line.lower()))
            if old_words:
                overlap = len(new_words & old_words) / max(len(new_words), 1)
                if overlap > 0.8:
                    return
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M")
    line = f"- [{ts} · {reviewer} on {run_name}] {ll}\n"
    try:
        with open(p, "a") as f:
            f.write(line)
    except OSError as e:
        print(f"[council] could not append lesson: {e}", flush=True)


def scan_lessons_file(path: Path | None = None) -> dict:
    """Audit an existing lessons.md file. For each entry, run the quality
    classifier and report counts + a list of suspect entries. Intended for
    use from the maintenance / Lessons UI / cron, not as part of the hot
    write path. Returns:
      {
        "path": "<str>",
        "total": int,
        "ok": int,
        "bad": int,
        "bad_reasons": {"no_run_id": 3, ...},
        "samples_bad": [{"line": "...", "reason": "..."}, ...],   # up to 5
      }
    If the file doesn't exist or the workspace isn't set, returns an empty
    report — the caller can decide whether to surface it."""
    p = path or _lessons_path()
    if not p or not p.exists():
        return {"path": str(p) if p else "", "total": 0, "ok": 0, "bad": 0,
                "bad_reasons": {}, "samples_bad": []}
    try:
        text = p.read_text(errors="ignore")
    except OSError:
        return {"path": str(p), "total": 0, "ok": 0, "bad": 0,
                "bad_reasons": {}, "samples_bad": []}
    # Each lesson is a single line beginning with "- [<ts> · <reviewer> on
    # <run>] <body>". Strip the prefix before scoring.
    line_re = re.compile(r"^-\s*\[[^\]]+\]\s*(.+)$")
    ok = bad = 0
    bad_reasons: dict[str, int] = {}
    samples: list[dict] = []
    for raw in text.splitlines():
        m = line_re.match(raw.strip())
        if not m:
            continue
        body = m.group(1).strip()
        q = classify_lesson_quality(body)
        if q["ok"]:
            ok += 1
        else:
            bad += 1
            bad_reasons[q["reason"]] = bad_reasons.get(q["reason"], 0) + 1
            if len(samples) < 5:
                samples.append({"line": raw, "reason": q["reason"]})
    return {
        "path": str(p),
        "total": ok + bad,
        "ok": ok,
        "bad": bad,
        "bad_reasons": bad_reasons,
        "samples_bad": samples,
    }


def _build_context(run_id: str) -> dict | None:
    db = SessionLocal()
    try:
        proj = db.query(Project).first()
        run = db.query(Run).filter(Run.id == run_id).first()
        if not (proj and run):
            return None
        every_run = db.query(Run).all()
        frontier = _frontier_ids(every_run)
        ideas = db.query(Idea).filter(Idea.id.like("deck-%")).all()
        maximize = proj.metric_direction == "maximize"

        # Compress every prior run into a one-liner. Sort: frontier-movers
        # first, then crashed, then everything else. This is the central
        # change — the council now sees the WHOLE project history, not just
        # the last 8 runs.
        others = [r for r in every_run if r.id != run.id]

        def _key(r):
            on_front = r.id in frontier
            is_crash = (r.status == "crashed")
            # frontier first (0), then crash (1), then others (2);
            # within each bucket newest first
            return (0 if on_front else (1 if is_crash else 2),
                    -1 * (1 if r.created_at else 0),
                    r.created_at or "")
        others.sort(key=_key)
        run_lines = [_compact_one_line(r, frontier, maximize) for r in others]
        run_lines_text = "\n".join(run_lines)[:24000]   # cap ~6k tokens

        stats = _aggregate_stats(every_run, proj)

        ctx = {
            "project": {
                "name": proj.name,
                "purpose": proj.purpose,
                "metric": proj.validation_metric,
                "direction": proj.metric_direction,
                "baseline_metric": getattr(proj, "baseline_metric", None),
            },
            "this_run": {
                "id": run.id,
                "name": run.run_name,
                "status": run.status,
                "headline_metric": run.headline_metric,
                "baseline_delta": run.baseline_delta,
                "config": run.config if isinstance(run.config, dict) else {},
            },
            "aggregate": stats,
            "all_prior_runs_count": len(others),
            "all_prior_runs_oneliners": run_lines_text,
            "pending_ideas": [
                {"idea_id": i.idea_id, "what": i.description}
                for i in ideas
            ][:30],
            "pending_total_count": len(ideas),
            "lessons_so_far": _read_lessons(max_chars=6000),
        }
        return ctx
    finally:
        db.close()


# ── model adapters (stdlib only) ──────────────────────────────────────────
# Reasoning models (gpt-5 high, o3-pro, gemini-2.5-pro) can take 30-180s.
_TIMEOUT = 240
_RETRY_DELAYS = (4, 10, 25)             # backoff for 429s


def _post_json(url: str, body: dict, headers: dict) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", errors="ignore"))


def _post_json_retry(url: str, body: dict, headers: dict) -> dict:
    """POST with retry on 429 (rate limit) and 5xx (transient). Other HTTP
    errors propagate so the caller can log and skip."""
    last_exc: Exception | None = None
    for i, d in enumerate((0,) + _RETRY_DELAYS):
        if d:
            time.sleep(d)
        try:
            return _post_json(url, body, headers)
        except urllib.error.HTTPError as e:
            last_exc = e
            if e.code == 429 or 500 <= e.code < 600:
                continue
            raise
        except Exception as e:
            last_exc = e
            continue
    if last_exc:
        raise last_exc
    raise RuntimeError("unreachable")


def _call_gemini(system: str, user: str, cfg: dict) -> str:
    key = os.environ["GEMINI_API_KEY"]
    model = cfg.get("council_gemini_model") or DEFAULTS["council_gemini_model"]
    url = ("https://generativelanguage.googleapis.com/v1beta/"
           f"models/{model}:generateContent?key={key}")
    body = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"responseMimeType": "application/json",
                             "temperature": 0.7},
    }
    data = _post_json_retry(url, body, {})
    return data["candidates"][0]["content"]["parts"][0]["text"]


def _call_openai(system: str, user: str, cfg: dict) -> str:
    key = os.environ["OPENAI_API_KEY"]
    model = cfg.get("council_openai_model") or DEFAULTS["council_openai_model"]
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "response_format": {"type": "json_object"},
    }
    effort = cfg.get("council_openai_effort") or DEFAULTS["council_openai_effort"]
    # gpt-5 family and o-series accept reasoning_effort
    if "gpt-5" in model or model.startswith("o"):
        body["reasoning_effort"] = effort
    data = _post_json_retry("https://api.openai.com/v1/chat/completions", body,
                            {"Authorization": f"Bearer {key}"})
    return data["choices"][0]["message"]["content"]


def _call_claude(system: str, user: str, cfg: dict) -> str:
    key = os.environ["ANTHROPIC_API_KEY"]
    model = cfg.get("council_claude_model") or DEFAULTS["council_claude_model"]
    body = {
        "model": model,
        "max_tokens": 2000,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    data = _post_json_retry("https://api.anthropic.com/v1/messages", body,
                            {"x-api-key": key,
                             "anthropic-version": "2023-06-01"})
    return data["content"][0]["text"]


_CALLERS = {"gemini": _call_gemini, "openai": _call_openai,
            "claude": _call_claude}


# ── parsing ──────────────────────────────────────────────────────────────
def _strip_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = s.strip("`").lstrip()
        if s.lower().startswith("json"):
            s = s[4:].lstrip()
        if s.endswith("```"):
            s = s[:-3].rstrip()
    return s.strip()


def _safe_parse(text: str) -> dict | None:
    text = _strip_fences(text)
    try:
        out = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return None
        try:
            out = json.loads(m.group(0))
        except Exception:
            return None
    if not isinstance(out, dict):
        return None
    out.setdefault("verdict", "inconclusive")
    out.setdefault("learning", "")
    out.setdefault("rerank_pending", [])
    out.setdefault("new_ideas", [])
    out.setdefault("veto", [])
    return out


# ── one reviewer turn ────────────────────────────────────────────────────
def _emit_token_failure_event(reviewer: str, kind: str, detail: str) -> None:
    """Persist a failure as an Event so the user SEES it in the Summary
    feed + email digest instead of finding out by reading the backend log.
    Deduplicates by (reviewer, kind) within a single hour so a runaway 401
    doesn't drown the feed. Best-effort: never raises into the caller."""
    try:
        import os as _os
        from .db import SessionLocal
        from .models import Event
        import datetime as _dt
        db = SessionLocal()
        try:
            cutoff = (_dt.datetime.now(_dt.timezone.utc)
                      - _dt.timedelta(hours=1)).isoformat()
            recent = (db.query(Event)
                      .filter(Event.type == f"reviewer_{kind}")
                      .filter(Event.actor == reviewer)
                      .filter(Event.created_at > cutoff).first())
            if recent:
                return                                # already announced
            ev = Event(id=f"ev-{_os.urandom(4).hex()}",
                       type=f"reviewer_{kind}",
                       severity="warning",
                       actor=reviewer,
                       message=(f"{reviewer} reviewer is failing "
                                f"({kind}): {detail[:160]}. "
                                "Council will run with the other reviewers "
                                "until you fix the token / quota."),
                       created_at=_dt.datetime.now(
                           _dt.timezone.utc).isoformat())
            db.add(ev)
            db.commit()
            try:
                from .bus import bus
                bus.publish("events", "event", ev.dict())
            except Exception:
                pass
        finally:
            db.close()
    except Exception as e:                              # noqa: BLE001
        print(f"[council] _emit_token_failure_event failed: {e}", flush=True)


def _call_reviewer(reviewer: str, system: str, user: str, cfg: dict
                   ) -> dict | None:
    # PAUSE / HALT GATE — single choke point for ALL external council
    # API calls. Every other council entry point (deliberate,
    # strategic_review, _bless_worker) eventually funnels through here
    # so this one early-return covers the whole module. If the user has
    # paused research (or the system has hard-halted), we must NOT fire
    # a Gemini/GPT/Claude API call — that's the entire point of pause.
    try:
        from . import notify as _notify
        if _notify.research_paused():
            print(f"[council] {reviewer} call suppressed — "
                  "research_paused", flush=True)
            return None
        halted, _reason = _notify.research_halted()
        if halted:
            print(f"[council] {reviewer} call suppressed — "
                  "research_halted", flush=True)
            return None
    except Exception:
        # Best-effort: don't break the council if the gate helper
        # malfunctions.
        pass
    try:
        text = _CALLERS[reviewer](system, user, cfg)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="ignore")[:300]
        except Exception:
            pass
        print(f"[council] {reviewer} HTTP {e.code}: {body}", flush=True)
        # Surface auth + rate-limit errors as Events so the user sees
        # them in the Summary feed + digest emails.
        if e.code in (401, 403):
            _emit_token_failure_event(reviewer, "auth_failed",
                                       f"HTTP {e.code} {body}")
        elif e.code == 429:
            _emit_token_failure_event(reviewer, "rate_limited",
                                       f"HTTP 429 {body}")
        return None
    except Exception as e:                              # noqa: BLE001
        print(f"[council] {reviewer} call failed: {e}", flush=True)
        return None
    out = _safe_parse(text)
    if not out:
        print(f"[council] {reviewer} returned non-JSON; first 200: "
              f"{text[:200]!r}", flush=True)
        return None
    out["reviewer"] = reviewer
    out["reviewed_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    return out


def _agreement(a: dict, b: dict) -> bool:
    """Two reviews agree if verdict matches AND top-3 of rerank match AS A
    SET AND veto sets are equal."""
    if (a.get("verdict") or "") != (b.get("verdict") or ""):
        return False
    aa = set((a.get("rerank_pending") or [])[:3])
    bb = set((b.get("rerank_pending") or [])[:3])
    if aa != bb:
        return False
    return set(a.get("veto") or []) == set(b.get("veto") or [])


# ── the debate orchestrator ──────────────────────────────────────────────
def _ctx_text(ctx: dict) -> str:
    """Build the user message for the council. We INLINE the all-runs
    digest + lessons.md as natural-language blocks (not JSON-quoted) so the
    model reads them as the body of context, not as a stringified blob. The
    rest of the context (project, this_run, aggregate, pending, ...) is
    serialised as JSON for structure."""
    total = ctx.get("pending_total_count", 0)
    n_runs = ctx.get("all_prior_runs_count", 0)
    note = ""
    if total > 40:
        note += (f"\n\nNOTE: queue has {total} pending ideas (you only see "
                 f"top 30). Focus on RERANK / VETO. Only propose new_ideas "
                 f"if they would CLEARLY replace existing ones.")
    # Inline blocks
    runs_block = ctx.pop("all_prior_runs_oneliners", "") or "(none)"
    lessons_block = ctx.pop("lessons_so_far", "").strip() or \
        "(none yet — this is the first review)"
    return (
        "You are reviewing the most recent run on an autonomous ML research "
        "project. Use all the context below — especially the lessons "
        "already learned and the history of prior runs — to avoid "
        "recommending anything that's already been tried or dead-ended."
        + note + "\n\n"
        "=== LESSONS LEARNED SO FAR (from your previous reviews) ===\n"
        + lessons_block + "\n\n"
        f"=== ALL PRIOR RUNS ({n_runs} total — frontier-movers ★ first, then "
        "crashed, then everything else) ===\n"
        "status    metric   run_id                              what\n"
        "" + runs_block + "\n\n"
        "=== STRUCTURED CONTEXT (project / this run / aggregate / pending) "
        "===\n" + json.dumps(ctx, indent=2, default=str))


def deliberate(run_id: str) -> dict | None:
    """Full debate orchestrator. Returns the final aggregate review (with
    rounds embedded under 'rounds') or None if nothing could be produced."""
    cfg = _settings()
    reviewers = _available_reviewers(cfg)
    if not reviewers:
        return None
    ctx = _build_context(run_id)
    if not ctx:
        return None
    user_initial = _ctx_text(ctx)

    # Round 0 — independent reviews
    rounds: list[dict] = []
    positions: dict[str, dict] = {}
    threads, results = [], {}

    def _worker(rev):
        results[rev] = _call_reviewer(rev, SYSTEM, user_initial, cfg)

    for rev in reviewers:
        t = threading.Thread(target=_worker, args=(rev,),
                             daemon=True, name=f"council-r0-{rev}")
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    for rev in reviewers:
        if results.get(rev):
            positions[rev] = results[rev]
    if not positions:
        return None
    rounds.append({"round": 0, "positions": dict(positions)})

    # Debate rounds — only if >1 reviewer succeeded AND user enabled it
    final = None
    debate_on = bool(cfg.get("run_debate", True)) and len(positions) > 1
    max_rounds = max(0, int(cfg.get("debate_max_rounds", 3)))
    n_round = 0
    if debate_on:
        while n_round < max_rounds:
            # check agreement among all positions
            revs_sorted = sorted(positions.keys())
            agreed = all(_agreement(positions[revs_sorted[0]], positions[r])
                         for r in revs_sorted[1:])
            if agreed:
                break
            n_round += 1
            # each reviewer sees the OTHER reviewers' last position
            new_positions: dict[str, dict] = {}
            threads, results = [], {}

            def _debate(rev):
                others = {r: positions[r] for r in positions if r != rev}
                user_msg = (user_initial
                            + "\n\nOTHER REVIEWERS' POSITIONS (round "
                            + f"{n_round - 1}):\n"
                            + json.dumps(others, indent=2, default=str)
                            + "\n\nYour previous position:\n"
                            + json.dumps(positions[rev], indent=2, default=str))
                results[rev] = _call_reviewer(rev, DEBATE_SYSTEM, user_msg, cfg)

            for rev in positions.keys():
                t = threading.Thread(target=_debate, args=(rev,),
                                     daemon=True,
                                     name=f"council-r{n_round}-{rev}")
                t.start()
                threads.append(t)
            for t in threads:
                t.join()
            for rev in list(positions.keys()):
                if results.get(rev):
                    new_positions[rev] = results[rev]
                else:                            # call failed -> hold position
                    new_positions[rev] = positions[rev]
            positions = new_positions
            rounds.append({"round": n_round, "positions": dict(positions)})

    # Did they end up agreeing?
    revs_sorted = sorted(positions.keys())
    if len(revs_sorted) <= 1:
        agreed = True
    else:
        agreed = all(_agreement(positions[revs_sorted[0]], positions[r])
                     for r in revs_sorted[1:])

    tiebreaker = None
    if not agreed and _claude_available(cfg):
        user_tb = (user_initial
                   + "\n\nFINAL POSITIONS AFTER " + str(n_round)
                   + " ROUNDS OF DEBATE:\n"
                   + json.dumps(positions, indent=2, default=str))
        tiebreaker = _call_reviewer("claude", TIEBREAKER_SYSTEM, user_tb, cfg)
        rounds.append({"round": "tiebreaker", "positions": {
            "claude": tiebreaker} if tiebreaker else {}})
        if tiebreaker:
            final = dict(tiebreaker)
            final["reviewer"] = "claude (tiebreaker)"

    if final is None:
        # Consensus or no tiebreaker available — pick the first reviewer's
        # latest position as canonical (Gemini wins ties by alpha order).
        final = dict(positions[revs_sorted[0]])
        final["reviewer"] = (
            "+".join(revs_sorted) + (" (consensus)" if agreed else
                                     " (no agreement, no tiebreaker)")
        )

    final["rounds"] = rounds
    final["agreement"] = agreed
    final["reviewed_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    return final


# ── persistence + ideas.md surgery (unchanged interface) ─────────────────
# ── strategic review (batch) ─────────────────────────────────────────────
# Counter of runs finished since the last strategic review. The api layer
# bumps this on every track/finish; when it crosses the threshold we trigger
# one strategic review on the WHOLE BATCH.
_BATCH_LOCK = threading.Lock()
_BATCH_FINISHED_RUN_IDS: list[str] = []
_BATCH_INFLIGHT = False


def _gpu_count() -> int:
    db = SessionLocal()
    try:
        return db.query(Gpu).count() or 1
    finally:
        db.close()


def _strategic_threshold(cfg: dict) -> int:
    n = int(cfg.get("strategic_review_batch_n") or 0)
    return n if n > 0 else max(_gpu_count(), 1)


def note_run_finished(run_id: str) -> bool:
    """Called by api.track_finish for every run that lands. Adds the run to
    the strategic-review batch and, if the batch is full, kicks off the
    strategic review. Returns True if a strategic review was triggered.
    Per-run reviews can be enabled separately in Settings."""
    cfg = _settings()
    if not _available_reviewers(cfg):
        return False
    if not cfg.get("strategic_review_enabled", True):
        return False
    global _BATCH_INFLIGHT
    threshold = _strategic_threshold(cfg)
    with _BATCH_LOCK:
        _BATCH_FINISHED_RUN_IDS.append(run_id)
        if _BATCH_INFLIGHT or len(_BATCH_FINISHED_RUN_IDS) < threshold:
            return False
        batch = list(_BATCH_FINISHED_RUN_IDS)
        _BATCH_FINISHED_RUN_IDS.clear()
        _BATCH_INFLIGHT = True
    threading.Thread(target=_strategic_worker, args=(batch,), daemon=True,
                     name=f"council-strategic-{len(batch)}").start()
    return True


def _strategic_worker(batch_run_ids: list[str]) -> None:
    global _BATCH_INFLIGHT
    try:
        # Only ONE strategic review at a time across the process (cheap
        # protection against runaway cost).
        if not _GLOBAL_SEMAPHORE.acquire(blocking=False):
            print("[council/strategic] busy — skipping this batch",
                  flush=True)
            return
        try:
            review = strategic_review(batch_run_ids)
            if not review:
                return
            # Persist a single Event + ChatMessage so the UI shows it,
            # apply rerank/new ideas to ideas.md (kept as read-only render
            # for backward compat), drive directives.jsonl (authoritative
            # via _persist_strategic -> _apply_to_directives_jsonl), and
            # write learning to lessons.md.
            _persist_strategic(batch_run_ids, review)
            _apply_to_ideas_md(review)
        finally:
            _GLOBAL_SEMAPHORE.release()
    finally:
        with _BATCH_LOCK:
            _BATCH_INFLIGHT = False


def _build_strategic_context(batch_run_ids: list[str]) -> dict | None:
    db = SessionLocal()
    try:
        proj = db.query(Project).first()
        if not proj:
            return None
        batch = [db.query(Run).filter(Run.id == rid).first()
                 for rid in batch_run_ids]
        batch = [r for r in batch if r is not None]
        if not batch:
            return None
        every_run = db.query(Run).all()
        frontier = _frontier_ids(every_run)
        ideas = db.query(Idea).filter(Idea.id.like("deck-%")).all()
        maximize = proj.metric_direction == "maximize"

        others = [r for r in every_run if r.id not in {r2.id for r2 in batch}]

        def _key(r):
            on_front = r.id in frontier
            is_crash = (r.status == "crashed")
            return (0 if on_front else (1 if is_crash else 2),
                    r.created_at or "")
        others.sort(key=_key)
        run_lines = [_compact_one_line(r, frontier, maximize) for r in others]
        run_lines_text = "\n".join(run_lines)[:24000]
        stats = _aggregate_stats(every_run, proj)

        return {
            "project": {
                "name": proj.name, "purpose": proj.purpose,
                "metric": proj.validation_metric,
                "direction": proj.metric_direction,
                "baseline_metric": getattr(proj, "baseline_metric", None),
            },
            "batch": [
                {
                    "id": r.id, "name": r.run_name, "status": r.status,
                    "headline_metric": r.headline_metric,
                    "baseline_delta": r.baseline_delta,
                    "config": r.config if isinstance(r.config, dict) else {},
                }
                for r in batch
            ],
            "aggregate": stats,
            "all_prior_runs_count": len(others),
            "all_prior_runs_oneliners": run_lines_text,
            "pending_ideas": [
                {"idea_id": i.idea_id, "what": i.description}
                for i in ideas
            ][:30],
            "pending_total_count": len(ideas),
            "lessons_so_far": _read_lessons(max_chars=6000),
        }
    finally:
        db.close()


def _strategic_ctx_text(ctx: dict, strategic_state: dict | None = None) -> str:
    runs_block = ctx.pop("all_prior_runs_oneliners", "") or "(none)"
    lessons_block = ctx.pop("lessons_so_far", "").strip() or "(none yet)"
    # Strategic state header — deterministic block consumed by the LLM
    # without recomputation. The escalation rule + orthogonal quota are
    # both downstream of these three numbers.
    if strategic_state:
        state_block = (
            "=== PRIOR STRATEGIC STATE (deterministic — echo back) ===\n"
            f"previous_top_directive_id: "
            f"{strategic_state.get('previous_top_directive_id', '')}\n"
            f"previous_directive_implemented: "
            f"{strategic_state.get('previous_directive_implemented', 'YES')}\n"
            f"consecutive_unimplemented_count: "
            f"{strategic_state.get('consecutive_unimplemented_count', 0)}\n"
            f"PRIOR 3 STRATEGIC VERDICTS:\n"
            + "\n".join(f"  - {v}" for v in
                       (strategic_state.get('prior_verdicts') or [])
                       [:3]) + "\n\n"
        )
        # Surface the last verdict explicitly — DEBATE_SYSTEM checks for
        # the literal "PRIOR STRATEGIC VERDICT: stagnant" / "regressing"
        # to flip into adversarial mode.
        last_verdict = (strategic_state.get('prior_verdicts') or [""])[0]
        if last_verdict:
            state_block += (
                f"PRIOR STRATEGIC VERDICT: {last_verdict}\n\n"
            )
    else:
        state_block = ""
    return (
        "You are doing a STRATEGIC review of a BATCH of recent runs (one "
        "wave of parallel experiments). Step back: look at the WHOLE "
        "project trajectory, this batch's results, and recommend the next "
        "research move. Return JSON per schema.\n\n"
        + state_block
        + "=== LESSONS LEARNED SO FAR (your prior reviews) ===\n"
        + lessons_block + "\n\n"
        f"=== ALL PRIOR RUNS ({ctx.get('all_prior_runs_count', 0)} total — "
        "frontier-movers ★ first, then crashed) ===\n"
        "status    metric   run_id                              what\n"
        + runs_block + "\n\n"
        "=== STRUCTURED CONTEXT (project / this batch / aggregate / pending)"
        " ===\n" + json.dumps(ctx, indent=2, default=str))


def _compute_strategic_state() -> dict:
    """Deterministically compute the orchestrator-side fields the
    strategic prompt depends on (RESEARCH_IMPROVEMENT_PLAN #2).

    Returns:
      {
        "previous_top_directive_id": "<id>",
        "previous_directive_implemented": "YES|NO|IN_PROGRESS",
        "consecutive_unimplemented_count": <int>,
        "prior_verdicts": ["<v_last>", "<v_-2>", "<v_-3>"],
        "stagnant_streak": <int>,
      }
    """
    prev_top = _previous_top_directive_id()
    implemented = _was_directive_implemented(prev_top) if prev_top else "YES"
    # Count consecutive unimplemented only if implemented==NO right now.
    if implemented == "NO":
        # Add +1 for the current review BEFORE recording it.
        ccu = _consecutive_unimplemented_count(prev_top) + 1
    else:
        ccu = 0
    h = _strategic_history()
    prior_verdicts = [str(e.get("verdict") or "") for e in h[:3]]
    stagnant_streak = _consecutive_stagnant_count()
    return {
        "previous_top_directive_id": prev_top,
        "previous_directive_implemented": implemented,
        "consecutive_unimplemented_count": ccu,
        "prior_verdicts": prior_verdicts,
        "stagnant_streak": stagnant_streak,
    }


def strategic_review(batch_run_ids: list[str]) -> dict | None:
    """Run ONE expert through the strategic-review prompt over a batch.
    Strategic reviews don't debate — they're already a 'reflection' call
    that asks the model to look at the whole trajectory at once. We pick
    the highest-quality reviewer available (claude > openai > gemini)
    since the call only fires every N runs and we want the best judgment.

    The orchestrator-computed strategic state (previous top directive,
    implemented?, consecutive unimplemented count) is injected into the
    user message verbatim — the LLM is instructed to echo back the
    deterministic fields rather than re-compute them.
    """
    cfg = _settings()
    available = _available_reviewers(cfg)
    if not available:
        return None
    # Prefer claude for strategic if we have it, else openai, else gemini.
    if "claude" in available:
        reviewer = "claude"
    elif "openai" in available:
        reviewer = "openai"
    else:
        reviewer = available[0]
    ctx = _build_strategic_context(batch_run_ids)
    if not ctx:
        return None
    strategic_state = _compute_strategic_state()
    user = _strategic_ctx_text(ctx, strategic_state=strategic_state)
    print(f"[council/strategic] running {reviewer} over batch of "
          f"{len(batch_run_ids)} runs; ccu="
          f"{strategic_state['consecutive_unimplemented_count']}",
          flush=True)
    out = _call_reviewer(reviewer, STRATEGIC_SYSTEM, user, cfg)
    if not out:
        return None
    # HARD ESCALATION OVERRIDE — deterministic, not LLM-trusted. If
    # the orchestrator computed >= 3 consecutive unimplemented on the
    # same top directive, FORCE verdict=ESCALATION_HALT regardless of
    # what the model said, AND append a HALT directive if the council
    # didn't already include one.
    if strategic_state["consecutive_unimplemented_count"] >= 3:
        out["verdict"] = "ESCALATION_HALT"
        ups = list(out.get("directives_upsert") or [])
        if not any(str(d.get("type")) == "HALT" for d in ups):
            ups.append({
                "type": "HALT",
                "priority": 9999,
                "what": (f"ESCALATION_HALT: {strategic_state['consecutive_unimplemented_count']} "
                         "consecutive reviews on top directive "
                         f"{strategic_state['previous_top_directive_id']} with "
                         "no implementation — human PI required"),
                "acceptance": "Human PI marks this HALT resolved",
                "idea_class": "INFRA",
                "author": "council:escalation",
            })
        out["directives_upsert"] = ups
    out["scope"] = "strategic"
    out["batch_run_ids"] = batch_run_ids
    out["reviewer"] = reviewer + " (strategic)"
    out["strategic_state"] = strategic_state
    return out


def _persist_strategic(batch_run_ids: list[str], review: dict) -> None:
    """Persist a strategic review: write a ChatMessage + Event so it's
    visible in the Summary feed, append the learning to lessons.md, mark
    each batch run's config['strategic_review_id'], and record an entry
    in strategic_review_history for the next call's escalation counter.
    """
    db = SessionLocal()
    learning = (review.get("learning") or "").strip()
    pivot = review.get("pivot") or {}
    verdict = str(review.get("verdict") or "").lower()
    is_halt = verdict == "escalation_halt"
    rev_id = "rv-" + os.urandom(4).hex()
    try:
        # tag each run with the strategic_review's id
        for rid in batch_run_ids:
            r = db.query(Run).filter(Run.id == rid).first()
            if not r:
                continue
            cfg = dict(r.config) if isinstance(r.config, dict) else {}
            cfg["strategic_review_id"] = rev_id
            cfg["strategic_review"] = review
            r.config = cfg
        msg = (f"[Strategic review · {review.get('reviewer')}] "
               + (learning or "(no learning)"))[:1200]
        if is_halt:
            msg = ("[Strategic review — ESCALATION_HALT] "
                   "Agent has ignored the same top directive for "
                   "3+ reviews. Hard-halting runs pending human PI.\n\n"
                   + (learning or ""))[:1200]
        elif pivot.get("recommend"):
            msg = (f"[Strategic review — RECOMMENDING PIVOT to: "
                   f"{pivot.get('to_what', '?')}]  ") + (learning or "")
        db.add(ChatMessage(id="cm-" + os.urandom(4).hex(),
                           role="agent", content=msg, created_at=_iso()))
        db.add(Event(id="ev-" + os.urandom(4).hex(),
                     type=("escalation_halt" if is_halt
                           else "strategic_review"),
                     severity=("critical" if is_halt else "info"),
                     actor="council:" + (review.get("reviewer") or "?"),
                     message=(("ESCALATION_HALT: " if is_halt else "")
                              + (learning[:280] or
                                 "Strategic review completed")),
                     created_at=_iso()))
        db.commit()
    finally:
        db.close()
    # Persist directives + history.
    try:
        report = _apply_to_directives_jsonl(review)
        review["directives_report"] = report
        if report.get("rejected"):
            db = SessionLocal()
            try:
                db.add(Event(id="ev-" + os.urandom(4).hex(),
                             type="council_validator_rejected",
                             severity="warning",
                             actor="council:strategic",
                             message=("Council output rejected: "
                                      + str(report["rejected"])[:240]),
                             created_at=_iso()))
                db.commit()
            finally:
                db.close()
    except Exception as e:                                  # noqa: BLE001
        print(f"[council/strategic] directives apply failed: {e}",
              flush=True)
    # Record a tiny summary in strategic_review_history so the NEXT call
    # can compute consecutive_unimplemented_count deterministically.
    try:
        # Resolve "top directive id" — prefer the council's first upsert,
        # else fall back to whatever top_open() now sees.
        from . import directives as _d
        ups = review.get("directives_upsert") or []
        top_did = ""
        if ups and isinstance(ups[0], dict):
            top_did = str(ups[0].get("id") or "")
        if not top_did:
            t = _d.top_open()
            top_did = str(t.get("id") or "") if t else ""
        prev_top = (review.get("strategic_state") or {}).get(
            "previous_top_directive_id") or ""
        # If the new top is the same as the previous AND the implemented
        # field is NO, that's the streak signal.
        prev_impl = (review.get("strategic_state") or {}).get(
            "previous_directive_implemented") or "YES"
        _append_strategic_history({
            "id": rev_id,
            "at": _iso(),
            "verdict": verdict,
            "top_directive_id": top_did,
            "previous_top_directive_id": prev_top,
            "implemented": prev_impl,
            "reviewer": review.get("reviewer") or "",
        })
    except Exception as e:                                  # noqa: BLE001
        print(f"[council/strategic] history append failed: {e}",
              flush=True)
    # If we escalated, set research_halted so /api/track/run blocks ALL
    # runs (including _probe/_smoke). The PI agent will also see the
    # ESCALATION_HALT event and call /api/halt explicitly.
    if is_halt:
        try:
            from . import notify
            notify.set_research_halted(
                True, reason=(learning[:240] or
                              "Strategic council escalation"))
        except Exception as e:                              # noqa: BLE001
            print(f"[council/strategic] set_research_halted failed: {e}",
                  flush=True)
    try:
        _append_lesson(
            reviewer=(review.get("reviewer") or "strategic"),
            run_name=f"batch of {len(batch_run_ids)} runs",
            learning=learning)
    except Exception as e:                              # noqa: BLE001
        print(f"[council/strategic] _append_lesson failed: {e}", flush=True)
    try:
        from .bus import bus
        bus.publish("events", "runs_changed", {})
    except Exception:
        pass


def review_async(run_id: str) -> bool:
    """Fire-and-forget background debate. Idempotent per run, rate-limited
    per idea_id, and globally capped to _GLOBAL_MAX_CONCURRENT in-flight.

    Per-run reviews are GATED on council_per_run_enabled (default False
    after the strategic-review redesign); the strategic batch review does
    the heavy lifting."""
    cfg = _settings()
    if not _available_reviewers(cfg):
        return False
    if not cfg.get("council_per_run_enabled", False):
        return False
    if not _worth_reviewing(run_id):
        return False
    # Per-idea cooldown: at most 1 review per idea every _COOLDOWN_SEC.
    idea = _idea_id_of(run_id)
    if idea:
        with _COOLDOWN_LOCK:
            t = _LAST_REVIEW_AT.get(idea, 0)
            if time.time() - t < _COOLDOWN_SEC:
                return False
            # Mark optimistically; the worker will refresh it on success.
            _LAST_REVIEW_AT[idea] = time.time()
    with _INFLIGHT_LOCK:
        if run_id in _INFLIGHT:
            return False
        _INFLIGHT.add(run_id)
    # Global semaphore: if 2 reviews are already in flight, skip this one
    # rather than queueing it (queueing would mask runaway agent behaviour).
    if not _GLOBAL_SEMAPHORE.acquire(blocking=False):
        with _INFLIGHT_LOCK:
            _INFLIGHT.discard(run_id)
        print(f"[council] busy — skipping review of {run_id}", flush=True)
        return False
    threading.Thread(target=_worker_wrapped, args=(run_id,), daemon=True,
                     name=f"council-{run_id[:16]}").start()
    return True


def _idea_id_of(run_id: str) -> str:
    db = SessionLocal()
    try:
        r = db.query(Run).filter(Run.id == run_id).first()
        return (r.idea_id or "") if r else ""
    finally:
        db.close()


def _worker_wrapped(run_id: str) -> None:
    """Wraps _worker so the global semaphore is always released."""
    try:
        _worker(run_id)
    finally:
        try:
            _GLOBAL_SEMAPHORE.release()
        except Exception:
            pass


def _worth_reviewing(run_id: str) -> bool:
    """Skip runs that have nothing to teach: _probe pings, never-started
    runs, already-reviewed runs (in case of restart)."""
    if not run_id or run_id.startswith("_probe") or run_id == "_probe":
        return False
    db = SessionLocal()
    try:
        run = db.query(Run).filter(Run.id == run_id).first()
        if not run:
            return False
        if run.status not in ("kept", "discarded", "crashed", "failed",
                              "success"):
            return False
        # Already reviewed in this process? skip.
        cfg = run.config if isinstance(run.config, dict) else {}
        if cfg.get("reviews") or cfg.get("review"):
            return False
        return True
    finally:
        db.close()


def _worker(run_id: str) -> None:
    try:
        review = deliberate(run_id)
        if not review:
            return
        _persist(run_id, review)
        _apply_to_ideas_md(review)
    finally:
        with _INFLIGHT_LOCK:
            _INFLIGHT.discard(run_id)


def _iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _persist(run_id: str, review: dict) -> None:
    """Stash the review on run.config so /api/runs returns it, mirror the
    learning onto its Idea, and emit a feed event."""
    db = SessionLocal()
    captured_run_name = run_id
    try:
        run = db.query(Run).filter(Run.id == run_id).first()
        if not run:
            return
        captured_run_name = run.run_name or run.id    # capture before close
        cfg = dict(run.config) if isinstance(run.config, dict) else {}
        # Keep both: 'review' for backwards-compat with old UI, and the full
        # 'reviews' structure with all per-round positions.
        cfg["review"] = {k: v for k, v in review.items() if k != "rounds"}
        cfg["reviews"] = review              # full debate history
        run.config = cfg
        if run.idea_id:
            idea = db.query(Idea).filter(Idea.id == run.idea_id).first()
            if idea and (review.get("learning") or "").strip():
                idea.conclusion = review["learning"].strip()
        msg = (review.get("learning") or "")[:280] or (
            f"Council reviewed {run.run_name}")
        if review.get("agreement") is False:
            msg = "[tiebreaker] " + msg
        db.add(Event(
            id="ev-" + os.urandom(4).hex(),
            type="council_reviewed", severity="info",
            actor="council:" + (review.get("reviewer") or "?"),
            run_id=run.id,
            message=msg, created_at=_iso()))
        db.commit()
    finally:
        db.close()
    # Append the review's learning to lessons.md so future reviews see it.
    try:
        _append_lesson(
            reviewer=(review.get("reviewer") or "?"),
            run_name=captured_run_name,
            learning=review.get("learning") or "")
    except Exception as e:                              # noqa: BLE001
        print(f"[council] _append_lesson failed: {e}", flush=True)
    try:
        from .bus import bus
        bus.publish("events", "runs_changed", {})
    except Exception:
        pass


# ── ideas.md surgery ──────────────────────────────────────────────────────
_HEADER_RE = re.compile(r"^\|\s*status\s*\|", re.IGNORECASE)


def _onboarding_repo_name() -> str:
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(Setting.key == "onboarding").first()
        if not row or not isinstance(row.value, dict):
            return ""
        return (row.value.get("repo_name") or "").strip()
    finally:
        db.close()


def _apply_to_ideas_md(review: dict) -> None:
    """Rewrite the pending block of ideas.md per the review's rerank, append
    new_ideas, and veto vetoed ones. Atomic write (write tmp + rename).
    Serialized under _FILE_LOCK so concurrent reviews don't fight."""
    name = _onboarding_repo_name()
    if not name:
        return
    path = DATA_DIR / "workspace" / name / "ideas.md"
    if not path.exists():
        return
    with _FILE_LOCK:
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            return
        lines = text.splitlines()

        # Find the first ideas-table header row and walk to the end of its block.
        hdr = -1
        for i, ln in enumerate(lines):
            if _HEADER_RE.match(ln.strip()):
                hdr = i
                break
        if hdr < 0:
            return
        body_start = hdr + 1
        if (body_start < len(lines)
                and re.fullmatch(r"\|[\s\-\|:]+", lines[body_start].strip() or "|")):
            body_start += 1
        body_end = body_start
        while body_end < len(lines) and lines[body_end].lstrip().startswith("|"):
            body_end += 1

        pending: list[tuple[str, list[str]]] = []
        done_rows: list[str] = []
        for ln in lines[body_start:body_end]:
            cells = [c.strip() for c in ln.strip().strip("|").split("|")]
            if len(cells) < 2:
                done_rows.append(ln)
                continue
            status_cell = cells[0].lower()
            idea_id = cells[1].strip("`* ")
            is_pending = any(w in status_cell for w in
                             ("pending", "todo", "queued", "planned", "next"))
            if is_pending and idea_id:
                pending.append((idea_id, cells))
            else:
                done_rows.append(ln)

        if not pending and not review.get("new_ideas"):
            return

        veto = {str(v) for v in (review.get("veto") or []) if v}
        rerank = [str(x) for x in (review.get("rerank_pending") or []) if x]
        pending_by_id = {p[0]: p[1] for p in pending}
        new_pending: list[list[str]] = []
        for rid in rerank:
            if rid in veto:
                continue
            if rid in pending_by_id:
                new_pending.append(pending_by_id.pop(rid))
        for rid, cells in pending:                          # un-ranked tail
            if rid in pending_by_id and rid not in veto:
                new_pending.append(cells)
                pending_by_id.pop(rid, None)

        PENDING_HEALTHY = 30
        existing_ids = {p[0] for p in new_pending}
        existing_whats = {(p[2] if len(p) > 2 else "").strip().lower()
                          for p in new_pending}
        arity = max((len(p[1]) for p in pending), default=4)
        if len(new_pending) < PENDING_HEALTHY:
            room = PENDING_HEALTHY - len(new_pending)
            for ni in (review.get("new_ideas") or [])[:min(3, room)]:
                if not isinstance(ni, dict):
                    continue
                idea_id = re.sub(r"[^A-Za-z0-9_]+", "_",
                                 str(ni.get("idea_id") or "")).strip("_")
                if not idea_id or idea_id in existing_ids:
                    continue
                what = str(ni.get("what") or "").strip()
                wlo = what.lower()
                if any(wlo and (wlo in ew or ew in wlo) and len(ew) > 10
                       for ew in existing_whats):
                    continue
                why = str(ni.get("why") or "").strip()
                row = ["pending", idea_id, what, why]
                while len(row) < arity:
                    row.append("")
                new_pending.append(row[:arity])
                existing_ids.add(idea_id)
                existing_whats.add(wlo)

        out_rows = list(done_rows)
        out_rows.extend("| " + " | ".join(cells) + " |"
                        for cells in new_pending)
        if veto:
            out_rows.append("")
            for rid in sorted(veto):
                out_rows.append(f"<!-- council vetoed: {rid} -->")
        new_text = ("\n".join(lines[:body_start])
                    + ("\n" if body_start else "")
                    + "\n".join(out_rows)
                    + "\n"
                    + "\n".join(lines[body_end:]))
        if not new_text.endswith("\n"):
            new_text += "\n"

        try:
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".ideas-",
                                       suffix=".md")
            with os.fdopen(fd, "w") as f:
                f.write(new_text)
            shutil.move(tmp, str(path))
            print(f"[council] rewrote {path} — {len(new_pending)} pending, "
                  f"{len(veto)} vetoed", flush=True)
        except Exception as e:                          # noqa: BLE001
            print(f"[council] could not rewrite ideas.md: {e}", flush=True)


# ════════════════════════════════════════════════════════════════════════
#         Strategic review state — escalation counter + history
#         (RESEARCH_IMPROVEMENT_PLAN #2 / #5 / #10)
# ════════════════════════════════════════════════════════════════════════
# We persist the last N strategic verdicts to a Setting row so the
# escalation counter survives process restarts. Each entry is a small
# dict — the LLM call dwarfs any DB overhead so we store the whole
# review (sans `rounds` to keep size bounded).

_STRAT_HISTORY_KEY = "strategic_review_history"
_STRAT_HISTORY_MAX = 30


def _strategic_history() -> list[dict]:
    """Return the persisted history (newest first)."""
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(
            Setting.key == _STRAT_HISTORY_KEY).first()
        if row and isinstance(row.value, dict):
            v = row.value.get("entries") or []
            return list(v) if isinstance(v, list) else []
        return []
    finally:
        db.close()


def _append_strategic_history(entry: dict) -> None:
    """Persist a small summary of a strategic verdict so future reviews
    can compute consecutive_unimplemented_count deterministically."""
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(
            Setting.key == _STRAT_HISTORY_KEY).first()
        existing: list[dict] = []
        if row and isinstance(row.value, dict):
            existing = list(row.value.get("entries") or [])
        existing.insert(0, entry)
        existing = existing[:_STRAT_HISTORY_MAX]
        value = {"entries": existing}
        if row:
            row.value = value
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(row, "value")
        else:
            db.add(Setting(key=_STRAT_HISTORY_KEY, value=value))
        db.commit()
    finally:
        db.close()


def _previous_top_directive_id() -> str:
    """The most recent persisted top_directive_id, or "" if none."""
    h = _strategic_history()
    if not h:
        return ""
    return str(h[0].get("top_directive_id") or "")


def _was_directive_implemented(top_directive_id: str) -> str:
    """Has the top directive from the last review been implemented?
    Returns YES / NO / IN_PROGRESS based on the current directives.jsonl
    state. Deterministic — no LLM involved."""
    if not top_directive_id:
        return "YES"  # nothing to check — vacuously true
    from . import directives as _d
    cur = _d.get(top_directive_id)
    if cur is None:
        # Directive no longer exists — treat as implemented (council may
        # have replaced it with new structure).
        return "YES"
    st = (cur.get("status") or "").lower()
    if st in ("done", "vetoed"):
        return "YES"
    if st == "in_progress":
        return "IN_PROGRESS"
    return "NO"


def _consecutive_unimplemented_count(prev_top: str) -> int:
    """Count the consecutive trailing history entries where the SAME
    top_directive_id was carried over with implemented=NO. Deterministic —
    the LLM only echoes this back, it does not compute it."""
    if not prev_top:
        return 0
    h = _strategic_history()
    streak = 0
    for entry in h:
        if (str(entry.get("top_directive_id") or "") == prev_top
                and str(entry.get("implemented") or "").upper() == "NO"):
            streak += 1
        else:
            break
    return streak


# ════════════════════════════════════════════════════════════════════════
#         directives.jsonl applier + validator
#         (RESEARCH_IMPROVEMENT_PLAN #1 / #5)
# ════════════════════════════════════════════════════════════════════════

# Hard cap on INCREMENTAL:ORTHOGONAL ratio across all OPEN directives.
# A council output that would push the ratio above this is rejected.
_MAX_INCREMENTAL_ORTHOGONAL_RATIO = 3.0


def _gpu_count_for_quota() -> int:
    """Same as _gpu_count but inlined here so the test can monkey-patch
    just this without touching the batch trigger."""
    return _gpu_count()


def _consecutive_stagnant_count() -> int:
    """How many trailing strategic reviews have verdict ∈
    {stagnant, regressing}. Used by the ORTHOGONAL-quota rule."""
    h = _strategic_history()
    streak = 0
    for entry in h:
        v = str(entry.get("verdict") or "").lower()
        if v in ("stagnant", "regressing", "escalation_halt"):
            streak += 1
        else:
            break
    return streak


def _validate_directives_upsert(upsert: list[dict],
                                  prior_verdict: str,
                                  stagnant_streak: int) -> tuple[bool, str]:
    """Enforce the schema + quota rules from RESEARCH_IMPROVEMENT_PLAN #5.

    Returns (ok, error). ``ok=False`` means the council's output should
    NOT be persisted — the caller surfaces ``error`` as a Setting +
    Event so the next review knows what went wrong.

    Rules:
      1. Every entry must pass directives.validate_directive() — in
         particular every entry MUST carry idea_class ∈ IDEA_CLASSES.
      2. If we've been stagnant/regressing for >= 2*GPU_COUNT consecutive
         reviews, the upsert MUST include at least one ORTHOGONAL or
         REPRODUCE directive.
      3. Combined with currently-open directives, the INCREMENTAL :
         ORTHOGONAL ratio must not exceed 3:1. (REPRODUCE counts on the
         ORTHOGONAL side.)
    """
    from . import directives as _d
    if not isinstance(upsert, list):
        return False, "directives_upsert must be a list"
    # 1) per-entry validation.
    for i, d in enumerate(upsert):
        ok, err = _d.validate_directive(d)
        if not ok:
            return False, f"entry #{i}: {err}"
        if not d.get("idea_class"):
            return False, (f"entry #{i}: idea_class is REQUIRED on every "
                           "directives_upsert entry")
    # 2) ORTHOGONAL/REPRODUCE quota when stagnant.
    gpu_n = max(_gpu_count_for_quota(), 1)
    quota_floor = 2 * gpu_n
    if stagnant_streak >= quota_floor and upsert:
        has_diverging = any(
            (d.get("idea_class") in ("ORTHOGONAL", "REPRODUCE"))
            for d in upsert)
        if not has_diverging:
            return False, (
                f"verdict has been stagnant/regressing for {stagnant_streak} "
                f">= 2*GPU_COUNT={quota_floor} consecutive reviews — "
                "directives_upsert MUST include >= 1 ORTHOGONAL or REPRODUCE "
                "entry but none was provided")
    # 3) 3:1 INCREMENTAL:ORTHOGONAL across OPEN directives.
    cur = _d.counts_by_idea_class()
    for d in upsert:
        ic = d.get("idea_class") or "INCREMENTAL"
        cur[ic] = cur.get(ic, 0) + 1
    inc = cur.get("INCREMENTAL", 0)
    orth = cur.get("ORTHOGONAL", 0) + cur.get("REPRODUCE", 0)
    if orth > 0 and inc / orth > _MAX_INCREMENTAL_ORTHOGONAL_RATIO:
        return False, (
            f"INCREMENTAL:ORTHOGONAL ratio across all OPEN directives "
            f"would be {inc}:{orth} which exceeds the hard cap of 3:1")
    # 3b) all-INCREMENTAL with no orthogonal anywhere: only reject if the
    # number of open INCREMENTAL is already at or above the floor (4) —
    # this guards against the system collapsing to HP grids when the
    # council is healthy. We surface this as a soft warning, not a hard
    # rejection, so a council that's NOT stagnant can still ship purely
    # incremental work.
    return True, ""


def _apply_to_directives_jsonl(review: dict) -> dict:
    """Translate a strategic review's ``directives_upsert`` /
    ``directives_close`` payloads into the directives.jsonl file.

    Returns a small report ``{"upserted": N, "closed": M, "rejected": "..."}``
    that the caller can attach to the persisted strategic review for
    visibility on the dashboard.

    On validator failure NOTHING is persisted — the caller sees
    ``rejected=<reason>`` and emits an Event so the next strategic
    review knows the prior output was bad.
    """
    from . import directives as _d
    upsert = list(review.get("directives_upsert") or [])
    close_ids = list(review.get("directives_close") or [])
    prior_verdict = str(review.get("verdict") or "").lower()
    stagnant_streak = _consecutive_stagnant_count()
    ok, err = _validate_directives_upsert(
        upsert, prior_verdict=prior_verdict,
        stagnant_streak=stagnant_streak)
    if not ok:
        print(f"[council/directives] REJECTED: {err}", flush=True)
        return {"upserted": 0, "closed": 0, "rejected": err}
    n_up = 0
    n_close = 0
    for entry in upsert:
        try:
            _d.upsert(entry)
            n_up += 1
        except Exception as e:                              # noqa: BLE001
            print(f"[council/directives] upsert failed: {e}", flush=True)
    for did in close_ids:
        try:
            if _d.close(str(did), evidence="closed by strategic council"):
                n_close += 1
        except Exception as e:                              # noqa: BLE001
            print(f"[council/directives] close failed: {e}", flush=True)
    return {"upserted": n_up, "closed": n_close, "rejected": ""}


# ════════════════════════════════════════════════════════════════════════
#                       Code-bless — pre-flight code review
# ════════════════════════════════════════════════════════════════════════
#
# After the research agent scaffolds program.md / train.py / prepare.py /
# ideas.md but BEFORE any training run launches, the council reviews the
# codebase for blocking bugs (arui SDK usage, __METRIC__ wiring, baseline
# correctness, eval hookup, off-by-ones). The verdict gates POST
# /api/track/run — until the council approves, the agent cannot start
# experiments. This prevents the classic "we waste 10 GPU-hours then
# discover the metric was being maximised when it should be minimised."
#
# State machine — persisted to Setting key "code_bless":
#   {"status": "pending"}           review in flight
#   {"status": "approved",          unlocked: /api/track/run accepts
#    "at": iso, "verdicts": {...}}
#   {"status": "rejected",          blocked: agent must fix + re-submit
#    "blockers": [...], "verdicts": {...}}
#   absent / {"status": "not_requested"}  no bless ever requested → blocked

_BLESS_SYSTEM = """You are a senior ML research engineer doing a final code
review BEFORE the autoresearcher agent launches any training run. You will
be shown the entire research codebase (program.md, train.py, prepare.py,
ideas.md, plus any model/eval helper files).

Your job is to catch BLOCKING bugs — things that would invalidate every
single run if left in. Examples of blockers:

  - the arui SDK is not used to log the validation metric, OR the headline
    metric key `arui.summary["__METRIC__"]` is misspelled or missing
  - the metric being reported maximises when it should minimise (e.g.
    logging loss but the metric direction is "maximize") or vice versa
  - the evaluation set is the SAME as the training set (data leakage)
  - the baseline doesn't match what was promised in program.md
  - train.py crashes on import (missing class, undefined variable)
  - the script never calls .backward() / never updates weights
  - obvious off-by-ones in epoch / step counting that would mis-attribute
    metrics
  - missing seed handling that makes runs non-deterministic without warning
  - the dataset loader assumes a path that doesn't exist on this node
  - SEED-TASK GATE (RESEARCH_IMPROVEMENT_PLAN #7): the validation set has
    < 100 examples and program.md does NOT explicitly mark
    `dataset_kind: smoke`. This is a smoke task, not a research baseline.
  - SEED-TASK GATE: the agent's bless request reports `train_30s: true`
    (train.py runs to completion in < 30s on a single CPU). That is a
    smoke test, not a research baseline.

Do NOT flag:
  - style nits, hyperparameter choices, model architecture preferences,
    suggestions for ablations, "consider also trying X" — these are
    research decisions, NOT blockers.
  - missing GPU-saturation logic — the orchestrator handles that.
  - the presence of TODOs that don't break the baseline.

Reply with STRICT JSON:
  {
    "approved": <true if NO blockers found, else false>,
    "blockers": [<one short sentence per blocker, each ending with the
                  filename + roughly which lines>],
    "suggestions": [<optional non-blocking improvements>],
    "summary": "<one-sentence verdict for the dashboard>"
  }

NEVER set approved=true with blockers in the list. NEVER cite style
issues as blockers. Be strict but not paranoid — if you can imagine the
baseline run scoring something meaningful, approve it.
"""


def _collect_codebase(workspace: str | os.PathLike) -> str:
    """Read every text file in the agent's workspace that's small enough to
    fit in the prompt. Skips binaries, caches, datasets, virtualenvs."""
    from pathlib import Path
    ws = Path(str(workspace))
    if not ws.exists():
        return ""
    SKIP_DIRS = {"__pycache__", ".git", ".venv", "venv", "data", "ckpts",
                 "checkpoints", "runs", "wandb", "node_modules"}
    EXT = {".py", ".md", ".yaml", ".yml", ".json", ".toml", ".cfg", ".sh"}
    MAX_BYTES_PER_FILE = 24_000
    MAX_TOTAL = 220_000
    chunks: list[str] = []
    total = 0
    for p in sorted(ws.rglob("*")):
        if not p.is_file():
            continue
        # SKIP_DIRS must match only directory components RELATIVE to
        # the workspace, not absolute-path ancestors. Every
        # autoresearcherUI workspace lives under data/workspace/...
        # by setup convention, so a naive `part in SKIP_DIRS for part
        # in p.parts` skipped EVERY file because the ancestor "data"
        # always matched. Council then saw an empty codebase and
        # auto-rejected (the bug Francois hit on 2026-05-31, found
        # and one-line-fixed by the research agent in-flight).
        try:
            rel_parts = p.relative_to(ws).parts
        except ValueError:
            continue                                # outside workspace
        if any(part in SKIP_DIRS for part in rel_parts[:-1]):
            continue
        if p.suffix.lower() not in EXT and p.name not in (
                "Dockerfile", "Makefile"):
            continue
        try:
            text = p.read_text(errors="ignore")
        except OSError:
            continue
        if not text.strip():
            continue
        text = text[:MAX_BYTES_PER_FILE]
        rel = p.relative_to(ws).as_posix()
        block = f"\n\n===== FILE: {rel} =====\n{text}\n"
        if total + len(block) > MAX_TOTAL:
            chunks.append("\n\n[... codebase truncated to fit context ...]\n")
            break
        chunks.append(block)
        total += len(block)
    return "".join(chunks).strip()


def _bless_state_get() -> dict:
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(Setting.key == "code_bless").first()
        if row and isinstance(row.value, dict):
            return dict(row.value)
        return {"status": "not_requested"}
    finally:
        db.close()


def _bless_state_set(state: dict) -> None:
    state = dict(state)
    state.setdefault("updated_at",
                     dt.datetime.now(dt.timezone.utc).isoformat())
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(Setting.key == "code_bless").first()
        if row:
            row.value = state
        else:
            db.add(Setting(key="code_bless", value=state))
        db.commit()
    finally:
        db.close()
    try:
        from .bus import bus
        bus.publish("events", "runs_changed", {})
    except Exception:
        pass


def bless_status() -> dict:
    """Live state of the pre-flight code review. Read by the dashboard and
    by the /api/track/run gate. Augments the raw _bless_state_get() value
    with a `preflight` block so the dashboard's 3-pill banner can render
    without a second round-trip."""
    st = dict(_bless_state_get())
    st["preflight"] = preflight_summary()
    return st


# ════════════════════════════════════════════════════════════════════════
#                       Pre-flight SOP (steps 1 + 2)
# ════════════════════════════════════════════════════════════════════════
#
# Every major code change must pass three checks before any real run is
# allowed. Step 3 is the council bless above; steps 1 + 2 are recorded
# here as timestamped flags. The bless gate refuses approval unless both
# step-1 and step-2 timestamps exist AND are NEWER than the most recent
# `changed_at_iso` marker (which the agent sets via
# /api/preflight/code_changed whenever it edits the code in a
# load-bearing way).
#
# State is persisted to Setting key "preflight":
#   {
#     "static_overfit_at_iso": "...",   when step 1 last passed
#     "static_overfit_evidence": "...",
#     "static_overfit_final_loss": 0.0008,
#     "uniform_init_at_iso": "...",     when step 2 last passed
#     "uniform_init_evidence": "...",
#     "uniform_init_entropy": 6.905,
#     "changed_at_iso": "..."           bumped when code changes
#   }

_PREFLIGHT_STALE_HOURS = 24  # a preflight check older than this is stale


def _preflight_state_get() -> dict:
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(Setting.key == "preflight").first()
        if row and isinstance(row.value, dict):
            return dict(row.value)
        return {}
    finally:
        db.close()


def _preflight_state_set(state: dict) -> None:
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(Setting.key == "preflight").first()
        if row:
            row.value = state
        else:
            db.add(Setting(key="preflight", value=state))
        db.commit()
    finally:
        db.close()
    try:
        from .bus import bus
        bus.publish("events", "runs_changed", {})
    except Exception:
        pass


def _preflight_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _parse_iso(s):
    if not s:
        return None
    try:
        out = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        if out.tzinfo is None:
            out = out.replace(tzinfo=dt.timezone.utc)
        return out
    except Exception:
        return None


def _is_fresh(ts_iso, vs_iso) -> bool:
    """A preflight timestamp is fresh iff it exists, is no older than
    _PREFLIGHT_STALE_HOURS, AND is newer than the last `changed_at_iso`
    marker (if any)."""
    ts = _parse_iso(ts_iso)
    if ts is None:
        return False
    now = dt.datetime.now(dt.timezone.utc)
    age_hours = (now - ts).total_seconds() / 3600.0
    if age_hours > _PREFLIGHT_STALE_HOURS:
        return False
    changed = _parse_iso(vs_iso)
    if changed is not None and ts < changed:
        return False
    return True


def preflight_record_static_overfit(evidence: str, final_loss) -> dict:
    """Mark step 1 (static-batch overfit to ~0 train loss) as passed."""
    st = _preflight_state_get()
    st["static_overfit_at_iso"] = _preflight_now_iso()
    st["static_overfit_evidence"] = (evidence or "")[:1000]
    if final_loss is not None:
        try:
            st["static_overfit_final_loss"] = float(final_loss)
        except (TypeError, ValueError):
            pass
    _preflight_state_set(st)
    _emit_preflight_event(
        "preflight_static_overfit_passed",
        f"Pre-flight step 1 (static-batch overfit) recorded. "
        f"final_loss={st.get('static_overfit_final_loss', '?')}")
    return preflight_summary()


def preflight_record_uniform_init(evidence: str, entropy) -> dict:
    """Mark step 2 (uniform classification-head distribution at init)
    as passed."""
    st = _preflight_state_get()
    st["uniform_init_at_iso"] = _preflight_now_iso()
    st["uniform_init_evidence"] = (evidence or "")[:1000]
    if entropy is not None:
        try:
            st["uniform_init_entropy"] = float(entropy)
        except (TypeError, ValueError):
            pass
    _preflight_state_set(st)
    _emit_preflight_event(
        "preflight_uniform_init_passed",
        f"Pre-flight step 2 (uniform-init head) recorded. "
        f"entropy={st.get('uniform_init_entropy', '?')}")
    return preflight_summary()


def preflight_record_code_changed(reason: str) -> dict:
    """Bump the `changed_at_iso` marker. This invalidates any preflight
    timestamp recorded BEFORE now — the agent must re-run steps 1 + 2 +
    re-request bless before any real run can launch again."""
    st = _preflight_state_get()
    st["changed_at_iso"] = _preflight_now_iso()
    st["changed_reason"] = (reason or "")[:500]
    _preflight_state_set(st)
    # Also reset the bless state — the previous approval is now stale by
    # definition.
    cur = _bless_state_get()
    if cur.get("status") == "approved":
        _bless_state_set({"status": "not_requested",
                          "summary": ("Code changed — previous approval "
                                      "is stale. Re-run preflight + "
                                      "bless."),
                          "blockers": [], "suggestions": [],
                          "verdicts": {}})
    _emit_preflight_event(
        "preflight_code_changed",
        f"Significant code change recorded: "
        f"{reason or 'no reason given'}. Preflight + bless must be "
        "re-run.")
    return preflight_summary()


def preflight_summary() -> dict:
    """Snapshot of the 3-pill state for the dashboard banner + the bless
    gate."""
    st = _preflight_state_get()
    bless_st = _bless_state_get().get("status")
    changed = st.get("changed_at_iso")
    return {
        "static_overfit_passed":
            _is_fresh(st.get("static_overfit_at_iso"), changed),
        "uniform_init_passed":
            _is_fresh(st.get("uniform_init_at_iso"), changed),
        "blessed": bless_st == "approved",
        "static_overfit_at_iso": st.get("static_overfit_at_iso"),
        "uniform_init_at_iso": st.get("uniform_init_at_iso"),
        "changed_at_iso": changed,
        "static_overfit_final_loss": st.get("static_overfit_final_loss"),
        "uniform_init_entropy": st.get("uniform_init_entropy"),
        "static_overfit_evidence": st.get("static_overfit_evidence"),
        "uniform_init_evidence": st.get("uniform_init_evidence"),
        "stale_hours": _PREFLIGHT_STALE_HOURS,
    }


def preflight_blocking_reasons():
    """Human-readable list of which preflight steps are missing/stale.
    Empty list means steps 1 + 2 are both good — bless is allowed to
    proceed. Used by the bless gate."""
    s = preflight_summary()
    out = []
    if not s["static_overfit_passed"]:
        out.append(
            "Pre-flight step 1 (static-batch overfit) has not been "
            "recorded — train.py is unverified. POST "
            "/api/preflight/static_overfit with {evidence, final_loss} "
            "after you see ~0 train loss on a tiny static batch.")
    if not s["uniform_init_passed"]:
        out.append(
            "Pre-flight step 2 (uniform classification-head at init) "
            "has not been recorded — the architecture is unverified. "
            "POST /api/preflight/uniform_init with {evidence, entropy} "
            "after you confirm the head outputs ~1/num_classes per "
            "class at init.")
    return out


def _emit_preflight_event(ev_type: str, message: str) -> None:
    try:
        from .models import Event
        db = SessionLocal()
        try:
            ev = Event(id="ev-" + os.urandom(4).hex(),
                       type=ev_type, severity="info",
                       actor="preflight", message=message,
                       created_at=_preflight_now_iso())
            db.add(ev)
            db.commit()
            try:
                from .bus import bus
                bus.publish("events", "event", ev.dict())
            except Exception:
                pass
        finally:
            db.close()
    except Exception as e:                                  # noqa: BLE001
        print(f"[council/preflight] event-emit failed: {e}", flush=True)


def is_code_blessed() -> bool:
    """True iff the council has approved the current codebase — OR if
    there are no reviewers configured at all (Claude-only setups + the
    e2e test, neither of which can sensibly produce a verdict, get the
    gate auto-opened so they can launch runs).

    The Setting row's actual status is still authoritative once the user
    has configured reviewers and the agent has called /api/council/bless;
    this only short-circuits the 'no reviewers ever => infinite block'
    failure mode."""
    st = _bless_state_get().get("status")
    if st == "approved":
        return True
    # Defensive: a half-finished setup with no reviewers should not
    # permanently block /api/track/run. The dashboard banner still tells
    # the user no review happened.
    cfg = _settings()
    if not _available_reviewers(cfg):
        return True
    return False


def seed_task_blockers(meta: dict | None) -> list[str]:
    """Deterministic seed-task gate (RESEARCH_IMPROVEMENT_PLAN #7).

    ``meta`` carries the agent-reported metadata about its baseline
    that the council can't reliably extract from source alone. Recognised
    keys (all optional):
      - val_set_size: int — number of validation examples
      - dataset_kind: str — "smoke" means the agent EXPLICITLY accepts
        a small val set and the gate skips the size check
      - train_30s: bool — True means train.py finishes in < 30s on
        a single CPU (smoke-task signal)
      - program_md_marks_smoke: bool — convenience alias for dataset_kind

    Returns a list of human-readable blocker strings (empty if the gate
    passes). Surfaces nothing if no meta is supplied — the gate is opt-in
    via the agent's bless payload.
    """
    out: list[str] = []
    if not isinstance(meta, dict):
        return out
    smoke_marked = (
        str(meta.get("dataset_kind") or "").lower() == "smoke"
        or bool(meta.get("program_md_marks_smoke")))
    val_n = meta.get("val_set_size")
    try:
        val_n_int = int(val_n) if val_n is not None else None
    except (TypeError, ValueError):
        val_n_int = None
    if val_n_int is not None and val_n_int < 100 and not smoke_marked:
        out.append(
            f"[seed-task] validation set has {val_n_int} examples (< 100) "
            "and program.md does not mark dataset_kind: smoke — this is a "
            "smoke task, not a research baseline. Mark it as smoke in "
            "program.md OR use a real validation set with >= 100 examples.")
    if bool(meta.get("train_30s")):
        out.append(
            "[seed-task] train.py reportedly runs to completion in < 30s "
            "on a single CPU — this is a smoke test, not a research "
            "baseline. Build a real training pipeline before requesting "
            "bless.")
    return out


def _bless_worker(workspace: str, bless_meta: dict | None = None) -> None:
    cfg = _settings()
    reviewers = _available_reviewers(cfg)
    # ─── seed-task gate (RESEARCH_IMPROVEMENT_PLAN #7) ─────────────────
    # Deterministic check — runs BEFORE the LLM call so the agent gets
    # the exact reason without burning council tokens.
    seed_blockers = seed_task_blockers(bless_meta)
    if seed_blockers:
        _bless_state_set({
            "status": "rejected",
            "summary": ("Seed-task gate blocked the bless — agent must "
                        "scale the dataset / build a real training "
                        "pipeline first."),
            "blockers": seed_blockers,
            "suggestions": [
                "Mark program.md with `dataset_kind: smoke` if you "
                "deliberately want to evaluate on a tiny set.",
                "Otherwise scale val_set_size >= 100 examples and "
                "ensure train.py takes >= 30s on a single CPU.",
            ],
            "verdicts": {},
            "seed_task_gate": True,
        })
        return
    # If no reviewers are configured we cannot block forever; auto-approve
    # but record the verdict honestly so the dashboard says so.
    if not reviewers:
        _bless_state_set({
            "status": "approved",
            "summary": ("No council reviewers configured — auto-approved. "
                        "Add an OpenAI or Gemini key in Settings to enable "
                        "code review before training runs."),
            "blockers": [], "suggestions": [], "verdicts": {},
            "auto": True,
        })
        return
    code = _collect_codebase(workspace)
    if not code:
        _bless_state_set({
            "status": "rejected", "blockers": [
                "no source files found in the agent workspace — the agent "
                "has not scaffolded anything yet"],
            "suggestions": [], "verdicts": {}})
        return
    user_prompt = (
        "Review the codebase below. Respond with STRICT JSON per the "
        "schema in the system message.\n" + code)
    verdicts: dict[str, dict] = {}
    for reviewer in reviewers:
        v = _call_reviewer(reviewer, _BLESS_SYSTEM, user_prompt, cfg)
        if not v:
            verdicts[reviewer] = {"approved": None,
                                  "blockers": [], "suggestions": [],
                                  "summary":
                                  f"{reviewer}: call failed (token / quota?)"}
            continue
        verdicts[reviewer] = {
            "approved": bool(v.get("approved")),
            "blockers": list(v.get("blockers") or []),
            "suggestions": list(v.get("suggestions") or []),
            "summary": (v.get("summary") or "")[:280],
        }
    # Aggregate: ALL working reviewers must approve. Any blocker from any
    # reviewer is included in the blocker list shown to the agent.
    working = [r for r, v in verdicts.items() if v.get("approved") is not None]
    approved = bool(working) and all(verdicts[r]["approved"] for r in working)
    blockers: list[str] = []
    suggestions: list[str] = []
    for r, v in verdicts.items():
        for b in v.get("blockers", []):
            blockers.append(f"[{r}] {b}")
        for s in v.get("suggestions", []):
            suggestions.append(f"[{r}] {s}")
    summary = ("approved by " + ", ".join(working)
               if approved else
               f"{len(blockers)} blocker(s) raised — agent must fix")
    _bless_state_set({
        "status": "approved" if approved else "rejected",
        "summary": summary,
        "blockers": blockers,
        "suggestions": suggestions,
        "verdicts": verdicts,
    })
    # Emit an Event so it shows up in the Summary feed live.
    try:
        from .models import Event
        db = SessionLocal()
        try:
            ev = Event(id="ev-" + os.urandom(4).hex(),
                       type=("code_blessed" if approved
                             else "code_rejected"),
                       severity=("info" if approved else "warning"),
                       actor="council",
                       message=("Council approved the codebase — "
                                "training runs unlocked."
                                if approved else
                                f"Council rejected the codebase: "
                                f"{len(blockers)} blocker(s). "
                                "Agent must fix before any run can launch."),
                       created_at=dt.datetime.now(
                           dt.timezone.utc).isoformat())
            db.add(ev)
            db.commit()
            try:
                from .bus import bus
                bus.publish("events", "event", ev.dict())
            except Exception:
                pass
        finally:
            db.close()
    except Exception as e:                              # noqa: BLE001
        print(f"[council/bless] event-emit failed: {e}", flush=True)


def bless_async(workspace: str, bless_meta: dict | None = None) -> dict:
    """Kick off a fresh review of ``workspace``. Returns the state
    immediately (pending); the verdict is written later by the worker.

    PRE-FLIGHT GATE: refuses to even start a review if steps 1 + 2 of
    the SOP have not been recorded (or are stale). The bless verdict
    becomes meaningless if the agent hasn't proven train.py actually
    optimises and the architecture isn't broken at init, so we short-
    circuit before burning council tokens. The agent gets a clear
    `blocked_on_preflight` status + the missing-step blocker list and
    knows what to do.

    ``bless_meta`` carries agent-reported seed-task metadata
    (val_set_size, dataset_kind, train_30s) so the deterministic seed-
    task gate (RESEARCH_IMPROVEMENT_PLAN #7) can run before the LLM
    call. See :func:`seed_task_blockers` for the exact rules."""
    blockers = preflight_blocking_reasons()
    if blockers:
        _bless_state_set({
            "status": "blocked_on_preflight",
            "summary": ("Pre-flight SOP not complete — refusing to run "
                        "council bless until steps 1 + 2 pass."),
            "blockers": blockers,
            "suggestions": [
                "Run a tiny static-batch overfit to ~0 train loss, then "
                "POST /api/preflight/static_overfit.",
                "Verify the classification head is uniform at init, then "
                "POST /api/preflight/uniform_init.",
                "Then re-POST /api/council/bless.",
            ],
            "verdicts": {},
        })
        return bless_status()
    _bless_state_set({"status": "pending",
                      "summary": "Council is reviewing the codebase…",
                      "blockers": [], "suggestions": [], "verdicts": {}})
    threading.Thread(target=_bless_worker,
                     args=(workspace, bless_meta),
                     daemon=True, name="council-bless").start()
    return bless_status()


# ════════════════════════════════════════════════════════════════════════
#  Research conclusion review (agent declares "the purpose is answered")
# ════════════════════════════════════════════════════════════════════════
#
# Why this exists
# ---------------
# The agent is forbidden from sitting idle. When its directive queue is
# empty it has exactly two legal moves:
#   (B) propose a new SCIENCE directive, OR
#   (C) declare the research purpose conclusively answered and ask the
#       council to bless that conclusion.
#
# This block implements (C). The agent POSTs /api/research/conclude with
# a 1-paragraph summary + the run_ids that support its claim. We persist
# the conclusion as a Setting row ``research_conclusion`` with status
# ``pending``, fire an async council review, and the stuck_detector then
# surfaces the state as ``awaiting_completion_review`` (purple/indigo).
# When the council returns we set status=approved or rejected; the
# dashboard then surfaces "complete" (green/trophy) or kicks the agent
# to address the missing evidence the council called out.
#
# State lives in Setting key ``research_conclusion``:
#   {
#     "status": "pending|approved|rejected",
#     "summary": "<agent's 1-paragraph finding>",
#     "answer_to_purpose": "YES_CONCLUSIVELY|YES_PARTIAL|NO",
#     "evidence": ["run_id_1", ...],
#     "recommendation": "WRITE_PAPER|NEED_ORTHOGONAL_DIRECTION|NEED_MORE_DATA",
#     "conclude_at": "<iso>",
#     "council_verdict": {                      # set after review
#         "verdict": "APPROVED|REJECTED|NEEDS_MORE",
#         "reasons": [...],
#         "missing_evidence": [...],
#         "summary": "<one-paragraph council judgment>",
#         "verdicts": {reviewer: {...}},
#         "reviewed_at": "<iso>",
#     },
#   }


_RESEARCH_CONCLUSION_KEY = "research_conclusion"


_COMPLETION_REVIEW_SYSTEM = (
    "You are a senior research advisor on a council reviewing whether "
    "an autonomous ML research agent's conclusion that the project "
    "purpose has been answered is BACKED BY THE EVIDENCE.\n\n"
    "You will be given:\n"
    "  - the project Purpose (what question was the agent trying to answer?)\n"
    "  - the agent's one-paragraph summary of what was learned\n"
    "  - the agent's claim level: YES_CONCLUSIVELY, YES_PARTIAL, or NO\n"
    "  - the agent's recommendation: WRITE_PAPER / NEED_ORTHOGONAL_DIRECTION / NEED_MORE_DATA\n"
    "  - the run ids the agent cites as evidence (with metrics + status)\n"
    "  - the project-wide best metric and baseline\n"
    "  - all prior runs as one-liners (so you can sanity-check the claim)\n\n"
    "Decide:\n"
    "  - APPROVED — the evidence really does answer the Purpose. The user\n"
    "    can move to paper mode. Be STRICT: at minimum the cited runs\n"
    "    must (a) actually exist, (b) be 'kept' or 'success' status,\n"
    "    (c) directly speak to the Purpose, (d) be statistically credible\n"
    "    (multiple seeds OR a single decisive non-trivial gap). For NO\n"
    "    claims the bar is 'agent tried multiple orthogonal approaches\n"
    "    and they failed' — APPROVE only if that's actually the case.\n"
    "  - REJECTED — the evidence does NOT support the conclusion. The\n"
    "    research must continue. Give the agent concrete `missing_evidence`\n"
    "    items it should produce next (these will be turned into new\n"
    "    SCIENCE directives by the orchestrator).\n"
    "  - NEEDS_MORE — directionally right but more confirmation runs are\n"
    "    needed before paper. Same `missing_evidence` shape applies.\n\n"
    "Return STRICT JSON only — no markdown fence — matching:\n"
    "  {\n"
    "    \"verdict\": \"APPROVED\" | \"REJECTED\" | \"NEEDS_MORE\",\n"
    "    \"reasons\": [\"<one-line rationale>\", ...],\n"
    "    \"missing_evidence\": [\"<concrete experiment the agent should run next>\", ...],\n"
    "    \"summary\": \"<one-paragraph judgment for the dashboard>\"\n"
    "  }\n"
    "Be terse. Be specific. The agent will read your `missing_evidence`\n"
    "literally and turn each item into a SCIENCE directive — write them\n"
    "as concrete experiments, not vague platitudes."
)


def _conclusion_state_get() -> dict:
    db = SessionLocal()
    try:
        row = (db.query(Setting)
               .filter(Setting.key == _RESEARCH_CONCLUSION_KEY).first())
        if row and isinstance(row.value, dict):
            return dict(row.value)
        return {"status": "none"}
    finally:
        db.close()


def _conclusion_state_set(state: dict) -> None:
    state = dict(state)
    state.setdefault("updated_at",
                     dt.datetime.now(dt.timezone.utc).isoformat())
    db = SessionLocal()
    try:
        row = (db.query(Setting)
               .filter(Setting.key == _RESEARCH_CONCLUSION_KEY).first())
        if row:
            row.value = state
        else:
            db.add(Setting(key=_RESEARCH_CONCLUSION_KEY, value=state))
        db.commit()
    finally:
        db.close()
    try:
        from .bus import bus
        bus.publish("events", "research_health", {"state":
                                                    state.get("status")})
    except Exception:
        pass


def conclusion_state() -> dict:
    """Snapshot of the current research-conclusion state (dashboard read).

    Returns the raw setting row plus a normalised ``status`` of
    ``none|pending|approved|rejected``. ``none`` means the agent hasn't
    declared completion (or the conclusion was cleared by the operator).
    """
    s = _conclusion_state_get()
    # Defensive normalisation — older rows / partial writes shouldn't
    # crash the dashboard pill.
    if not isinstance(s, dict):
        return {"status": "none"}
    s.setdefault("status", "none")
    return s


def clear_conclusion(reason: str = "") -> dict:
    """Wipe the current conclusion (used when the operator rejects it).

    Leaves a small audit Event so the timeline shows the rejection.
    Returns the cleared state."""
    prev = _conclusion_state_get()
    _conclusion_state_set({"status": "none",
                           "cleared_at":
                               dt.datetime.now(dt.timezone.utc).isoformat(),
                           "cleared_reason": (reason or "")[:500],
                           "previous": prev if prev.get("status") != "none"
                                       else None})
    try:
        db = SessionLocal()
        try:
            db.add(Event(
                id="ev-" + os.urandom(4).hex(),
                type="research_conclusion_cleared",
                severity="info", actor="user",
                message=(f"Research conclusion cleared by operator. "
                         f"Reason: {reason or '(no reason given)'}")[:280],
                created_at=dt.datetime.now(dt.timezone.utc).isoformat()))
            db.commit()
        finally:
            db.close()
    except Exception as e:                                  # noqa: BLE001
        print(f"[council/conclude] clear event-emit failed: {e}",
              flush=True)
    return conclusion_state()


def _build_completion_review_context(evidence_run_ids: list[str],
                                       summary: str,
                                       answer_to_purpose: str,
                                       recommendation: str) -> dict | None:
    db = SessionLocal()
    try:
        proj = db.query(Project).first()
        if not proj:
            return None
        every_run = db.query(Run).all()
        # Pull the explicitly-cited evidence runs
        ev_runs = [db.query(Run).filter(Run.id == rid).first()
                   for rid in evidence_run_ids]
        ev_runs = [r for r in ev_runs if r is not None]
        ev_block = [{
            "id": r.id, "name": r.run_name, "status": r.status,
            "headline_metric": r.headline_metric,
            "baseline_delta": r.baseline_delta,
            "config": r.config if isinstance(r.config, dict) else {},
        } for r in ev_runs]
        # All runs as compact one-liners (frontier-movers first, crashed
        # next) so the reviewer can sanity-check the claim.
        frontier = _frontier_ids(every_run)
        maximize = proj.metric_direction == "maximize"

        def _key(r):
            on_front = r.id in frontier
            is_crash = (r.status == "crashed")
            return (0 if on_front else (1 if is_crash else 2),
                    r.created_at or "")
        others = sorted(every_run, key=_key)
        run_lines = [_compact_one_line(r, frontier, maximize)
                     for r in others]
        run_lines_text = "\n".join(run_lines)[:18000]
        stats = _aggregate_stats(every_run, proj)
        return {
            "project": {
                "name": proj.name, "purpose": proj.purpose,
                "metric": proj.validation_metric,
                "direction": proj.metric_direction,
                "baseline_metric": getattr(proj, "baseline_metric", None),
            },
            "agent_claim": {
                "summary": summary,
                "answer_to_purpose": answer_to_purpose,
                "recommendation": recommendation,
                "n_evidence_runs": len(ev_block),
            },
            "evidence_runs": ev_block,
            "aggregate": stats,
            "all_prior_runs_count": len(every_run),
            "all_prior_runs_oneliners": run_lines_text,
        }
    finally:
        db.close()


def _completion_review_worker(evidence_run_ids: list[str], summary: str,
                                answer_to_purpose: str,
                                recommendation: str) -> None:
    cfg = _settings()
    reviewers = _available_reviewers(cfg)
    if not reviewers:
        # No reviewers — auto-approve so the agent isn't blocked, but
        # record it honestly so the dashboard tells the user nothing
        # actually got reviewed.
        existing = _conclusion_state_get()
        existing.update({
            "status": "approved",
            "council_verdict": {
                "verdict": "APPROVED",
                "reasons": ["no council reviewers configured — auto-approved"],
                "missing_evidence": [],
                "summary": ("No reviewers configured; conclusion was "
                            "auto-approved. Add an OpenAI or Gemini key "
                            "to enable real completion review."),
                "verdicts": {},
                "auto": True,
                "reviewed_at":
                    dt.datetime.now(dt.timezone.utc).isoformat(),
            },
        })
        _conclusion_state_set(existing)
        _emit_completion_event(
            "research_completion_auto_approved",
            "Research conclusion auto-approved (no reviewers).")
        return
    ctx = _build_completion_review_context(
        evidence_run_ids, summary, answer_to_purpose, recommendation)
    if ctx is None:
        existing = _conclusion_state_get()
        existing.update({
            "status": "rejected",
            "council_verdict": {
                "verdict": "REJECTED",
                "reasons": ["no project — completion review aborted"],
                "missing_evidence": [],
                "summary": "Could not load the project; conclusion rejected.",
                "verdicts": {},
                "reviewed_at":
                    dt.datetime.now(dt.timezone.utc).isoformat(),
            },
        })
        _conclusion_state_set(existing)
        return
    user_prompt = (
        "Review the agent's claim that the project Purpose has been "
        "answered. Return STRICT JSON per the schema in the system "
        "message.\n\n"
        + json.dumps(ctx, indent=2, default=str))[:60000]
    verdicts: dict[str, dict] = {}
    for reviewer in reviewers:
        v = _call_reviewer(reviewer, _COMPLETION_REVIEW_SYSTEM,
                            user_prompt, cfg)
        if not v:
            verdicts[reviewer] = {"verdict": None,
                                  "reasons": [],
                                  "missing_evidence": [],
                                  "summary":
                                  f"{reviewer}: call failed (token / quota?)"}
            continue
        verdicts[reviewer] = {
            "verdict": str(v.get("verdict") or "").upper(),
            "reasons": list(v.get("reasons") or []),
            "missing_evidence": list(v.get("missing_evidence") or []),
            "summary": (v.get("summary") or "")[:600],
        }
    # Aggregate: ALL working reviewers must say APPROVED. Otherwise it's
    # either REJECTED or NEEDS_MORE (worst wins).
    working = [r for r, v in verdicts.items()
               if v.get("verdict") in ("APPROVED", "REJECTED", "NEEDS_MORE")]
    if not working:
        final_verdict = "REJECTED"
    elif all(verdicts[r]["verdict"] == "APPROVED" for r in working):
        final_verdict = "APPROVED"
    elif any(verdicts[r]["verdict"] == "REJECTED" for r in working):
        final_verdict = "REJECTED"
    else:
        final_verdict = "NEEDS_MORE"
    reasons_agg: list[str] = []
    missing_agg: list[str] = []
    summaries: list[str] = []
    for r, v in verdicts.items():
        for x in v.get("reasons", []):
            reasons_agg.append(f"[{r}] {x}")
        for x in v.get("missing_evidence", []):
            missing_agg.append(f"[{r}] {x}")
        if v.get("summary"):
            summaries.append(f"[{r}] {v.get('summary')}")
    status_out = ("approved" if final_verdict == "APPROVED"
                  else "rejected")
    existing = _conclusion_state_get()
    existing.update({
        "status": status_out,
        "council_verdict": {
            "verdict": final_verdict,
            "reasons": reasons_agg,
            "missing_evidence": missing_agg,
            "summary": "\n".join(summaries)[:2000],
            "verdicts": verdicts,
            "reviewed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        },
    })
    _conclusion_state_set(existing)
    # When the council REJECTED or said NEEDS_MORE we want the agent to
    # immediately turn the missing_evidence into directives so the loop
    # restarts itself without the operator. We don't auto-upsert (that
    # would step on the agent's planning); instead we emit a Chat bubble
    # + Event with the list and the strategic_review pipeline catches it.
    sev = "info" if final_verdict == "APPROVED" else "warning"
    msg = ("Research conclusion APPROVED — write the paper."
           if final_verdict == "APPROVED" else
           f"Research conclusion {final_verdict} — "
           f"{len(missing_agg)} missing-evidence item(s) returned.")
    _emit_completion_event(
        "research_completion_reviewed", msg, severity=sev)


def _emit_completion_event(ev_type: str, message: str,
                             severity: str = "info") -> None:
    """Persist an Event + ChatMessage for the completion-review flow."""
    try:
        db = SessionLocal()
        try:
            ts = dt.datetime.now(dt.timezone.utc).isoformat()
            db.add(Event(id="ev-" + os.urandom(4).hex(),
                         type=ev_type, severity=severity, actor="council",
                         message=message[:280], created_at=ts))
            db.add(ChatMessage(
                id="cm-" + os.urandom(4).hex(),
                role="agent",
                content=("[completion review] " + message)[:1200],
                created_at=ts))
            db.commit()
        finally:
            db.close()
        try:
            from .bus import bus
            bus.publish("events", "research_health", {})
        except Exception:
            pass
    except Exception as e:                                  # noqa: BLE001
        print(f"[council/conclude] event-emit failed: {e}", flush=True)


def review_completion_async(evidence_run_ids: list[str],
                              summary: str,
                              answer_to_purpose: str,
                              recommendation: str = "") -> dict:
    """Kick off an async council review of the agent's conclusion.

    Sets ``research_conclusion`` to ``pending`` immediately and spawns a
    background worker thread. The worker writes the verdict back into
    the same Setting row when it's done."""
    # Persist the agent's claim BEFORE we run the review — the dashboard
    # shows it immediately so the user can see what the agent is
    # asserting while the council deliberates.
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    _conclusion_state_set({
        "status": "pending",
        "summary": (summary or "")[:4000],
        "answer_to_purpose": answer_to_purpose or "",
        "evidence": list(evidence_run_ids or [])[:200],
        "recommendation": recommendation or "",
        "conclude_at": now,
    })
    threading.Thread(target=_completion_review_worker,
                     args=(list(evidence_run_ids or []),
                           summary or "",
                           answer_to_purpose or "",
                           recommendation or ""),
                     daemon=True, name="council-completion").start()
    return conclusion_state()


# ════════════════════════════════════════════════════════════════════════
#   Proactive "propose next move" trigger (called from pi.cycle when
#   the system has been ``needs_direction`` for too long)
# ════════════════════════════════════════════════════════════════════════


def propose_next_move_async() -> dict:
    """Kick off a strategic council call whose ONE job is to either
    (a) upsert the next SCIENCE directive, OR (b) flag that the agent
    should call /api/research/conclude with NEED_ORTHOGONAL_DIRECTION.

    Reuses ``strategic_review`` over the most recent N runs (or all of
    them if there are fewer). Returns {"started": bool, "reason": str}
    immediately; the actual upsert happens inside ``_strategic_worker``.
    """
    global _BATCH_INFLIGHT
    cfg = _settings()
    if not _available_reviewers(cfg):
        return {"started": False, "reason": "no_reviewers"}
    db = SessionLocal()
    try:
        n = max(_strategic_threshold(cfg), 1)
        runs = (db.query(Run)
                .filter(Run.status.in_(["kept", "discarded", "crashed",
                                        "failed", "success", "kept_novel",
                                        "kept_replicate"]))
                .order_by(Run.ended_at.desc())
                .limit(n).all())
        ids = [r.id for r in runs]
    finally:
        db.close()
    with _BATCH_LOCK:
        if _BATCH_INFLIGHT:
            return {"started": False, "reason": "already_inflight"}
        _BATCH_INFLIGHT = True
    threading.Thread(target=_strategic_worker, args=(ids,), daemon=True,
                     name="council-propose-next").start()
    return {"started": True, "reason": "ok",
            "n_runs": len(ids)}

