"""Scoping gate — Phase 0, BEFORE the research agent spends any GPU.

Flow (grounded in the existing architecture, no new lexicon):

  onboarding submit
    -> scoping.start(cfg)                 (instead of realrun.start_real)
       - Lit Agent (lit_agent.discover_for_purpose) sweeps arxiv +
         Semantic Scholar off the PURPOSE + seed ideas, caching
         PaperCitation rows (same table the author agent reads at paper time)
       - Council (council.scope_review) synthesizes the state of the art and
         adversarially pressure-tests the direction: every idea must cite its
         closest prior work BY KEY and carry a cheap kill test
       - a parallel, cheap environment preflight runs (claude/gpu/keys/net)
    -> scoping modal: user reviews, chats back-and-forth (council.scope_chat),
       pushes/pulls on the plan
    -> scoping.confirm(...)               (the gate)
       - approved ideas become SCIENCE directives in directives.jsonl
       - a "Related work / SOTA" block is seeded into lessons.md
       - literature_review.md is written to the workspace
       - cfg gets a `scope_brief` so the agent's _setup_prompt is grounded
       - THEN realrun.start_real(cfg) spawns the research agent

State lives in Setting key "scope_state" (mirrors council's bless_state).
Nothing here is a new daemon — the sweep runs on a background thread spawned
on demand.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import threading

from .config import DATA_DIR
from .db import SessionLocal
from .models import Setting

_SCOPE_KEY = "scope_state"
_LOCK = threading.Lock()


def _iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


# ── flag ───────────────────────────────────────────────────────────────────

def gate_enabled() -> bool:
    """The scoping gate is ON by default; set ARUI_SCOPING_GATE=0 to bypass
    (the pre-existing straight-to-start_real behaviour)."""
    v = os.environ.get("ARUI_SCOPING_GATE")
    if v is not None:
        return v.strip().lower() not in ("0", "false", "no", "off", "")
    return True


# ── state ──────────────────────────────────────────────────────────────────

def state_get() -> dict:
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(Setting.key == _SCOPE_KEY).first()
        if row and isinstance(row.value, dict):
            return dict(row.value)
        return {"status": "idle"}
    finally:
        db.close()


def state_set(state: dict) -> None:
    state = dict(state)
    state["updated_at"] = _iso()
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(Setting.key == _SCOPE_KEY).first()
        if row:
            row.value = state
        else:
            db.add(Setting(key=_SCOPE_KEY, value=state))
        db.commit()
    finally:
        db.close()
    try:
        from .bus import bus
        bus.publish("events", "scope_changed", {})
    except Exception:
        pass


def _patch(**kw) -> dict:
    """Merge-update the persisted state (read-modify-write under lock)."""
    with _LOCK:
        st = state_get()
        st.update(kw)
        state_set(st)
        return st


# ── workspace helpers ──────────────────────────────────────────────────────

def _workspace(cfg: dict):
    name = (cfg.get("repo_name") or "research").strip() or "research"
    p = DATA_DIR / "workspace" / name
    return p


# ── environment preflight (runs in parallel with the lit sweep) ─────────────

def _run_preflight() -> dict:
    """Cheap, plan-independent readiness checks. We can't load the dataset
    yet (the agent writes train.py only AFTER confirm), so this is limited to
    environment readiness; the agent's existing static-overfit SOP covers
    data-load sanity later."""
    checks = []

    def add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    add("claude_binary", shutil.which("claude") is not None,
        "claude CLI on PATH" if shutil.which("claude") else "missing")
    add("anthropic_key", bool(os.environ.get("ANTHROPIC_API_KEY")))
    add("council_key", bool(os.environ.get("GEMINI_API_KEY")
                            or os.environ.get("OPENAI_API_KEY")))
    # GPU availability via nvidia-smi
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,memory.used,utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=12)
        gpus = [l for l in (out.stdout or "").strip().splitlines() if l.strip()]
        add("gpu", bool(gpus), f"{len(gpus)} GPU(s) visible")
    except Exception as e:                                  # noqa: BLE001
        add("gpu", False, f"nvidia-smi failed: {e}")
    # arxiv reachability
    try:
        import urllib.request
        req = urllib.request.Request(
            "http://export.arxiv.org/api/query?search_query=all:test&max_results=1",
            headers={"User-Agent": "autoresearcherUI/1"})
        urllib.request.urlopen(req, timeout=12).read(64)
        add("internet_arxiv", True, "arxiv reachable")
    except Exception as e:                                  # noqa: BLE001
        add("internet_arxiv", False, f"unreachable: {e}")
    return {"checks": checks, "ran_at": _iso()}


# ── the sweep worker ───────────────────────────────────────────────────────

def _worker(cfg: dict, preview: bool) -> None:
    from . import lit_agent, council
    purpose = cfg.get("purpose", "") or ""
    metric = cfg.get("metric", "") or ""
    seed_ideas = cfg.get("seed_ideas", "") or ""
    # 1) parallel preflight
    def _pf():
        try:
            pf = _run_preflight()
            _patch(preflight=pf)
        except Exception as e:                              # noqa: BLE001
            _patch(preflight={"error": str(e)})
    threading.Thread(target=_pf, daemon=True, name="scope-preflight").start()

    # 2) literature sweep
    _patch(status="searching",
           progress={"phase": "searching papers", "papers_found": 0})

    def _on_progress(n, q):
        st = state_get()
        prog = dict(st.get("progress") or {})
        prog.update({"phase": "searching papers", "papers_found": n,
                     "last_query": q[:120]})
        _patch(progress=prog)

    try:
        papers = lit_agent.discover_for_purpose(
            purpose, seed_ideas, max_papers=24, on_progress=_on_progress)
    except Exception as e:                                  # noqa: BLE001
        _patch(status="error", error=f"lit sweep failed: {e}")
        return
    _patch(papers=papers,
           progress={"phase": "synthesizing", "papers_found": len(papers)})

    # 3) council synthesis + adversarial assessment
    _patch(status="synthesizing")
    try:
        synth = council.scope_review(purpose, metric, seed_ideas, papers)
    except Exception as e:                                  # noqa: BLE001
        _patch(status="error", error=f"council synthesis failed: {e}")
        return
    if not synth:
        _patch(status="error",
               error="The advisor model returned nothing — check API keys.")
        return

    opening = _opening_message(synth)
    _patch(status="awaiting_user", synthesis=synth,
           messages=[{"role": "agent", "text": opening, "at": _iso()}],
           progress={"phase": "awaiting confirmation",
                     "papers_found": len(papers)})


def _opening_message(synth: dict) -> str:
    parts = []
    if synth.get("problem_restated"):
        parts.append("Here's how I understand the problem:\n" +
                     synth["problem_restated"])
    if synth.get("recommended_direction"):
        parts.append("\nThe direction I'd actually commit GPUs to:\n" +
                     synth["recommended_direction"])
    parts.append("\nReview the SOTA summary, my read on your seed ideas, and "
                 "the new directions on the left — each with its closest prior "
                 "work and a cheap kill test. Push back, ask for changes, or "
                 "tell me what to drop. When you're happy, confirm the plan "
                 "and I'll start the research.")
    return "\n".join(parts)


# ── public entry points ────────────────────────────────────────────────────

def start(cfg: dict, preview: bool = False) -> dict:
    """Kick off the scoping phase on a background thread. ``preview=True`` is
    for isolated testing: confirm/skip will NOT touch the live workspace or
    spawn the research agent."""
    st = {"status": "searching", "preview": bool(preview),
          "purpose": cfg.get("purpose", ""), "metric": cfg.get("metric", ""),
          "seed_ideas": cfg.get("seed_ideas", ""),
          "started_at": _iso(),
          "progress": {"phase": "starting", "papers_found": 0},
          "messages": [], "papers": [], "synthesis": {}}
    state_set(st)
    threading.Thread(target=_worker, args=(dict(cfg), bool(preview)),
                     daemon=True, name="scope-worker").start()
    return st


def chat(text: str) -> dict:
    """One conversational turn (stateless reducer over the stored history)."""
    from . import council
    text = (text or "").strip()
    if not text:
        return state_get()
    st = state_get()
    history = list(st.get("messages") or [])
    history.append({"role": "user", "text": text, "at": _iso()})
    _patch(messages=history, thinking=True)
    try:
        reply = council.scope_chat(
            history, st.get("synthesis") or {}, st.get("papers") or [],
            st.get("purpose", ""), st.get("metric", ""),
            st.get("seed_ideas", ""))
    except Exception as e:                                  # noqa: BLE001
        reply = f"(advisor error: {e})"
    history.append({"role": "agent", "text": reply, "at": _iso()})
    return _patch(messages=history, thinking=False)


def _approved_ideas(st: dict, keep_user, keep_new) -> list[dict]:
    """Collect the ideas the user kept, normalised into directive payloads."""
    synth = st.get("synthesis") or {}
    out = []
    ua = synth.get("user_ideas_assessment") or []
    na = synth.get("new_ideas") or []
    ku = set(keep_user) if keep_user is not None else set(range(len(ua)))
    kn = set(keep_new) if keep_new is not None else set(range(len(na)))
    for i, it in enumerate(ua):
        if i in ku and isinstance(it, dict):
            out.append({
                "what": it.get("idea", ""),
                "why": it.get("novel_delta", ""),
                "acceptance": it.get("cheap_kill_test", ""),
                "idea_class": "INCREMENTAL",
                "closest_prior_work": it.get("closest_prior_work") or []})
    for i, it in enumerate(na):
        if i in kn and isinstance(it, dict):
            out.append({
                "what": it.get("idea", ""),
                "why": it.get("why", "") or it.get("novel_delta", ""),
                "acceptance": it.get("cheap_kill_test", ""),
                "idea_class": (it.get("idea_class") or "ORTHOGONAL"),
                "closest_prior_work": it.get("closest_prior_work") or []})
    return [o for o in out if (o.get("what") or "").strip()]


def _render_literature_review(st: dict) -> str:
    synth = st.get("synthesis") or {}
    papers = st.get("papers") or []
    lines = ["# Literature review (scoping phase)", "",
             f"_Generated {_iso()} by the Lit Agent + Council scoping review._",
             "", "## Problem", synth.get("problem_restated", ""), "",
             "## State of the art", synth.get("sota_summary", ""), "",
             "## Recommended direction", synth.get("recommended_direction", ""),
             "", "## Retrieved papers", ""]
    for p in papers:
        lines.append(f"- **[{p.get('key','')}]** {p.get('title','')} "
                     f"({p.get('year','')}) — {p.get('authors','')[:100]}. "
                     f"{p.get('relevance','')}")
    return "\n".join(lines) + "\n"


def confirm(final_direction: str = "", keep_user=None, keep_new=None) -> dict:
    """The gate. Materialize the confirmed plan into directives.jsonl +
    lessons.md + literature_review.md, then (unless preview) spawn the
    research agent grounded in the lit review."""
    st = state_get()
    preview = bool(st.get("preview"))
    approved = _approved_ideas(st, keep_user, keep_new)

    seeded = []
    review_md = _render_literature_review(st)
    if not preview:
        # 1) seed SCIENCE directives from approved ideas
        try:
            from . import directives
            for idea in approved:
                payload = {"type": "SCIENCE", "what": idea["what"],
                           "why": idea.get("why", ""),
                           "acceptance": idea.get("acceptance", ""),
                           "idea_class": idea.get("idea_class", "INCREMENTAL"),
                           "priority": 600, "author": "scope"}
                try:
                    d, _ = directives.upsert(payload)
                    seeded.append(d.get("id"))
                except Exception as e:                      # noqa: BLE001
                    print(f"[scope] directive seed failed: {e}", flush=True)
        except Exception as e:                              # noqa: BLE001
            print(f"[scope] directives import failed: {e}", flush=True)
        # 2) write literature_review.md + seed lessons.md Related Work
        try:
            cfg = _onboarding_cfg()
            ws = _workspace(cfg)
            ws.mkdir(parents=True, exist_ok=True)
            (ws / "literature_review.md").write_text(review_md)
            lessons = ws / "lessons.md"
            synth = st.get("synthesis") or {}
            block = ("\n\n## Related work / SOTA (from scoping)\n\n"
                     + (synth.get("sota_summary", "") or "")
                     + "\n\nConfirmed direction: "
                     + (final_direction or synth.get("recommended_direction", ""))
                     + "\n")
            with open(lessons, "a") as f:
                f.write(block)
        except Exception as e:                              # noqa: BLE001
            print(f"[scope] artifact write failed: {e}", flush=True)

    new_st = _patch(status="confirmed",
                    final_direction=final_direction,
                    seeded_directives=seeded,
                    confirmed_at=_iso())

    # 3) spawn the research agent, grounded in the lit review
    if not preview:
        try:
            cfg = _onboarding_cfg()
            cfg["scope_brief"] = _scope_brief(st, final_direction)
            _persist_onboarding(cfg)
            from . import realrun
            realrun.start_real(cfg)
        except Exception as e:                              # noqa: BLE001
            print(f"[scope] start_real failed: {e}", flush=True)
            return _patch(status="error", error=f"start_real failed: {e}")
    return new_st


def skip(reason: str = "") -> dict:
    """Expert escape hatch — bypass the gate and start research immediately."""
    st = state_get()
    preview = bool(st.get("preview"))
    new_st = _patch(status="skipped", skip_reason=reason, confirmed_at=_iso())
    if not preview:
        try:
            cfg = _onboarding_cfg()
            from . import realrun
            realrun.start_real(cfg)
        except Exception as e:                              # noqa: BLE001
            return _patch(status="error", error=f"start_real failed: {e}")
    return new_st


def _scope_brief(st: dict, final_direction: str) -> str:
    synth = st.get("synthesis") or {}
    papers = st.get("papers") or []
    top = "; ".join(f"[{p.get('key','')}] {p.get('title','')[:80]}"
                    for p in papers[:8])
    return (
        "# Literature grounding (from the scoping phase)\n"
        "A literature review was completed before you started. The full review "
        "is in `literature_review.md` and the 'Related work / SOTA' section of "
        "`lessons.md`. Honor it — do not redo the search from scratch.\n\n"
        f"## State of the art\n{synth.get('sota_summary','')}\n\n"
        f"## Confirmed research direction (agreed with the researcher)\n"
        f"{final_direction or synth.get('recommended_direction','')}\n\n"
        f"## Key prior work\n{top}\n\n"
        "Your initial directives.jsonl has been seeded with the approved, "
        "novelty-checked ideas (each carries a cheap kill test as its "
        "acceptance criterion). Start from the top of that queue.")


def _onboarding_cfg() -> dict:
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(Setting.key == "onboarding").first()
        if row and isinstance(row.value, dict):
            return dict(row.value)
        return {}
    finally:
        db.close()


def _persist_onboarding(cfg: dict) -> None:
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(Setting.key == "onboarding").first()
        if row:
            row.value = cfg
        else:
            db.add(Setting(key="onboarding", value=cfg))
        db.commit()
    finally:
        db.close()
