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

You return JSON ONLY, no prose around it, no markdown fence, matching:
{
  "verdict": "kept" | "discarded" | "crashed" | "inconclusive",
  "learning": "<2-4 sentences: what this run taught us and what the data
    suggests next. If recommending a pivot, say so clearly.>",
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
- learning: explain what the data shows, not what the agent did. Be
  specific about the metric and direction."""


DEBATE_SYSTEM = SYSTEM + """

DEBATE MODE: another expert reviewer also looked at this run. Their position
will be appended to the user message. Read it carefully. If you AGREE with
their points, adopt their framing (your output should match theirs on
verdict + top-3 rerank + veto set). If you DISAGREE, restate your position
but ALSO address their argument specifically in the "learning" field — say
why their take is wrong or incomplete. The goal is to converge on the
strongest call, not to be stubborn. Keep returning the same JSON schema."""


TIEBREAKER_SYSTEM = """You are the tiebreaker on a senior advisory panel for
an autonomous ML research project. Two expert reviewers disagree after 3
rounds of debate. Read both of their final positions and the run context,
then make the final call. Return the same JSON schema as the reviewers
(verdict, learning, rerank_pending, new_ideas, veto). In "learning", briefly
explain which reviewer you sided with and why. Be decisive — no hedging."""


STRATEGIC_SYSTEM = """You are the senior scientific advisor on an autonomous
ML research project. The agent has just finished a BATCH of N parallel
experiments (one per GPU). Your job is not to micro-review each one but
to step back and decide what the AGENT SHOULD DO NEXT — a strategic call
on the whole project trajectory, based on this batch + all prior history.

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

JSON ONLY, no markdown, schema:
{
  "verdict": "progress" | "stagnant" | "regressing",
  "learning": "<3-6 sentences: state of the project, what this batch
    showed, and what you recommend next (concrete HPs / direction).
    If recommending a pivot or abandoning a direction, say so.>",
  "rerank_pending": ["<idea_id_best_next>", ...],
  "new_ideas": [
    {"idea_id": "<snake_case>", "what": "<concrete with HP values>",
     "why": "<falsifiable hypothesis>"}
  ],
  "veto": ["<idea_id_to_drop>", ...],
  "pivot": {"recommend": true|false, "to_what": "<one-line direction>"}
}

- rerank_pending: ONLY existing pending idea_ids, best first.
- new_ideas: 0-5 entries. Concrete HP values where relevant.
- pivot: only set recommend=true if the data strongly supports it (>100
  flat runs, or this project's hypothesis is materially contradicted)."""


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


def _append_lesson(reviewer: str, run_name: str, learning: str) -> None:
    """Append the council's takeaway from this run to lessons.md, dedup'd
    against the previous N entries so identical / near-identical lessons
    don't clutter the file."""
    p = _lessons_path()
    if not p or not learning or not learning.strip():
        return
    ll = learning.strip()
    if len(ll) < 12:
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
def _call_reviewer(reviewer: str, system: str, user: str, cfg: dict
                   ) -> dict | None:
    try:
        text = _CALLERS[reviewer](system, user, cfg)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="ignore")[:300]
        except Exception:
            pass
        print(f"[council] {reviewer} HTTP {e.code}: {body}", flush=True)
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
            # apply rerank/new ideas to ideas.md, and write learning to
            # lessons.md.
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


def _strategic_ctx_text(ctx: dict) -> str:
    runs_block = ctx.pop("all_prior_runs_oneliners", "") or "(none)"
    lessons_block = ctx.pop("lessons_so_far", "").strip() or "(none yet)"
    return (
        "You are doing a STRATEGIC review of a BATCH of recent runs (one "
        "wave of parallel experiments). Step back: look at the WHOLE "
        "project trajectory, this batch's results, and recommend the next "
        "research move. Return JSON per schema.\n\n"
        "=== LESSONS LEARNED SO FAR (your prior reviews) ===\n"
        + lessons_block + "\n\n"
        f"=== ALL PRIOR RUNS ({ctx.get('all_prior_runs_count', 0)} total — "
        "frontier-movers ★ first, then crashed) ===\n"
        "status    metric   run_id                              what\n"
        + runs_block + "\n\n"
        "=== STRUCTURED CONTEXT (project / this batch / aggregate / pending)"
        " ===\n" + json.dumps(ctx, indent=2, default=str))


def strategic_review(batch_run_ids: list[str]) -> dict | None:
    """Run ONE expert through the strategic-review prompt over a batch.
    Strategic reviews don't debate — they're already a 'reflection' call
    that asks the model to look at the whole trajectory at once. We pick
    the highest-quality reviewer available (claude > openai > gemini)
    since the call only fires every N runs and we want the best judgment."""
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
    user = _strategic_ctx_text(ctx)
    print(f"[council/strategic] running {reviewer} over batch of "
          f"{len(batch_run_ids)} runs", flush=True)
    out = _call_reviewer(reviewer, STRATEGIC_SYSTEM, user, cfg)
    if not out:
        return None
    out["scope"] = "strategic"
    out["batch_run_ids"] = batch_run_ids
    out["reviewer"] = reviewer + " (strategic)"
    return out


def _persist_strategic(batch_run_ids: list[str], review: dict) -> None:
    """Persist a strategic review: write a ChatMessage + Event so it's
    visible in the Summary feed, append the learning to lessons.md, and
    mark each batch run's config['strategic_review_id']."""
    db = SessionLocal()
    learning = (review.get("learning") or "").strip()
    pivot = review.get("pivot") or {}
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
        if pivot.get("recommend"):
            msg = (f"[Strategic review — RECOMMENDING PIVOT to: "
                   f"{pivot.get('to_what', '?')}]  ") + (learning or "")
        db.add(ChatMessage(id="cm-" + os.urandom(4).hex(),
                           role="agent", content=msg, created_at=_iso()))
        db.add(Event(id="ev-" + os.urandom(4).hex(),
                     type="strategic_review", severity="info",
                     actor="council:" + (review.get("reviewer") or "?"),
                     message=learning[:280] or "Strategic review completed",
                     created_at=_iso()))
        db.commit()
    finally:
        db.close()
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
