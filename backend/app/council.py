"""LLM council — post-experiment deliberation.

After every run finishes (kept / discarded / crashed) the monitor enqueues a
review here. We round-robin through whichever external models the user has
keys for (Gemini 3 Pro, GPT 5.5 high, Claude Opus 4.7) and ask one of them to:

  1. Say what this experiment taught us (in 2-4 sentences).
  2. Rerank the pending rows in ideas.md (best-EV idea first).
  3. Propose 0-3 NEW high-value ideas (NOT HP tuning, NOT seed variations).
  4. Veto any pending ideas that look like a waste of GPU time.

The output is persisted onto the Run (config["review"]) so the Summary rail
can render it, and the rerank/new ideas are written atomically into the
project's ideas.md so the agent picks them up on its next planning loop.

Keys come from .deploy/keys.env (gitignored). If no keys are present, the
module is a no-op — the rest of the system still works.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

from .config import DATA_DIR, ROOT
from .db import SessionLocal
from .models import Event, Idea, Project, Run, Setting

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


# ── reviewer roster ──────────────────────────────────────────────────────
def _available_reviewers() -> list[str]:
    rs = []
    if os.environ.get("GEMINI_API_KEY"):
        rs.append("gemini")
    if os.environ.get("OPENAI_API_KEY"):
        rs.append("openai")
    if os.environ.get("ANTHROPIC_API_KEY"):
        rs.append("claude")
    return rs


_RR = 0
_RR_LOCK = threading.Lock()
_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT: set[str] = set()


def is_enabled() -> bool:
    return bool(_available_reviewers())


def _pick_reviewer() -> str | None:
    global _RR
    rs = _available_reviewers()
    if not rs:
        return None
    with _RR_LOCK:
        choice = rs[_RR % len(rs)]
        _RR += 1
    return choice


# ── the prompt ────────────────────────────────────────────────────────────
SYSTEM = """You are the senior scientific advisor on an autonomous ML research
project. An autonomous agent just finished one experiment. Your job is to
decide what the agent should do next so the next experiments are HIGH-VALUE.

HARD RULES — violating these is a failure:
- DO NOT recommend hyperparameter sweeps (lr / batch_size / weight_decay /
  warmup grids) unless THIS run was clearly bottlenecked by an HP choice
  (training diverged early, gradients exploded, schedule was obviously
  miscalibrated). Boring HP grids are NOT research.
- DO NOT recommend rerunning anything with a different random seed unless
  the reported variance across existing runs is large enough that the result
  is genuinely uncertain.
- Each new idea must change ONE substantive thing — architecture, objective,
  data regime, training procedure, evaluation, or a clearly motivated HP at
  the edge of stability — with a one-line hypothesis WHY it might help.
- Prefer experiments that, if they work, MOVE THE FRONTIER. Reject your own
  safe / incremental ideas before you submit them.
- Penalise repetition: if the recent runs already explored a direction
  exhaustively, DROP that direction from the queue (veto it).

You return JSON ONLY, no prose around it, no markdown fence, matching:
{
  "verdict": "kept" | "discarded" | "crashed" | "inconclusive",
  "learning": "<2-4 sentences in plain English: what this run taught us>",
  "rerank_pending": ["<idea_id_best_next>", "<idea_id_2nd>", ...],
  "new_ideas": [
    {"idea_id": "<snake_case_id>", "what": "<one line, concrete>",
     "why": "<one line: a falsifiable hypothesis>"}
  ],
  "veto": ["<idea_id_to_drop>", ...]
}

- rerank_pending: ONLY existing pending idea_ids from the input, in the
  order you want them tried (best first). Omit ones you'd skip — put them in
  veto.
- new_ideas: 0-3 entries. Quality over quantity. snake_case ids only.
- learning: explain what the data shows, not what the agent did. Be specific
  about the metric and direction."""


# ── context bundle ────────────────────────────────────────────────────────
def _frontier(runs):
    """Return ids of the running frontier (each run that beat all earlier
    kept runs on the project metric). Used to give the reviewer a sense of
    progress."""
    out = []
    best = None
    for r in sorted(runs, key=lambda r: r.created_at or ""):
        if r.headline_metric is None:
            continue
        if best is None or r.headline_metric < best:
            best = r.headline_metric
            out.append(r.id)
    return out


def _build_context(run_id: str) -> dict | None:
    db = SessionLocal()
    try:
        proj = db.query(Project).first()
        run = db.query(Run).filter(Run.id == run_id).first()
        if not (proj and run):
            return None
        all_runs = db.query(Run).order_by(Run.created_at.desc()).limit(40).all()
        frontier = set(_frontier(db.query(Run).all()))
        ideas = db.query(Idea).filter(Idea.id.like("deck-%")).all()

        def _cfg(r):
            c = r.config if isinstance(r.config, dict) else {}
            return {k: c.get(k) for k in ("what", "why") if c.get(k)}

        return {
            "project": {
                "name": proj.name,
                "purpose": proj.purpose,
                "metric": proj.validation_metric,
                "direction": proj.metric_direction,
            },
            "this_run": {
                "id": run.id,
                "name": run.run_name,
                "status": run.status,
                "headline_metric": run.headline_metric,
                "baseline_delta": run.baseline_delta,
                "config": run.config if isinstance(run.config, dict) else {},
            },
            "recent_runs": [
                {
                    "id": r.id,
                    "status": r.status,
                    "metric": r.headline_metric,
                    "on_frontier": r.id in frontier,
                    "config": _cfg(r),
                }
                for r in all_runs if r.id != run.id
            ][:18],
            # Cap pending list sent to the model: the council should focus on
            # the top of the queue, not deliberate over 200 stale ideas.
            "pending_ideas": [
                {"idea_id": i.idea_id, "what": i.description}
                for i in ideas
            ][:30],
            "pending_total_count": len(ideas),
        }
    finally:
        db.close()


# ── model adapters (stdlib only) ──────────────────────────────────────────
# Reasoning models (gpt-5 high, o3-pro, gemini-2.5-pro) can take 30-90s, so
# we give the council a generous timeout. It runs in a background thread so
# this never blocks the dashboard.
_TIMEOUT = 240


def _post_json(url: str, body: dict, headers: dict) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", errors="ignore"))


def _call_gemini(system: str, user: str) -> str:
    key = os.environ["GEMINI_API_KEY"]
    model = os.environ.get("ARUI_COUNCIL_GEMINI_MODEL", "gemini-2.5-pro")
    url = ("https://generativelanguage.googleapis.com/v1beta/"
           f"models/{model}:generateContent?key={key}")
    body = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {"responseMimeType": "application/json",
                             "temperature": 0.7},
    }
    data = _post_json(url, body, {})
    return data["candidates"][0]["content"]["parts"][0]["text"]


def _call_openai(system: str, user: str) -> str:
    key = os.environ["OPENAI_API_KEY"]
    # gpt-5 on chat/completions with reasoning_effort=high is the closest
    # match to the user's "GPT 5.5 high"; gpt-5-pro requires the Responses
    # API and many keys don't have access to it.
    model = os.environ.get("ARUI_COUNCIL_OPENAI_MODEL", "gpt-5")
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "response_format": {"type": "json_object"},
        "reasoning_effort": os.environ.get("ARUI_COUNCIL_OPENAI_EFFORT",
                                            "high"),
    }
    data = _post_json("https://api.openai.com/v1/chat/completions", body,
                      {"Authorization": f"Bearer {key}"})
    return data["choices"][0]["message"]["content"]


def _call_claude(system: str, user: str) -> str:
    key = os.environ["ANTHROPIC_API_KEY"]
    body = {
        "model": os.environ.get("ARUI_COUNCIL_CLAUDE_MODEL", "claude-opus-4-6"),
        "max_tokens": 1500,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    data = _post_json("https://api.anthropic.com/v1/messages", body,
                      {"x-api-key": key, "anthropic-version": "2023-06-01"})
    return data["content"][0]["text"]


_CALLERS = {"gemini": _call_gemini, "openai": _call_openai,
            "claude": _call_claude}


# ── parse the model's response ────────────────────────────────────────────
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


# ── entry points ──────────────────────────────────────────────────────────
def deliberate(run_id: str) -> dict | None:
    """Call one council member and return its parsed review."""
    reviewer = _pick_reviewer()
    if not reviewer:
        return None
    ctx = _build_context(run_id)
    if not ctx:
        return None
    total_pending = ctx.get("pending_total_count", 0)
    queue_note = ""
    if total_pending > 40:
        queue_note = (f"\n\nNOTE: the project's pending queue already has "
                      f"{total_pending} ideas (you're only shown the top 30). "
                      f"Focus your effort on RERANK / VETO — only propose "
                      f"new_ideas if they would CLEARLY replace several of "
                      f"the existing ones. Quality > quantity. It is fine to "
                      f"return new_ideas: [].")
    user = ("Here is the current state of an autonomous ML research project. "
            "Review the most recent run and return JSON per the schema in the "
            "system prompt." + queue_note + "\n\n"
            + json.dumps(ctx, indent=2, default=str))
    try:
        text = _CALLERS[reviewer](SYSTEM, user)
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
        print(f"[council] {reviewer} returned non-JSON; first 200 chars: "
              f"{text[:200]!r}", flush=True)
        return None
    out["reviewer"] = reviewer
    out["reviewed_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    return out


def review_async(run_id: str) -> bool:
    """Fire-and-forget background review. Idempotent per run."""
    if not is_enabled():
        return False
    with _INFLIGHT_LOCK:
        if run_id in _INFLIGHT:
            return False
        _INFLIGHT.add(run_id)
    threading.Thread(target=_worker, args=(run_id,), daemon=True,
                     name=f"council-{run_id[:16]}").start()
    return True


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
    """Stash the review on the run's config so /api/runs returns it, mirror
    the learning onto its Idea, and emit a feed event."""
    db = SessionLocal()
    try:
        run = db.query(Run).filter(Run.id == run_id).first()
        if not run:
            return
        cfg = dict(run.config) if isinstance(run.config, dict) else {}
        cfg["review"] = review
        run.config = cfg
        if run.idea_id:
            idea = db.query(Idea).filter(Idea.id == run.idea_id).first()
            if idea and (review.get("learning") or "").strip():
                idea.conclusion = review["learning"].strip()
        db.add(Event(
            id="ev-" + os.urandom(4).hex(),
            type="council_reviewed", severity="info",
            actor="council:" + (review.get("reviewer") or "?"),
            run_id=run.id,
            message=(review.get("learning") or "")[:280] or
                    f"Council ({review.get('reviewer')}) reviewed {run.run_name}",
            created_at=_iso()))
        db.commit()
    finally:
        db.close()
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
    """Rewrite the pending block of ideas.md per the council's rerank, append
    new_ideas, and veto vetoed ones. Atomic write (write tmp + rename)."""
    name = _onboarding_repo_name()
    if not name:
        return
    path = DATA_DIR / "workspace" / name / "ideas.md"
    if not path.exists():
        return
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
    # Skip the separator row (|---|---|...) if present.
    body_start = hdr + 1
    if (body_start < len(lines)
            and re.fullmatch(r"\|[\s\-\|:]+", lines[body_start].strip() or "|")):
        body_start += 1
    # The table runs until the first non-pipe line.
    body_end = body_start
    while body_end < len(lines) and lines[body_end].lstrip().startswith("|"):
        body_end += 1

    pending: list[tuple[str, list[str]]] = []   # (idea_id, cells incl. status)
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

    # New ideas — dedup, fuzzy-match against existing pending, and only add
    # them if the queue is shallow enough to need fresh proposals. Otherwise
    # the council should rerank/prune, not pile on.
    PENDING_HEALTHY = 30          # if the queue is fuller than this, no adds
    existing_ids = {p[0] for p in new_pending}
    existing_whats = {(p[1][2] if len(p[1]) > 2 else "").strip().lower()
                      for p in [(c[0], c) for c in new_pending]}
    arity = max((len(p[1]) for p in pending), default=4)
    if len(new_pending) < PENDING_HEALTHY:
        room = PENDING_HEALTHY - len(new_pending)
        for ni in (review.get("new_ideas") or [])[:min(3, room)]:
            if not isinstance(ni, dict):
                continue
            idea_id = re.sub(r"[^A-Za-z0-9_]+", "_",
                             str(ni.get("idea_id") or "")).strip("_")
            if not idea_id or idea_id in existing_ids:
                continue                # exact dedup
            what = str(ni.get("what") or "").strip()
            wlo = what.lower()
            # fuzzy dedup: skip if a very similar "what" line already exists
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

    # Render
    out_rows = list(done_rows)
    out_rows.extend("| " + " | ".join(cells) + " |" for cells in new_pending)
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
        print(f"[council] rewrote {path} — {len(new_pending)} pending rows, "
              f"{len(veto)} vetoed", flush=True)
    except Exception as e:                              # noqa: BLE001
        print(f"[council] could not rewrite ideas.md: {e}", flush=True)
