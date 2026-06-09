"""All HTTP routes: REST + SSE streams + the arui ingest endpoints (doc 08)."""
from __future__ import annotations

import asyncio
import datetime as dt
import glob
import json
import math
import os
import random
import re
import shlex
import subprocess
import threading

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from . import (archive, authkeys, metrics, monitor, notify, orchestrator,
               realrun)
from .bus import bus
from .config import DATA_DIR, ROOT, WORKSPACE_DIR
from .db import Base, SessionLocal, engine, get_session
from .models import (ChatMessage, Event, Gpu, Idea, JournalEntry,
                     ModeHistory, PaperBaseline, PaperBudgetEvent,
                     PaperCitation, PaperClaim, PaperDecision, PaperFigure,
                     PaperMeta, PaperProposal, PaperReviewSim, PaperSection,
                     PaperVersion, Project, Run, Setting)

router = APIRouter(prefix="/api")
_rng = random.Random()


def _poke_author_to_integrate(run_id: str, metric: float | None,
                                claim_id: str, figure_id: str,
                                run_name: str) -> None:
    """Type a concrete integrate-this-run prompt into the author tmux.
    Called from /api/track/finish whenever a paper-mode run completes.
    The author agent's standing prompt tells it to monitor results, but
    this is an immediate kick so integration is real-time, not poll-time."""
    try:
        from . import author_agent
        if not author_agent.is_running():
            return
    except Exception:
        return
    metric_s = (f"{metric:.4f}" if isinstance(metric, (int, float))
                else "(no headline metric)")
    parts = [f"Paper run {run_id} ({run_name or '?'}) finished."]
    parts.append(f"Headline metric: {metric_s}.")
    if claim_id:
        parts.append(f"It supports claim {claim_id}.")
    if figure_id:
        parts.append(f"It feeds figure {figure_id}.")
    parts.append(
        "Read its result via /paper/runs/results, update the LaTeX "
        "section/figure that uses it, recompile via /paper/recompile, "
        "and commit. If this completes a planned ablation set, "
        "consider queueing the next dataset/seed.")
    msg = " ".join(parts)
    try:
        subprocess.run(["tmux", "send-keys", "-t", "author", "-l", msg],
                       capture_output=True, timeout=8)
        subprocess.run(["tmux", "send-keys", "-t", "author", "Enter"],
                       capture_output=True, timeout=8)
    except Exception as e:
        print(f"[paper] poke author tmux failed: {e}", flush=True)


def _apply_tokens_to_env() -> None:
    """Copy onboarding-saved API tokens into ``os.environ`` so council,
    PI, and lit-agent can find them.

    Bug history: ``council._call_gemini`` reads
    ``os.environ['GEMINI_API_KEY']`` directly. The onboarding form saves
    user-provided tokens to the ``Setting`` row under the snake-case keys
    ``gemini_token`` / ``openai_token`` / ``claude_token``. Nothing
    bridged the two, so the user could paste a perfectly good Gemini key
    and still see ``[pi] no API key for gemini-2.5-pro; skipping cycle``.

    Call this at backend startup (lifespan), after every
    ``POST /api/onboarding``, and after every ``PUT /api/settings``. The
    helper is idempotent and never overwrites an existing env var that
    was set externally (e.g. via ``.env`` or systemd) — that lets the
    user override the dashboard value when they really need to."""
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(Setting.key == "onboarding").first()
        cfg = dict(row.value) if row and isinstance(row.value, dict) else {}
    finally:
        db.close()
    pairs = {
        "ANTHROPIC_API_KEY": cfg.get("claude_token"),
        "OPENAI_API_KEY":    cfg.get("openai_token"),
        "GEMINI_API_KEY":    cfg.get("gemini_token"),
        "GOOGLE_API_KEY":    cfg.get("gemini_token"),  # some libs use this name
        "GITHUB_TOKEN":      cfg.get("github_token"),
    }
    set_names = []
    for env_name, val in pairs.items():
        val = (val or "").strip()
        if not val:
            continue
        # Don't clobber an externally-set value — env wins over Settings.
        if os.environ.get(env_name):
            continue
        os.environ[env_name] = val
        set_names.append(env_name)
    if set_names:
        print(f"[api] applied tokens from onboarding to env: "
              f"{', '.join(set_names)}", flush=True)


def _set_setting(key: str, value) -> None:
    """Persist a single Settings key (small helper for mode-switch logic)."""
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(Setting.key == "onboarding").first()
        cfg = dict(row.value) if row and isinstance(row.value, dict) else {}
        cfg[key] = value
        if row:
            row.value = cfg
        else:
            db.add(Setting(key="onboarding", value=cfg))
        db.commit()
    finally:
        db.close()


async def _safe_json(request: Request) -> dict:
    """Tolerant body parser: empty body or invalid JSON → {} instead of 500.
    Useful for endpoints where every field is optional."""
    try:
        if not request.headers.get("content-length"):
            return {}
        body = await request.json()
        return body if isinstance(body, dict) else {}
    except Exception:
        return {}


def _iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


# ─────────── required default metrics (kept in sync with arui SDK) ────────
# Every training run should log these so the drawer's "All plots" section is
# populated and runs are comparable. Skipping the agent's `import arui` (and
# so the SDK constant) is cheap and import-safe, so we duplicate the tuple
# here rather than importing across the project boundary.
REQUIRED_DEFAULT_METRICS = (
    "val_loss", "val_acc", "lr", "train_loss", "train_acc",
    "time_per_step", "samples_per_sec",
)


def _check_required_metrics(run_id: str) -> list[str]:
    """Return the list of REQUIRED_DEFAULT_METRICS the run never logged.
    Empty list means the run is well-behaved."""
    try:
        logged = set(metrics.keys(run_id))
    except Exception:
        logged = set()
    return [k for k in REQUIRED_DEFAULT_METRICS if k not in logged]


def _emit_missing_metric_warnings(run_id: str, missing: list[str]) -> None:
    """Persist one warning Event per missing default-metric key. The run
    drawer's Events section surfaces these so the researcher sees clearly
    that the agent skipped a default plot."""
    if not missing:
        return
    db = SessionLocal()
    try:
        for key in missing:
            ev = Event(
                id=f"ev-{_rng.randrange(16**8):08x}",
                type="missing_default_metric",
                severity="warning", actor="system",
                message=(f"Run did not log required default metric "
                         f"'{key}' — see arui.log_defaults(...) "
                         f"in $ARUI_REPO/arui/__init__.py"),
                run_id=run_id, created_at=_iso())
            db.add(ev)
        db.commit()
    finally:
        db.close()


# ─────────────────────────── headline metric ───────────────────────────────
# Keys that are configuration/bookkeeping, never a run's headline result.
_NON_METRIC = {
    "params", "n_params", "nparams", "num_params", "param_count",
    "seed", "step", "steps", "epoch", "epochs", "iter", "iters", "max_iters",
    "batch_size", "global_batch_size", "ensemble_size", "n_eval", "n",
    "size", "hidden_size", "h_cycles", "gpu", "gpu_index", "lr",
}
# Metric names worth using as a headline when the validation metric is absent.
_COMMON_METRICS = ["gsm8k_test_acc", "test_acc", "val_acc", "accuracy",
                   "score", "f1", "val_loss", "val_mse", "loss"]


def _looks_baseline(name: str) -> bool:
    return "baseline" in (name or "").lower()


def _as_number(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def _resolve_headline(run_id: str, summary: dict, metric_key: str):
    """A run's headline number: the project's validation metric if the agent
    reported it, else a logged value of it, else a sane numeric fallback.
    Never picks an obvious non-metric like a parameter count."""
    keys = ([metric_key] if metric_key else []) + \
           [k for k in _COMMON_METRICS if k != metric_key]
    for k in keys:                          # 1. summary value for a metric key
        n = _as_number((summary or {}).get(k))
        if n is not None:
            return n
    for k in keys:                          # 2. last logged point of a metric
        v = metrics.latest(run_id, k)
        if v is not None:
            return float(v)
    for k, v in (summary or {}).items():    # 3. any non-config numeric value
        if str(k).lower() not in _NON_METRIC:
            n = _as_number(v)
            if n is not None:
                return n
    return None


def _is_crashed(headline, direction: str) -> bool:
    """A run crashed if it has no metric or training diverged. For loss-style
    (minimize) metrics a huge value signals divergence; an accuracy-style
    (maximize) metric simply being low is a poor result, not a crash."""
    if headline is None or not math.isfinite(headline):
        return True
    if direction != "maximize" and headline >= 5e4:
        return True
    return False


# ───────────────────────────── REST: read ──────────────────────────────────

@router.get("/project")
def get_project(db: Session = Depends(get_session)):
    p = db.query(Project).first()
    if not p:
        return {}
    # Self-heal: if metric NAME clearly maximizes ('_acc', 'f1', 'score',
    # 'pass@', etc) but stored direction is minimize, FIX it. This was
    # how the Francois-2026-05-31 bug manifested on the dashboard: the
    # broken metric-dropdown UI saved val_loss + minimize at onboarding,
    # then the user manually re-set the metric to gsm8k_val_acc but the
    # direction stayed minimize → kept runs reading 1.0 were "worse"
    # than baseline 0.973 and never marked as frontier improvements,
    # so the hero chart was wrong. Source-of-truth for direction is
    # the metric name; storage is just a cache.
    correct_dir = _infer_metric_direction(p.validation_metric or "")
    if correct_dir and correct_dir != p.metric_direction:
        print(f"[api] /project auto-correcting metric_direction: "
              f"{p.validation_metric!r} stored as {p.metric_direction!r}, "
              f"name says {correct_dir!r}", flush=True)
        p.metric_direction = correct_dir
        db.commit()
    runs = db.query(Run).filter(Run.project_id == p.id).all()
    ideas = db.query(Idea).filter(Idea.project_id == p.id).all()
    # Status taxonomy (RESEARCH_IMPROVEMENT_PLAN #4) expanded the set:
    # kept_novel/kept_replicate/success_smoke all count as "done", and
    # we keep "kept" for back-compat with pre-migration rows.
    _DONE_STATUSES = ("kept", "kept_novel", "kept_replicate",
                       "success_smoke", "discarded", "crashed")
    _KEPT_STATUSES = ("kept", "kept_novel", "kept_replicate")
    done = [r for r in runs if r.status in _DONE_STATUSES]
    kept = [r for r in done if r.status in _KEPT_STATUSES]
    best = None
    for r in done:
        if (r.status == "crashed" or r.headline_metric is None
                or not math.isfinite(r.headline_metric)):
            continue
        if best is None or (r.headline_metric > best
                            if p.metric_direction == "maximize"
                            else r.headline_metric < best):
            best = r.headline_metric
    # ── baseline picking (FIXED 2026-06-05) ───────────────────────────
    # The dashboard's "improvement vs baseline" is meant to show *how far
    # the agent has moved the metric from the no-mitigation starting
    # point*. Agents tend to set `is_baseline=True` on the CONTROL run
    # (e.g. `clean_baseline` — the un-poisoned model that by definition
    # already sits at the metric's optimum), which makes the dashboard
    # read "0 → 0, no improvement" even when the agent has just gone
    # from 0.99 to 0.00.
    #
    # Heuristic: prefer the agent-marked baseline, BUT only if it's
    # actually a useful comparison anchor (i.e., it is NOT already
    # at-or-near the optimum direction). If the marked baseline equals
    # `best`, fall back to "the worst kept run" — that is the genuine
    # upper-bound the agent is trying to beat. Names like clean_* /
    # *_clean / *_floor are also skipped because they're conventionally
    # used for "ideal floor", not "starting point".
    def _is_better(a, b):
        if a is None: return False
        if b is None: return True
        return (a > b) if p.metric_direction == "maximize" else (a < b)

    def _looks_like_floor(name: str) -> bool:
        n = (name or "").lower()
        return ("clean_" in n or n.endswith("_clean")
                or n.startswith("clean_") or "_floor" in n)

    def _is_probe(name: str) -> bool:
        n = (name or "").lower()
        return n.startswith("_smoke") or n.startswith("_probe")

    def _finite_real(r) -> bool:
        # A real (non-probe), scoreable, non-crashed run.
        return (r.headline_metric is not None
                and math.isfinite(r.headline_metric)
                and not _is_crashed(r.headline_metric, p.metric_direction)
                and not _is_probe(r.run_name or ""))

    # Anchor pool: ANY real non-crashed run with a finite headline — NOT
    # just `kept` ones. The undefended "no-mitigation" baseline is often
    # not a kept run (it's a control / seed / discarded run), and when we
    # only looked at kept runs the dashboard grabbed the worst *already-
    # mitigated* run as the baseline — a near-optimal, misleading anchor
    # (e.g. it showed "0.034 → 0.0 solved" when the true undefended
    # baseline `seed_bl_lora` sat at ASR 0.85). 2026-06-09 fix.
    anchor_pool = [r for r in runs if _finite_real(r)]
    marked = next((r for r in runs if r.is_baseline and _finite_real(r)), None)
    base_run = None
    if marked and not _looks_like_floor(marked.run_name or "") \
            and not (_is_better(marked.headline_metric, best)
                     or marked.headline_metric == best):
        base_run = marked
    elif anchor_pool:
        # Worst real run — the WORST metric value, i.e. the genuine
        # "no progress yet / no-mitigation" starting point.
        base_run = (max if p.metric_direction != "maximize" else min)(
            anchor_pool, key=lambda r: r.headline_metric)

    # Degenerate-baseline guard: if we can't find an anchor that is
    # actually WORSE than `best`, the "improvement vs baseline" story is
    # meaningless (best == baseline, or no real run yet). Surface that
    # honestly instead of printing a fake near-optimal baseline — and tell
    # the agent how to fix it (mark the undefended run explicitly).
    base_metric = base_run.headline_metric if base_run else None
    baseline_degenerate = False
    baseline_note = ""
    if base_metric is None:
        baseline_degenerate = True
        baseline_note = "no baseline run established yet"
    elif best is not None and not _is_better(best, base_metric):
        baseline_degenerate = True
        baseline_note = ("baseline is at-or-better than the best run — mark "
                         "the true no-mitigation run with "
                         "arui.init(baseline=True)")

    return {
        **p.dict(),
        "experiments_done": len(done),
        "experiments_running": len([r for r in runs if r.status == "running"]),
        "experiments_total": len(ideas),
        "success_rate": round(len(kept) / len(done), 2) if done else 0,
        "best_metric": best,
        "baseline_metric": base_metric,
        "baseline_run_name": base_run.run_name if base_run else None,
        "baseline_degenerate": baseline_degenerate,
        "baseline_note": baseline_note,
    }


def _infer_metric_direction(metric: str) -> str:
    """Direction from metric NAME (same heuristic /api/onboarding uses).
    Returns '' if the name gives no strong signal."""
    _ml = re.sub(r"[\s\-]+", "_", (metric or "").strip().lower())
    _max = (
        "accuracy", "_acc", "acc_", "acc@",
        "f1", "exact_match", "em", "_em",
        "bleu", "rouge", "meteor", "chrf",
        "score", "reward",
        "auc", "map", "ndcg", "hit", "mrr",
        "pass@", "win", "elo",
    )
    _min = (
        "loss", "perplexity", "ppl", "error",
        "rmse", "mse", "mae", "bpb", "bpc",
        "fid", "kid", "divergence", "regret",
    )
    if any(t in _ml for t in _min):
        return "minimize"
    if any(t in _ml for t in _max):
        return "maximize"
    return ""


@router.get("/ideas")
def list_ideas(db: Session = Depends(get_session)):
    ideas = db.query(Idea).all()
    # upcoming sorted by manual priority then EV desc (doc 05 5.7)
    return sorted([i.dict() for i in ideas],
                  key=lambda i: (-i["manual_priority"], -i["ev"]))


@router.post("/ideas/reorder")
async def ideas_reorder(request: Request):
    """Re-rank queued ideas (top of the list runs first) and tell the agent."""
    body = await request.json()
    order = body.get("order", [])
    db = SessionLocal()
    names = []
    for i, iid in enumerate(order):
        idea = db.query(Idea).filter(Idea.id == iid).first()
        if idea:
            idea.manual_priority = len(order) - i
            names.append(idea.idea_id)
    db.commit()
    db.close()
    if names:
        try:
            monitor.message_agent(
                "The researcher reprioritised the idea queue — run the "
                "queued ideas in THIS order next: " + ", ".join(names)
                + ". Update ideas.md to match this ordering.")
        except Exception:                            # noqa: BLE001
            pass
    bus.publish("events", "runs_changed", {})
    return {"status": "ok", "order": names}


@router.post("/ideas/delete")
async def ideas_delete(request: Request):
    """Remove a queued idea; feed the reason back to the agent."""
    body = await request.json()
    iid = body.get("idea_id", "")
    reason = (body.get("reason") or "").strip()
    db = SessionLocal()
    idea = db.query(Idea).filter(Idea.id == iid).first()
    name = idea.idea_id if idea else iid
    if idea:
        db.delete(idea)
    row = db.query(Setting).filter(Setting.key == "dismissed_ideas").first()
    dismissed = list(row.value) if row and isinstance(row.value, list) else []
    if name and name not in dismissed:
        dismissed.append(name)
    if row:
        row.value = dismissed
    else:
        db.add(Setting(key="dismissed_ideas", value=dismissed))
    db.add(Event(id=f"ev-{_rng.randrange(16**8):08x}", type="idea_added",
                 severity="info", actor="human",
                 message=f"Researcher removed queued idea '{name}'"
                         + (f" — {reason}" if reason else ""),
                 created_at=_iso()))
    db.commit()
    db.close()
    try:
        monitor.message_agent(
            f"The researcher REMOVED the queued idea '{name}' from the plan."
            + (f" Their reason: {reason}." if reason else "")
            + " Do not run it — remove it from ideas.md and take this "
              "preference into account when choosing future ideas.")
    except Exception:                                # noqa: BLE001
        pass
    bus.publish("events", "runs_changed", {})
    return {"status": "ok"}


@router.get("/runs")
def list_runs(status: str = "", db: Session = Depends(get_session)):
    """List runs. Optional `?status=X` (or comma-separated `?status=X,Y`)
    filters by Run.status. Case-insensitive. Unknown statuses just yield
    an empty result. No filter → all runs."""
    q = db.query(Run)
    if status:
        wanted = [s.strip().lower() for s in status.split(",") if s.strip()]
        if wanted:
            q = q.filter(func.lower(Run.status).in_(wanted))
    return [r.dict() for r in q.all()]


@router.get("/runs/{run_id}")
def get_run(run_id: str, db: Session = Depends(get_session)):
    r = db.query(Run).filter(Run.id == run_id).first()
    if not r:
        return {}
    idea = db.query(Idea).filter(Idea.id == r.idea_id).first()
    return {**r.dict(), "idea": idea.dict() if idea else None,
            "metric_keys": metrics.keys(run_id)}


@router.get("/runs/{run_id}/metrics")
def run_metrics(run_id: str, keys: str = "", db: Session = Depends(get_session)):
    wanted = [k for k in keys.split(",") if k] or None
    return metrics.query(run_id, wanted)


@router.get("/gpus")
def list_gpus(db: Session = Depends(get_session)):
    return [g.dict() for g in db.query(Gpu).order_by(Gpu.index).all()]


@router.get("/events")
def list_events(limit: int = 60, before: str = "",
                db: Session = Depends(get_session)):
    """Recent events. Pass `before=<iso>` for the page older than that — used
    by the Summary feed's lazy scroll-up loader."""
    q = db.query(Event)
    if before:
        q = q.filter(Event.created_at < before)
    rows = q.order_by(Event.created_at.desc()).limit(limit).all()
    return [e.dict() for e in rows]


@router.get("/journal")
def list_journal(db: Session = Depends(get_session)):
    rows = db.query(JournalEntry).order_by(
        JournalEntry.created_at.desc()).all()
    return [j.dict() for j in rows]


# ───────────────────────── Agent phase reporting ──────────────────────────
# The agent is the source of truth for what phase the research loop is in.
# Every lifecycle transition (bootstrap → planning → launching_runs → ...)
# is reported by the agent via ``arui.phase(...)``. The dashboard pill
# reads ``orchestrator.phase`` directly instead of inferring phase from
# tmux scrollback (which was the root cause of all the stale-status bugs
# Francois reported on 2026-06-05).
#
# Allowed phases — mirror of ``arui.PHASES``. We DO NOT reject unknown
# phases server-side (the SDK already warns); we just store whatever the
# agent sent, so adding a new phase doesn't require a coordinated
# backend deploy.

_PHASE_KEY = "orchestrator.phase"
_PHASE_DEFAULT = {"phase": "bootstrap", "at": "", "detail": {}}


@router.get("/phase")
def get_phase(db: Session = Depends(get_session)):
    """Read the current agent-reported phase + a derived fallback.

    Returns ``{phase, at, detail, fallback_used}``. If the agent has
    never called ``arui.phase()`` yet (e.g. an old onboarding from
    before the SDK existed), ``fallback_used=True`` and ``phase`` is
    derived from DB ground truth (runs in flight, council in flight,
    etc) so the pill is still useful.
    """
    row = db.query(Setting).filter(Setting.key == _PHASE_KEY).first()
    if row and isinstance(row.value, dict) and row.value.get("phase"):
        v = dict(row.value)
        v.setdefault("at", "")
        v.setdefault("detail", {})
        v["fallback_used"] = False
        return v
    # Fallback: derive phase from DB so the pill is informative even on
    # legacy projects that don't call arui.phase().
    running = db.query(Run).filter(Run.status == "running").count()
    total = db.query(Run).count()
    if total == 0:
        ph = "bootstrap"
    elif running > 0:
        ph = "watching_runs"
    else:
        ph = "planning"
    return {"phase": ph, "at": "", "detail": {}, "fallback_used": True}


# ─────────────────────── Watchdog config (PR 4) ───────────────────────────
# The watchdog is a non-LLM monitoring harness that scans every RUNNING
# run against a registry of "scripts" (no_metric_flow, nan_loss, etc).
# Each script has DEFAULT_PARAMS the agent reviews at onboarding — the
# operator can also edit them later via the Settings → Watchdog panel.


@router.get("/watchdog/scripts")
def list_watchdog_scripts():
    """Default scripts shipped with the package. Used by the onboarding
    UI and Settings panel to show 'these monitors are watching your
    runs; here's what each one does'."""
    try:
        from . import watchdog as wd
        return {"scripts": wd.list_scripts()}
    except Exception as e:                                  # noqa: BLE001
        return {"scripts": [], "error": str(e)[:240]}


@router.get("/watchdog/config")
def get_watchdog_config():
    """Active per-project config (defaults merged with operator/agent
    overrides). The frontend renders this as a list of toggles +
    editable params."""
    try:
        from . import watchdog as wd
        return {"config": wd.get_config()}
    except Exception as e:                                  # noqa: BLE001
        return {"config": {}, "error": str(e)[:240]}


@router.post("/watchdog/config")
async def post_watchdog_config(request: Request):
    """Operator (or onboarding flow) overrides one or more script
    configs. Accepts a partial dict — unspecified scripts keep their
    current values."""
    body = await request.json()
    config = body.get("config") or {}
    source = body.get("source") or "operator"
    try:
        from . import watchdog as wd
        merged = wd.set_config(config, source=source)
        return {"ok": True, "config": merged}
    except Exception as e:                                  # noqa: BLE001
        return {"ok": False, "error": str(e)[:240]}


@router.post("/watchdog/run")
def run_watchdog_now():
    """Force one watchdog tick immediately and return what fired.
    Useful for verifying onboarding config before the next monitor.py
    sweep."""
    try:
        from .watchdog import runner
        fired = runner.run_once()
        return {"ok": True, "fired": fired}
    except Exception as e:                                  # noqa: BLE001
        return {"ok": False, "error": str(e)[:240]}


@router.post("/watchdog/review")
async def review_watchdog_config(request: Request):
    """Ask the council whether the watchdog defaults make sense for
    THIS research project. Idempotent: once applied, returns
    skipped/already_reviewed on subsequent calls unless body has
    {"force": true}. Called automatically after onboarding completes
    (see realrun.start_real) and also exposed for manual re-review
    when the operator changes the project purpose."""
    body = {}
    try:
        body = await request.json()
    except Exception:                                       # noqa: BLE001
        pass
    force = bool(body.get("force"))
    try:
        from .watchdog import onboarding as wd_onboarding
        return wd_onboarding.review_with_council(force=force)
    except Exception as e:                                  # noqa: BLE001
        return {"status": "error", "error": str(e)[:240]}


@router.get("/health")
def get_health(db: Session = Depends(get_session)):
    """The dashboard pill, modal, and PI all consume this. Source of
    truth for "is the loop OK?". See backend/app/health/service.py for
    the assembly logic. Returns the same HealthSnapshot.as_dict()
    shape on every call so the frontend can cache + diff."""
    try:
        from .health import service as _hs
        snap = _hs.compute()
        return snap.as_dict()
    except Exception as e:                                  # noqa: BLE001
        # Health computation must NEVER 500 the dashboard. Return a
        # safe fallback so the pill still renders something.
        print(f"[api] /health crashed: {e}", flush=True)
        return {
            "phase": {"phase": "bootstrap", "at": "",
                       "detail": {}, "fallback_used": True},
            "summary": f"health unavailable: {e!s}"[:240],
            "issues": [],
            "facts": {"error": True, "exception": str(e)[:240]},
        }


@router.post("/phase")
async def post_phase(request: Request):
    """Agent reports its current lifecycle phase. Persists the value
    to a Setting row, emits a ``phase_changed`` Event when the phase
    actually changed, and returns the persisted state.

    Body: ``{"phase": "<one of arui.PHASES>", "detail": {...}}``.
    Unknown phases are accepted (forward-compatible) — the SDK already
    warns on the client side.
    """
    body = await request.json()
    phase = (body.get("phase") or "").strip()
    detail = body.get("detail") or {}
    if not phase:
        return {"ok": False, "error": "phase required"}
    if not isinstance(detail, dict):
        detail = {"value": detail}
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(Setting.key == _PHASE_KEY).first()
        prev_phase = ""
        if row and isinstance(row.value, dict):
            prev_phase = row.value.get("phase") or ""
        now = _iso()
        new_value = {"phase": phase, "at": now, "detail": detail}
        if row is None:
            db.add(Setting(key=_PHASE_KEY, value=new_value))
        else:
            row.value = new_value
        # Emit a phase_changed Event only on actual transitions — every
        # tick re-asserting the same phase would flood the activity feed.
        if phase != prev_phase:
            ev_msg = (f"agent: {prev_phase or '(none)'} → {phase}")[:280]
            db.add(Event(id="ev-" + os.urandom(4).hex(),
                         type="phase_changed", severity="info",
                         actor="agent", message=ev_msg, created_at=now))
        db.commit()
        try:
            bus.publish("events", "phase_changed",
                        {"phase": phase, "prev": prev_phase, "detail": detail})
        except Exception:                                   # noqa: BLE001
            pass
        return {"ok": True, "phase": phase, "at": now,
                "detail": detail, "transitioned": phase != prev_phase}
    finally:
        db.close()


@router.get("/chat")
def list_chat(db: Session = Depends(get_session)):
    rows = db.query(ChatMessage).order_by(ChatMessage.created_at).all()
    return [m.dict() for m in rows]


# ──────────────────────────── REST: write ──────────────────────────────────

@router.post("/chat")
async def post_chat(request: Request):
    body = await request.json()
    text = (body.get("content") or "").strip()
    db = SessionLocal()
    msg = ChatMessage(id=f"cm-{_rng.randrange(16**8):08x}", role="researcher",
                      content=text, created_at=_iso())
    db.add(msg)
    db.commit()
    bus.publish("chat", "chat", msg.dict())
    db.close()
    asyncio.create_task(_agent_reply(text))
    return {"ok": True}


async def _agent_reply(prompt: str) -> None:
    """Scaffold stand-in for the Principal Researcher (doc 05 5.9)."""
    await asyncio.sleep(1.4)
    canned = ("On it. The four running experiments are all past step 200; "
              "prototype insertion is still the front-runner. I'll fold your "
              "note into the next planning loop.")
    db = SessionLocal()
    msg = ChatMessage(id=f"cm-{_rng.randrange(16**8):08x}", role="agent",
                      content=canned, created_at=_iso())
    db.add(msg)
    db.commit()
    bus.publish("chat", "chat", msg.dict())
    db.close()


@router.post("/ideas/reorder")
async def reorder_ideas(request: Request):
    """Pin a manual priority order on the idea queue (doc 05 5.8)."""
    body = await request.json()
    ordered = body.get("ordered_ids", [])
    db = SessionLocal()
    for rank, idea_id in enumerate(reversed(ordered)):
        idea = db.query(Idea).filter(Idea.id == idea_id).first()
        if idea:
            idea.manual_priority = rank + 1
    db.commit()
    db.close()
    bus.publish("events", "runs_changed", {})
    return {"ok": True}


# ────── council code-bless: pre-flight code review before any run ──────

@router.post("/council/bless")
async def council_bless(request: Request):
    """Kick off a council review of the current research workspace.

    Returns immediately with status='pending'; reviewers vote in the
    background. The agent's prompt instructs it to call this after
    scaffolding code, then poll /api/council/bless/status before any
    training run. The /api/track/run endpoint refuses (HTTP 423) until
    the verdict is 'approved'.

    Body may include seed-task metadata (RESEARCH_IMPROVEMENT_PLAN #7)
    consumed by the deterministic seed-task gate:
      - val_set_size: int
      - dataset_kind: "smoke" to opt out of the size gate
      - train_30s: bool — true if the agent observed train.py finish in
        < 30s on a single CPU (smoke-task signal)
    """
    from . import council as _c
    body = await _safe_json(request)
    # Default to the current project's workspace; allow override for tests.
    if body.get("workspace"):
        workspace = body["workspace"]
    else:
        db = SessionLocal()
        try:
            row = db.query(Setting).filter(
                Setting.key == "onboarding").first()
            cfg = dict(row.value) if row and isinstance(row.value, dict) \
                else {}
            proj = db.query(Project).first()
        finally:
            db.close()
        name = ((cfg.get("repo_name") or
                 (proj.name if proj else "research") or "research").strip())
        workspace = str(WORKSPACE_DIR / name)
    bless_meta = {
        k: body[k]
        for k in ("val_set_size", "dataset_kind", "train_30s",
                  "program_md_marks_smoke")
        if k in body
    }
    return _c.bless_async(workspace, bless_meta=bless_meta or None)


@router.get("/council/bless/status")
def council_bless_status():
    """The latest verdict (or 'not_requested' / 'pending' / 'approved' /
    'rejected'). Polled by the agent and shown on the dashboard."""
    from . import council as _c
    return _c.bless_status()


@router.post("/council/bless/reset")
def council_bless_reset():
    """Clear the bless state — used when the agent edits code after a
    rejection so the next /api/council/bless gets a clean slate. Also
    exposed as a dashboard button if the human wants a re-review."""
    from . import council as _c
    _c._bless_state_set({"status": "not_requested",
                         "summary": "Cleared — awaiting re-review"})
    return _c.bless_status()


# ───────── preflight SOP: 3-step validation before any real run ─────────
#
# The agent's prompt walks it through three checks that MUST pass before
# /api/council/bless is allowed to even start a review (and therefore
# before /api/track/run will accept any non-_probe/_smoke run):
#
#   step 1 — static-batch overfit to ~0 train loss (proves train.py)
#   step 2 — uniform classification head at init (proves architecture)
#   step 3 — council bless (proves the code matches the project purpose)
#
# The agent records steps 1 + 2 via the endpoints below. Step 3 is the
# existing /api/council/bless route, which now refuses to run until
# 1 + 2 are recorded AND fresh (newer than the last code_changed
# marker).

@router.post("/preflight/static_overfit")
async def preflight_static_overfit(request: Request):
    """Record that step 1 of the SOP (static-batch overfit to ~0 train
    loss) has passed. Body: {"evidence": "<one-line proof>",
    "final_loss": 0.0008}. Returns the updated preflight summary."""
    from . import council as _c
    body = await _safe_json(request)
    summary = _c.preflight_record_static_overfit(
        evidence=str(body.get("evidence") or ""),
        final_loss=body.get("final_loss"))
    return {"ok": True, "preflight": summary}


@router.post("/preflight/uniform_init")
async def preflight_uniform_init(request: Request):
    """Record that step 2 of the SOP (uniform classification head at
    init) has passed. Body: {"evidence": "<one-line proof>",
    "entropy": 6.91}. Returns the updated preflight summary."""
    from . import council as _c
    body = await _safe_json(request)
    summary = _c.preflight_record_uniform_init(
        evidence=str(body.get("evidence") or ""),
        entropy=body.get("entropy"))
    return {"ok": True, "preflight": summary}


@router.post("/preflight/code_changed")
async def preflight_code_changed(request: Request):
    """Mark a significant code change. Bumps `changed_at_iso`, which
    invalidates any earlier preflight step (and any prior bless
    approval) — agent must redo steps 1 + 2 + bless. Body:
    {"reason": "<one-line summary of the change>"}."""
    from . import council as _c
    body = await _safe_json(request)
    summary = _c.preflight_record_code_changed(
        reason=str(body.get("reason") or ""))
    return {"ok": True, "preflight": summary}


@router.get("/preflight/status")
def preflight_status():
    """Snapshot of all three pills (static_overfit_passed,
    uniform_init_passed, blessed) plus raw timestamps + evidence so the
    dashboard can render the 3-pill banner."""
    from . import council as _c
    return _c.preflight_summary()


@router.post("/preflight/bless")
async def preflight_bless(request: Request):
    """Convenience alias for /api/council/bless — keeps the SOP
    endpoint surface uniform (preflight/{static_overfit,uniform_init,
    bless}). Delegates to the same implementation."""
    return await council_bless(request)


# ───────────────────────── arui ingest (doc 06) ────────────────────────────

@router.post("/track/run")
async def track_run(request: Request):
    from fastapi.responses import JSONResponse
    from . import council as _c
    from . import directives as _directives
    from . import novelty
    body = await request.json()
    name = body.get("name", f"run-{_rng.randrange(16**6):06x}")
    config = body.get("config", {}) or {}
    # HARD-HALT GATE (RESEARCH_IMPROVEMENT_PLAN #6): when the strategic
    # council escalates or the PI agent calls /api/halt, ALL runs are
    # blocked — including _probe / _smoke. Only a human PI resume lifts
    # this. Checked BEFORE the probe whitelist on purpose.
    halted, halt_reason = notify.research_halted()
    if halted:
        return JSONResponse({
            "ok": False, "blocked": True, "reason": "research_halted",
            "halt_reason": halt_reason,
            "hint": ("Research is HALTED — POST /api/halt/resume "
                     "(requires admin passcode) to unlock."),
        }, status_code=423)
    # OPEN HALT DIRECTIVE: directives.jsonl HALT directives block ALL
    # runs (including probes) until the human PI closes them. Same shape.
    halt_d = _directives.open_halt()
    if halt_d:
        return JSONResponse({
            "ok": False, "blocked": True, "reason": "halt_directive",
            "directive": halt_d,
            "hint": ("There is an open HALT directive — resolve it via "
                     "POST /api/directives/<id>/done."),
        }, status_code=423)
    # CODE-BLESS GATE: the council must approve the codebase before the
    # first training run can register. The agent's prompt knows the dance
    # (POST /api/council/bless → poll /api/council/bless/status →
    # only proceed when status==approved). 423 = Locked. Whitelist:
    # any name starting with _probe / _smoke for the agent's pre-flight
    # smoke tests, which prove the code RUNS at all before bless.
    if not novelty.is_probe_or_smoke(name):
        # BLOCKER directive gate (RESEARCH_IMPROVEMENT_PLAN #1): a real
        # run must yield to any open BLOCKER_INFRA / BLOCKER_EVAL
        # directive. The agent's prompt instructs it to implement the
        # blocker first and POST /api/directives/<id>/done with evidence.
        blocker_kind = _directives.open_blocker_kind()
        if blocker_kind:
            return JSONResponse({
                "ok": False, "blocked": True,
                "reason": "open_blocker_directive",
                "blocker_kind": blocker_kind,
                "hint": ("There is an open " + blocker_kind + " directive — "
                         "implement it first, then POST "
                         "/api/directives/<id>/done with evidence. "
                         "Probe / smoke runs (_probe / _smoke names) are "
                         "still allowed."),
            }, status_code=423)
        # RESEARCH-PAUSED GATE: the user can hit "Pause research" in
        # Settings (Task #1) to stop the loop without killing in-flight
        # runs. Same 423-Locked shape as the bless gate so the agent
        # / SDK can detect it and back off cleanly. Checked BEFORE
        # bless so a paused project doesn't surface bless errors.
        if notify.research_paused():
            return JSONResponse({
                "ok": False,
                "blocked": True,
                "reason": "research_paused",
                "hint": ("Research is paused — POST /api/research/resume "
                         "(or click 'Resume research' in Settings) to "
                         "unlock new runs."),
            }, status_code=423)
        if not _c.is_code_blessed():
            st = _c.bless_status()
            return JSONResponse({
                "ok": False,
                "blocked": True,
                "reason": "code_not_blessed",
                "bless_status": st,
                "hint": ("POST /api/council/bless then poll "
                         "/api/council/bless/status — once status='approved' "
                         "training runs are unlocked."),
            }, status_code=423)
    # DUPLICATE-KILLER GATE (RESEARCH_IMPROVEMENT_PLAN #3): a fresh
    # config hash MUST be unique unless this is an explicit seed
    # replicate or a probe/smoke. Without this gate the agent was
    # re-launching the same 5-way ensemble 5×, the council noticed,
    # the agent ignored it, and the dashboard rewarded every copy.
    # See backend/app/novelty.py for the canonicalisation rules.
    accepted, existing_run_id, h = novelty.register(config, name)
    if not accepted:
        return JSONResponse({
            "ok": False,
            "error": "duplicate",
            "existing_run_id": existing_run_id,
            "novelty_hash": h,
            "hint": ("This config was already registered as run "
                     f"{existing_run_id!r}. Tag the run with "
                     "idea_class=REPRODUCE / seed_replicate=true / "
                     "run_id startswith 'seed_' to request an explicit "
                     "seed replicate."),
        }, status_code=409)
    db = SessionLocal()
    project = db.query(Project).first()
    pid = project.id if project else "proj-default"
    if not db.query(Run).filter(Run.id == name).first():
        idea = Idea(id=f"idea-{name}", project_id=pid, idea_id=name,
                    description="", status="running",
                    source="agent", created_at=_iso(), started_at=_iso())
        db.add(idea)
        # is_baseline can be set EXPLICITLY by the agent via
        # arui.init(baseline=True) (lands in config as "is_baseline"), or
        # inferred from a name containing "baseline". The explicit flag is
        # what lets the agent mark a no-mitigation anchor whose name isn't
        # literally "baseline" (e.g. "seed_bl_*") — see the baseline-picker
        # in /project. 2026-06-09 fix.
        explicit_bl = bool((config or {}).get("is_baseline"))
        db.add(Run(id=name, project_id=pid, idea_id=idea.id, run_name=name,
                   status="running",
                   is_baseline=(explicit_bl or _looks_baseline(name)),
                   config=config,
                   started_at=_iso(), created_at=_iso()))
        db.commit()
        bus.publish("events", "runs_changed", {})
    db.close()
    return {"run_id": name, "novelty_hash": h}


_METRICS_CHANGED_DEBOUNCE: dict[str, float] = {}
_METRICS_CHANGED_DEBOUNCE_LOCK = threading.Lock()


def _maybe_emit_metrics_changed(run_id: str) -> None:
    """Coalesce metrics_changed(run_id) events to at most one per 2 s per
    run. The Analysis tab uses this to refetch bucketed data; firing more
    often than that just makes lines flash as buckets shift. Combined
    with the frontend's 1.2 s refresh debounce, this gives a smooth
    once-per-2 s tick on running runs."""
    import time as _time
    now = _time.time()
    with _METRICS_CHANGED_DEBOUNCE_LOCK:
        last = _METRICS_CHANGED_DEBOUNCE.get(run_id, 0.0)
        if now - last < 2.0:
            return
        _METRICS_CHANGED_DEBOUNCE[run_id] = now
    bus.publish("metrics", "metrics_changed", {"run_id": run_id})


@router.post("/track/log")
async def track_log(request: Request):
    body = await request.json()
    run_id = body["run_id"]
    points = body.get("points", [])
    metrics.append(run_id, points)
    bus.publish("metrics", "metric", {"run_id": run_id, "points": points})
    _maybe_emit_metrics_changed(run_id)
    return {"ok": True}


@router.post("/track/logs")
async def track_logs(request: Request):
    """Append a run's captured console output (sent by the arui SDK). This is
    how run logs are collected — independent of tmux."""
    body = await request.json()
    run_id = body.get("run_id", "")
    text = body.get("text", "")
    if run_id and re.match(r"^[A-Za-z0-9_.\-=]+$", run_id) and text:
        try:
            d = DATA_DIR / "run_logs"
            d.mkdir(parents=True, exist_ok=True)
            with open(d / f"{run_id}.log", "a", errors="ignore") as f:
                f.write(text)
        except OSError:
            pass
    return {"ok": True}


@router.post("/track/artifact")
async def track_artifact(request: Request):
    await request.json()
    return {"ok": True}


@router.post("/track/finish")
async def track_finish(request: Request):
    from . import novelty
    body = await request.json()
    run_id = body["run_id"]
    summary = body.get("summary", {})
    db = SessionLocal()
    run = db.query(Run).filter(Run.id == run_id).first()
    if run:
        proj = db.query(Project).first()
        metric_key = (proj.validation_metric if proj else "") or ""
        direction = (proj.metric_direction if proj else "minimize") \
            or "minimize"
        headline = _resolve_headline(run_id, summary, metric_key)
        run.headline_metric = headline
        run.ended_at = _iso()
        crashed = _is_crashed(headline, direction)
        # ─── status taxonomy (RESEARCH_IMPROVEMENT_PLAN #4) ──────────
        # The legacy taxonomy was {kept, crashed, discarded}. That made
        # "didn't crash" indistinguishable from "real progress on real
        # eval", so the council and the dashboard rewarded a 100%-acc
        # smoke task the same as a real GSM8K improvement.
        #
        # New taxonomy:
        #   success_smoke   — _probe / _smoke runs (info only; never
        #                     compared on the frontier).
        #   kept_novel      — finite metric, finished, novel hash.
        #   kept_replicate  — finite metric, finished, explicit
        #                     replicate (idea_class=REPRODUCE / seed_
        #                     replicate / run_id startswith 'seed_').
        #   discarded       — duplicate config that the duplicate
        #                     killer somehow let in (defensive — the
        #                     /api/track/run gate normally rejects
        #                     these with HTTP 409 before they ever
        #                     reach /track/finish).
        #   crashed         — non-finite metric / diverged. Unchanged.
        cfg = run.config if isinstance(run.config, dict) else {}
        if crashed:
            new_status = "crashed"
        elif novelty.is_probe_or_smoke(run_id):
            new_status = "success_smoke"
        elif novelty.is_seed_replicate(cfg, run_id):
            new_status = "kept_replicate"
        else:
            # The /api/track/run gate already rejected duplicates;
            # anything that reaches here with a finite metric is
            # genuinely novel.
            new_status = "kept_novel"
        run.status = new_status
        idea = db.query(Idea).filter(Idea.id == run.idea_id).first()
        if idea:
            idea.status = "failed" if crashed else "success"
            idea.ended_at = _iso()
        ev = Event(id=f"ev-{_rng.randrange(16**8):08x}", type="run_finished",
                   severity="warning" if crashed else "info", actor="agent",
                   message=(f"{run_id} diverged" if crashed
                            else f"{run_id} finished — {headline:.4f}"),
                   run_id=run_id, created_at=_iso())
        db.add(ev)
        db.commit()
        payload = ev.dict()
        # Default-metric audit: surface a warning Event per required key the
        # run never logged. The drawer's "All plots" section was showing
        # "(not logged)" for the seven defaults — this makes the gap loud
        # and traceable rather than silent. Probe/smoke runs are exempt
        # since they're a one-step sanity check, not a real experiment.
        if not (str(run_id).startswith("_probe")
                or str(run_id).startswith("_smoke")):
            try:
                missing = _check_required_metrics(run_id)
                _emit_missing_metric_warnings(run_id, missing)
            except Exception as e:                              # noqa: BLE001
                print(f"[track_finish] required-metric audit "
                      f"failed: {e}", flush=True)
        # Snapshot the Run attributes we'll need AFTER closing the session
        # (touching them post-close triggers a refresh and a
        # DetachedInstanceError; that's a release-gate bug the e2e catches).
        run_context = run.context
        run_paper_claim_id = run.paper_claim_id
        run_paper_figure_id = run.paper_figure_id
        run_run_name = run.run_name
        db.close()
        bus.publish("events", "event", payload)
        # immediate email if this run set a new best (notify decides)
        threading.Thread(target=notify.on_run_finished, args=(run_id,),
                         daemon=True).start()
        # Council reviews. Two paths:
        #  1) Per-run review (default OFF — was noisy and pushed the agent
        #     off working tracks). Settings.council_per_run_enabled enables.
        #  2) Strategic batch review every N=GPU-count finished runs — one
        #     reflection per parallel-run wave. This is the default.
        from . import council
        council.review_async(run_id)             # gated inside on settings
        council.note_run_finished(run_id)        # batch trigger
        # Paper-mode auto-integrate: when a paper_run finishes, push a
        # concrete prompt into the author agent's tmux so it integrates
        # the result without waiting for its next poll cycle.
        if run_context == "paper":
            try:
                _poke_author_to_integrate(run_id, headline,
                                          run_paper_claim_id,
                                          run_paper_figure_id,
                                          run_run_name)
            except Exception as e:
                print(f"[paper] poke author failed: {e}", flush=True)
    else:
        db.close()
    bus.publish("events", "runs_changed", {})
    return {"ok": True}


# ─────────────────────────── SSE streams (doc 11 D1) ───────────────────────

def _sse(topic: str) -> StreamingResponse:
    return StreamingResponse(
        bus.subscribe(topic), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/stream/metrics")
async def stream_metrics():
    return _sse("metrics")


@router.get("/stream/events")
async def stream_events():
    return _sse("events")


@router.get("/stream/gpus")
async def stream_gpus():
    return _sse("gpus")


@router.get("/stream/chat")
async def stream_chat():
    return _sse("chat")


# ──────── orchestrator: run the bundled example research project ────────────
# Used by the e2e integration test and for a hardware-free local run of the
# full autonomous loop (FakeAgent + the tiny-sgd project).

@router.post("/dev/run-example")
async def dev_run_example():
    ex = ROOT / "tests" / "example_project"
    if not ex.exists():
        return {"status": "error", "detail": "example project not found"}
    o = orchestrator.active()
    if o and o.running:
        return {"status": "already_running"}
    orchestrator.start(str(ex), name="tiny-sgd", n_slots=3,
                       metric_key="val_mse", direction="minimize")
    return {"status": "started", "project": "tiny-sgd"}


@router.get("/dev/status")
def dev_status():
    o = orchestrator.active()
    return {"running": bool(o and o.running),
            "project_id": o.project_id if o else None}


# ──────────── agent terminal: live view + chat into the session ─────────────

def _tmux_alive(session: str = "agent") -> bool:
    return subprocess.run(["tmux", "has-session", "-t", session],
                          capture_output=True).returncode == 0


@router.post("/agent/resize")
async def agent_resize(request: Request):
    """Resize a tmux pane so it matches the xterm.js dimensions.

    Without this, Claude Code (running inside tmux) renders its UI
    at whatever -x/-y we spawned tmux with (default 210x52). When the
    browser's xterm.js is narrower than that, every Claude status
    line wraps mid-character and the terminal looks like garbage.

    The frontend hooks xterm's onResize after FitAddon.fit() and
    POSTs the new (cols, rows) here. We:
      1. tmux resize-window -x cols -y rows -t <session>
      2. send Ctrl-L so Claude clears + redraws at the new size
         (its in-place spinner / status bar are positioned with
         absolute escapes that don't auto-reflow).

    Body: {"session": "agent"|"author"|<run_id>, "cols": N, "rows": N}
    """
    body = await _safe_json(request)
    sess = (body.get("session") or "agent").strip()
    try:
        cols = int(body.get("cols") or 0)
        rows = int(body.get("rows") or 0)
    except (TypeError, ValueError):
        return {"ok": False, "error": "cols/rows must be int"}
    if (not sess or len(sess) > 80 or not _SAFE_NAME.match(sess)
            or cols < 20 or cols > 500 or rows < 5 or rows > 200):
        return {"ok": False, "error": "bad session/cols/rows"}
    if not _tmux_alive(sess):
        return {"ok": False, "error": f"no tmux session: {sess}"}
    # resize-window works on the whole window (the right level for our
    # single-pane sessions). NB: do NOT pass -A here — `-A` tells tmux to
    # size the window to the LARGEST attached client and ignore the
    # explicit -x/-y, so the window stayed pinned at the 120x40 spawn
    # default and never shrank to the rail's ~76 cols. That was the
    # "Research agent terminal renders garbled / lines wrap mid-character"
    # bug: tmux at 120 cols, xterm at 76. Plain -x/-y with window-size
    # manual applies the exact dimensions.
    r = subprocess.run(
        ["tmux", "resize-window", "-t", sess, "-x", str(cols),
         "-y", str(rows)],
        capture_output=True, text=True, timeout=4)
    if r.returncode != 0:
        # Older tmux without resize-window: fall back to resize-pane.
        r = subprocess.run(
            ["tmux", "resize-pane", "-t", sess, "-x", str(cols),
             "-y", str(rows)],
            capture_output=True, text=True, timeout=4)
    # Ctrl-L → Claude redraws at the new dimensions.
    subprocess.run(["tmux", "send-keys", "-t", sess, "C-l"],
                   capture_output=True)
    # Cache for restart restoration — when the agent is respawned via
    # /api/agent/restart, RealAgent.start() will re-apply these so the
    # new pane comes up at the rail's actual width (instead of the
    # 120x40 default that produces garbled wrapping in any narrower
    # rail).
    from . import pane_stream
    pane_stream.remember_size(sess, cols, rows)
    return {"ok": True, "cols": cols, "rows": rows,
            "stderr": (r.stderr or "")[:200]}


@router.get("/url")
def public_url():
    """Return the current cloudflared public URL.

    The cloudflared quick-tunnel rotates the hostname every time it
    respawns, and users frequently lose the URL they bookmarked. This
    endpoint tails `data/cloudflared.log` for the most recent
    `https://*.trycloudflare.com` line, which is the URL the tunnel
    is currently registered under. Empty string if the log is missing
    or the tunnel hasn't started yet.

    Local-only access: this is only useful from inside the pod — the
    cloudflared tunnel itself would have to be UP for an external
    caller to reach this endpoint in the first place. Doubles as a
    debug ping ("is the backend alive and can I read the log?")."""
    import re as _re
    log = DATA_DIR / "cloudflared.log"
    try:
        txt = log.read_text(errors="ignore") if log.exists() else ""
    except OSError:
        txt = ""
    urls = _re.findall(r"https://[a-z0-9-]+\.trycloudflare\.com", txt)
    return {"url": urls[-1] if urls else "",
            "all_urls": list(dict.fromkeys(urls[-10:]))}


@router.get("/emails/status")
def emails_status():
    """Whether the user has paused outbound emails in Settings.

    The dashboard's Settings modal calls this to render the
    Pause / Resume button label correctly."""
    from . import notify
    return {"paused": notify.emails_paused()}


@router.post("/emails/pause")
async def emails_pause():
    """Pause ALL outgoing emails — digests, token failures, system
    warnings, paper-mode digests, anti-pattern alerts. Sets
    ``emails_paused: true`` on the onboarding settings row. Effect is
    instant; the next scheduler tick / send() call early-returns."""
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(Setting.key == "onboarding").first()
        if not row:
            row = Setting(key="onboarding", value={})
            db.add(row)
        cur = dict(row.value) if isinstance(row.value, dict) else {}
        cur["emails_paused"] = True
        row.value = cur
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(row, "value")
        db.commit()
    finally:
        db.close()
    return {"ok": True, "paused": True}


@router.post("/emails/resume")
async def emails_resume():
    """Re-enable outgoing emails. Counterpart of /emails/pause."""
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(Setting.key == "onboarding").first()
        if not row:
            row = Setting(key="onboarding", value={})
            db.add(row)
        cur = dict(row.value) if isinstance(row.value, dict) else {}
        cur["emails_paused"] = False
        row.value = cur
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(row, "value")
        db.commit()
    finally:
        db.close()
    return {"ok": True, "paused": False}


@router.get("/research/status")
def research_status():
    """Whether the user has paused the autonomous research loop.

    Read by the Settings modal to render the Pause / Resume button
    correctly, and by anything else that wants to know whether the
    loop is asleep (e.g. status badges)."""
    return {"paused": notify.research_paused()}


# ───────── directives.jsonl — authoritative command queue (PLAN #1) ─────

@router.get("/directives")
def directives_list():
    """List every directive, oldest first. Each is a dict per the
    schema in backend/app/directives.py. Cheap pure file read."""
    from . import directives as _d
    items = _d.read_all()
    return {"directives": items,
            "open_count": sum(1 for d in items
                              if d.get("status") == _d.STATUS_OPEN)}


@router.get("/directives/{directive_id}")
def directives_get(directive_id: str):
    """Single directive by id, or 404."""
    from fastapi.responses import JSONResponse
    from . import directives as _d
    d = _d.get(directive_id)
    if not d:
        return JSONResponse({"error": "not_found",
                              "id": directive_id}, status_code=404)
    return d


@router.post("/directives/upsert")
async def directives_upsert(request: Request):
    """Upsert one directive. Body: {"directive": {...}}.

    Returns the stored row + ``created`` flag. Validation errors yield
    HTTP 400 with the reason in ``error``."""
    from fastapi.responses import JSONResponse
    from . import directives as _d
    body = await _safe_json(request)
    d = body.get("directive") or {}
    try:
        stored, created = _d.upsert(d)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)},
                             status_code=400)
    return {"ok": True, "directive": stored, "created": created}


@router.post("/directives/{directive_id}/done")
async def directives_done(directive_id: str, request: Request):
    """Mark a directive done. Body: {"evidence": "<one-liner proof>"}.

    The evidence is preserved on the row for the dashboard's audit view.
    Returns 404 if no such id; otherwise the updated directive."""
    from fastapi.responses import JSONResponse
    from . import directives as _d
    body = await _safe_json(request)
    evidence = str(body.get("evidence") or "")
    d = _d.close(directive_id, evidence=evidence,
                  status=_d.STATUS_DONE)
    if not d:
        return JSONResponse({"error": "not_found",
                              "id": directive_id}, status_code=404)
    return d


# ───────── halt: hard-stop authority (PLAN #6) ───────────────────────────

@router.post("/halt")
async def halt_research(request: Request):
    """Hard-halt all research runs. Body: {"reason": "..."}.

    Effects:
      - Sets the ``research_halted`` setting (separate from
        ``research_paused``).
      - Blocks every subsequent /api/track/run including _probe/_smoke
        names — the gate returns 423.
      - Emits a red-banner Event so the dashboard shows the reason.

    Idempotent: calling /halt while halted just updates the reason. Use
    /halt/resume (admin-only via passcode) to lift the halt."""
    body = await _safe_json(request)
    reason = str(body.get("reason") or "no reason provided")[:500]
    notify.set_research_halted(True, reason=reason)
    return {"ok": True, "halted": True, "reason": reason}


@router.post("/halt/resume")
async def halt_resume(request: Request):
    """Lift a hard halt (admin-only via passcode).

    Body may include {"passcode": "..."} which must match the onboarding
    passcode; the dashboard's resume button submits this. Returns 401 if
    the passcode is wrong."""
    from fastapi.responses import JSONResponse
    body = await _safe_json(request)
    submitted = str(body.get("passcode") or "")
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(Setting.key == "onboarding").first()
        cfg = dict(row.value) if row and isinstance(row.value, dict) else {}
    finally:
        db.close()
    real = str(cfg.get("passcode") or "")
    # If no passcode is set, allow resume (single-user dev path).
    if real and submitted != real:
        return JSONResponse({"ok": False, "error": "bad_passcode"},
                             status_code=401)
    notify.set_research_halted(False)
    return {"ok": True, "halted": False}


@router.get("/halt/status")
def halt_status():
    """Current halt state: {"halted": bool, "reason": "..."}."""
    halted, reason = notify.research_halted()
    return {"halted": halted, "reason": reason}


@router.get("/research_health")
def research_health():
    """Stuck-detector pill payload for the dashboard header.

    Returns the current health state of the research loop:
      {"state": "healthy|nagged|stalled|looping|dry",
       "details": {top_directive, consecutive_unimplemented_reviews,
                    collision_rate, kept_novel_in_window, ...},
       "reason": "<one-line human summary>"}

    DEPRECATED (PR 6 of state-control rewrite, 2026-06-05). Use
    ``GET /api/health`` — this endpoint now wraps the new
    HealthSnapshot in the legacy ``{state, details, reason}`` shape so
    older frontends keep working until the next session reload picks
    up the new app.js. The state column maps phase → legacy enum:

        bootstrap → "setting_up"
        watching_runs + no issues → "healthy"
        any issue → "needs_direction"
    """
    try:
        from .health import service as _hs
        snap = _hs.compute()
        phase = snap.phase.phase
        issues = snap.issues or []
        top = issues[0] if issues else None
        if phase == "bootstrap":
            state = "setting_up"
        elif top is not None:
            state = "needs_direction"
        else:
            state = "healthy"
        return {
            "state": state,
            "details": {
                "phase": phase,
                "phase_fallback_used": snap.phase.fallback_used,
                "n_issues": len(issues),
                "issues": [i.as_dict() for i in issues[:5]],
            },
            "reason": snap.summary,
        }
    except Exception as e:                                  # noqa: BLE001
        return {"state": "healthy",
                "details": {"error": str(e)[:200]},
                "reason": "health probe failed; defaulting to healthy"}


@router.post("/research/conclude")
async def research_conclude(request: Request):
    """The agent declares the research purpose conclusively answered.

    Body:
      {
        "summary": "<1-paragraph summary of what was learned>",
        "answer_to_purpose": "YES_CONCLUSIVELY|YES_PARTIAL|NO",
        "evidence": ["run_id_1", "run_id_2", ...],
        "recommendation": "WRITE_PAPER|NEED_ORTHOGONAL_DIRECTION|NEED_MORE_DATA"
      }

    Effects:
      - Persists conclude_summary, conclude_at, etc. on the Setting row
        ``research_conclusion`` with status ``pending``.
      - Emits a Event ``research_concluded`` (severity=info).
      - Triggers an async council completion-review. The reviewers
        decide APPROVED / REJECTED / NEEDS_MORE; the verdict lands back
        on the same Setting row.

    The dashboard then surfaces ``awaiting_completion_review`` until the
    council returns. After APPROVED the state flips to ``complete`` and
    the user can move to paper mode. After REJECTED the council's
    ``missing_evidence`` list tells the agent what to do next.

    This is the only legal exit out of "the queue is empty" besides
    upserting a new SCIENCE directive — the agent prompt forbids idle.
    """
    from fastapi.responses import JSONResponse
    from . import council as _c
    body = await _safe_json(request)
    summary = str(body.get("summary") or "").strip()
    answer = str(body.get("answer_to_purpose") or "").strip().upper()
    evidence = body.get("evidence") or []
    recommendation = str(body.get("recommendation") or "").strip().upper()
    if not summary:
        return JSONResponse(
            {"ok": False, "error": "summary is required"},
            status_code=400)
    if answer not in ("YES_CONCLUSIVELY", "YES_PARTIAL", "NO"):
        return JSONResponse(
            {"ok": False, "error":
             "answer_to_purpose must be YES_CONCLUSIVELY|YES_PARTIAL|NO"},
            status_code=400)
    if not isinstance(evidence, list):
        return JSONResponse(
            {"ok": False, "error": "evidence must be a list of run_ids"},
            status_code=400)
    if (recommendation and recommendation not in
            ("WRITE_PAPER", "NEED_ORTHOGONAL_DIRECTION", "NEED_MORE_DATA")):
        return JSONResponse(
            {"ok": False, "error":
             ("recommendation must be WRITE_PAPER|"
              "NEED_ORTHOGONAL_DIRECTION|NEED_MORE_DATA")},
            status_code=400)
    # Emit Event so it shows up in the Summary feed live.
    db = SessionLocal()
    try:
        db.add(Event(
            id="ev-" + os.urandom(4).hex(),
            type="research_concluded", severity="info", actor="agent",
            message=(f"Agent declared the research purpose "
                     f"{answer.replace('_', ' ').lower()}: "
                     f"{summary[:180]}")[:280],
            created_at=dt.datetime.now(dt.timezone.utc).isoformat()))
        db.commit()
    finally:
        db.close()
    state = _c.review_completion_async(
        evidence_run_ids=[str(x) for x in evidence],
        summary=summary,
        answer_to_purpose=answer,
        recommendation=recommendation)
    try:
        bus.publish("events", "research_health", {})
    except Exception:
        pass
    return {"ok": True, "conclusion": state}


@router.get("/research/conclusion")
def research_conclusion_get():
    """Snapshot of the current research-conclusion state.

    Returns ``{"status": "none|pending|approved|rejected", "summary": ...,
    "answer_to_purpose": ..., "evidence": [...], "recommendation": ...,
    "council_verdict": {...}, "conclude_at": "...", ...}``. The dashboard
    polls this to render the "Research complete" banner / "council is
    reviewing" pill / write-the-paper CTA.
    """
    from . import council as _c
    return _c.conclusion_state()


@router.post("/research/conclusion/clear")
async def research_conclusion_clear(request: Request):
    """Operator-only: clear the current conclusion (rejects it).

    Body: {"reason": "<optional one-line reason>",
            "blocker_directive": {<optional directive dict>}}.

    When the operator hits "Reject conclusion" on the dashboard they may
    optionally include a BLOCKER directive describing what was wrong;
    we upsert it so the agent picks it up on the next tick. The
    conclusion state is wiped (status=none) so the loop returns to its
    pre-conclusion state."""
    from . import council as _c
    from . import directives as _d
    body = await _safe_json(request)
    reason = str(body.get("reason") or "").strip()
    blocker = body.get("blocker_directive") or None
    out = _c.clear_conclusion(reason=reason)
    upserted = None
    if isinstance(blocker, dict) and blocker:
        try:
            stored, _created = _d.upsert(blocker)
            upserted = stored
        except Exception as e:                              # noqa: BLE001
            print(f"[research/conclusion/clear] blocker upsert failed: "
                  f"{e}", flush=True)
    try:
        bus.publish("events", "research_health", {})
    except Exception:
        pass
    return {"ok": True, "conclusion": out, "blocker": upserted}


@router.post("/research/pause")
async def research_pause():
    """Pause the autonomous research loop. Sets ``research_paused: true``
    on the onboarding settings row AND interrupts the running agent /
    author tmux sessions so Claude Code stops burning tokens RIGHT NOW.

    Effects (all read the same flag — single source of truth):
      - Orchestrator: skips launching any new run while paused; in-flight
        runs keep going (kill them with /api/reset if you want a full
        stop).
      - PI agent: skips its hourly nudge cycle, so the research / author
        agent isn't pestered while the human is debugging.
      - /api/track/run: rejects new run registrations with HTTP 423
        Locked, same shape as the council bless gate.
      - Council: every _call_reviewer / deliberate / strategic_review /
        bless_async path early-returns, so we don't fire Gemini / GPT
        API calls while paused.
      - tmux agent + tmux author: receives Ctrl-C twice (Claude Code
        halts on the second Ctrl-C) + a literal pause message in the
        prompt buffer.
    """
    res = notify.set_research_paused(True)
    return {"ok": True, "paused": True,
            "sessions_interrupted": res.get("sessions_interrupted", [])}


@router.post("/research/resume")
async def research_resume():
    """Re-enable the autonomous research loop. Counterpart of
    /research/pause — orchestrator resumes launching runs on its next
    tick, PI agent resumes nudging on its next cadence, /api/track/run
    accepts runs again, council un-mutes, and a resume message is typed
    into the agent / author tmux sessions so the agent picks up where
    it left off."""
    res = notify.set_research_paused(False)
    return {"ok": True, "paused": False,
            "sessions_interrupted": res.get("sessions_interrupted", [])}


@router.get("/agent/raw")
def agent_raw_stream(session: str = "agent", offset: int = 0):
    """Byte-offset incremental read of a tmux session's RAW pane bytes.

    This is what the rail xterm.js subscribes to so the embedded terminal
    feels like a real terminal: each call returns only the bytes emitted
    since the caller's last ``offset``. xterm.js calls ``t.write(chunk)``
    on the returned bytes — ANSI escapes, colors, cursor moves, spinners
    all render natively because xterm is re-parsing the program's actual
    output (mirrored via ``tmux pipe-pane``).

    Response:
        {
          "chunk":  "<base64-encoded raw bytes>",
          "offset": <new offset to send next time>,
          "size":   <current total file size>,
          "alive":  <bool — tmux session still running?>,
          "rotated": <bool — true if we resynced from 0>
        }

    The client sends back ``offset`` on the next request to resume from
    that point. If the file shrank (e.g. session restarted, raw file
    truncated), the server resets ``offset`` to 0 and sets
    ``rotated: true`` so xterm.js can call ``t.reset()`` first.
    """
    from . import pane_stream
    import base64
    if not session or len(session) > 80 or not _SAFE_NAME.match(session):
        return {"error": "bad session"}
    pre_size = pane_stream.size(session)
    rotated = bool(offset and offset > pre_size)
    chunk, new_off, size = pane_stream.read_range(session, offset)
    return {
        "chunk": base64.b64encode(chunk).decode("ascii"),
        "offset": new_off,
        "size": size,
        "alive": _tmux_alive(session),
        "rotated": rotated,
    }


@router.get("/agent/terminal")
def agent_terminal():
    """Live contents of the agent's tmux session — drives the rail Live tab."""
    r = subprocess.run(
        ["tmux", "capture-pane", "-t", "agent", "-p", "-S", "-3000"],
        capture_output=True, text=True)
    text = r.stdout if r.returncode == 0 else ""
    if not text.strip():                       # no live pane — fall back to log
        logs = [p for p in glob.glob(
                str(WORKSPACE_DIR / "*" / "agent.log"))
                if os.path.exists(p) and os.path.getsize(p) > 0]
        logs.sort(key=os.path.getmtime)
        if logs:
            try:
                text = open(logs[-1], errors="ignore").read()[-16000:]
            except OSError:
                pass
    return {"text": text or "(no agent session yet)",
            "alive": _tmux_alive("agent")}


@router.post("/pi/run")
async def pi_run_now(request: Request):
    """Trigger an immediate PI cycle. Returns the decision dict so the UI /
    a test script can verify the PI is wired up correctly."""
    from . import pi as _pi
    out = _pi.cycle(force=True)
    return out or {"status": "skipped"}


@router.post("/council/strategic")
async def council_strategic_now(request: Request):
    """Trigger an immediate strategic review on the N most-recent finished
    runs (or a caller-supplied list). Test endpoint."""
    from . import council as _c
    body = await request.json()
    ids = body.get("run_ids") or []
    if not ids:
        n = int(body.get("n") or _c._strategic_threshold(_c._settings()))
        db = SessionLocal()
        try:
            runs = (db.query(Run)
                    .filter(Run.status.in_(["kept", "discarded", "crashed",
                                            "failed", "success"]))
                    .order_by(Run.ended_at.desc())
                    .limit(n).all())
            ids = [r.id for r in runs]
        finally:
            db.close()
    out = _c.strategic_review(ids)
    if out:
        _c._persist_strategic(ids, out)
        _c._apply_to_ideas_md(out)
    return out or {"status": "no_review"}


@router.post("/council/review")
async def council_review_now(request: Request):
    """Trigger a council review on a specific run_id (synchronous)."""
    from . import council as _c
    body = await request.json()
    rid = (body.get("run_id") or "").strip()
    if not rid:
        return {"status": "error", "detail": "run_id required"}
    out = _c.deliberate(rid)
    if out:
        _c._persist(rid, out)
        _c._apply_to_ideas_md(out)
    return out or {"status": "no_review"}


@router.post("/agent/send")
async def agent_send(request: Request):
    """Type a message into the agent's Claude Code tmux session."""
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "empty"}
    if not _tmux_alive("agent"):
        return {"ok": False, "error": "no agent session is running"}
    subprocess.run(["tmux", "send-keys", "-t", "agent", "-l", text],
                   capture_output=True)
    subprocess.run(["tmux", "send-keys", "-t", "agent", "Enter"],
                   capture_output=True)
    db = SessionLocal()
    msg = ChatMessage(id=f"cm-{_rng.randrange(16**8):08x}", role="researcher",
                      content=text, created_at=_iso())
    db.add(msg)
    db.commit()
    payload = msg.dict()
    db.close()
    bus.publish("chat", "chat", payload)
    return {"ok": True}


@router.get("/agent/oauth_url")
def agent_oauth_url():
    """Extract a Claude Code OAuth URL from the agent's tmux pane,
    UN-WRAPPED across terminal line breaks.

    tmux hard-wraps long URLs across the pane width (default 210
    columns), so a 600-char OAuth URL spans 3 lines with `\n` mid-token.
    `capture-pane -J` joins wrapped lines back together, which lets us
    recover the full URL the user actually needs to click. The boot
    overlay polls this endpoint when it detects the OAuth state in the
    regular /agent/terminal output."""
    try:
        r = subprocess.run(
            ["tmux", "capture-pane", "-t", "agent", "-p", "-J",
             "-S", "-3000"],
            capture_output=True, text=True, timeout=8)
        text = r.stdout if r.returncode == 0 else ""
    except Exception as e:                              # noqa: BLE001
        return {"url": "", "error": str(e)}
    # The OAuth URL starts at one of these patterns and continues
    # uninterrupted to a terminator. With -J, line wraps are already
    # collapsed, so a normal greedy scan to the next whitespace works.
    import re as _re
    m = _re.search(
        r"https://(?:claude\.com/cai/oauth|platform\.claude\.com/oauth"
        r"|console\.anthropic\.com|claude\.com/oauth)[^\s'\"]+",
        text)
    if not m:
        return {"url": ""}
    return {"url": m.group(0)}


@router.post("/agent/keys")
async def agent_keys(request: Request):
    """Forward raw keystrokes to a tmux session — used by the xterm.js
    terminal in the rail to make the embedded terminal feel real.

    Body:
        {
          "session": "agent" | "author" | <run_id>,   (default: agent)
          "data":    "<raw bytes>"                     (xterm onData payload)
        }

    Special-case Enter (\\r → tmux 'Enter'), Backspace (\\x7f → 'BSpace'),
    Tab (\\t → 'Tab'), Esc (\\x1b → 'Escape'), arrow keys (\\x1b[A/B/C/D),
    Ctrl-* sequences. Everything else goes through with ``-l`` (literal)
    so multi-char paste is preserved verbatim.
    """
    body = await _safe_json(request)
    sess = (body.get("session") or "agent").strip()
    data = body.get("data") or ""
    if not sess or len(sess) > 80 or not _SAFE_NAME.match(sess):
        return {"ok": False, "error": "bad session"}
    if not isinstance(data, str) or len(data) > 16384:
        return {"ok": False, "error": "bad data"}
    if not _tmux_alive(sess):
        return {"ok": False, "error": f"no tmux session: {sess}"}
    # Map known control sequences to tmux send-keys symbolic names.
    # Anything not mapped goes through with -l (literal) so URLs, paste
    # buffers, and unicode all reach tmux intact.
    SPECIALS = {
        "\r": "Enter", "\n": "Enter",
        "\x7f": "BSpace", "\x08": "BSpace",
        "\t": "Tab",
        "\x1b": "Escape",
        "\x1b[A": "Up", "\x1b[B": "Down",
        "\x1b[C": "Right", "\x1b[D": "Left",
        "\x1b[H": "Home", "\x1b[F": "End",
        "\x1b[3~": "DC",   # Delete
        "\x1b[5~": "PPage", "\x1b[6~": "NPage",
    }
    if data in SPECIALS:
        subprocess.run(["tmux", "send-keys", "-t", sess, SPECIALS[data]],
                       capture_output=True)
        return {"ok": True, "kind": "special"}
    # Ctrl-A..Ctrl-Z (0x01..0x1A) → C-a..C-z
    if len(data) == 1 and 1 <= ord(data) <= 26:
        sym = "C-" + chr(ord(data) + ord('a') - 1)
        subprocess.run(["tmux", "send-keys", "-t", sess, sym],
                       capture_output=True)
        return {"ok": True, "kind": "ctrl"}
    # Literal multi-char paste: send the whole buffer with -l so tmux
    # does NOT interpret it as keysym names (e.g., "Enter" inside the
    # paste would otherwise fire an Enter).
    subprocess.run(["tmux", "send-keys", "-t", sess, "-l", data],
                   capture_output=True)
    return {"ok": True, "kind": "literal"}


@router.post("/agent/paste_oauth")
async def agent_paste_oauth(request: Request):
    """Type an OAuth callback code into the Research Agent's tmux pane.

    Claude Code on a fresh ~/.claude sometimes falls back to its OAuth
    flow ("Paste code here if prompted >") instead of using
    ANTHROPIC_API_KEY. The dashboard's boot overlay detects this state
    and offers a paste-back input; this endpoint takes the OAuth code
    the user copied from their browser and `tmux send-keys` it into
    the agent session, followed by Enter."""
    body = await _safe_json(request)
    code = (body.get("code") or "").strip()
    if not code or len(code) > 4096:
        return {"ok": False, "error": "empty or oversized code"}
    if not _tmux_alive("agent"):
        return {"ok": False, "error": "no agent session is running"}
    subprocess.run(["tmux", "send-keys", "-t", "agent", "-l", code],
                   capture_output=True)
    subprocess.run(["tmux", "send-keys", "-t", "agent", "Enter"],
                   capture_output=True)
    return {"ok": True}


@router.post("/agent/restart")
async def agent_restart(request: Request):
    """Re-launch an agent's tmux session.

    Now dispatches on body.name (default 'research' for backward compat):
      - "research" / "agent"   → research agent (legacy default)
      - "author"               → paper-mode Author Agent
      - "both"                 → both, in parallel

    Previously this endpoint silently ignored `name` and always restarted
    the research agent. That made the frontend's "restart author"
    button restart the wrong session — Francois hit this on 2026-06-06
    after the paper-mode rebuild. /api/paper/author/restart remains as
    a compat alias for any callers that still hit it.
    """
    try:
        body = await request.json() if request.headers.get(
            "content-type", "").startswith("application/json") else {}
    except Exception:
        body = {}
    name = (body.get("name") or "research").strip().lower()
    if name in ("agent", "research", "researcher"):
        targets = ["research"]
    elif name == "author":
        targets = ["author"]
    elif name == "both":
        targets = ["research", "author"]
    else:
        return {"ok": False,
                "error": f"unknown agent name '{name}'; "
                          "expected one of: research, author, both"}
    results: dict = {}
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(
            Setting.key == "onboarding").first()
        cfg = dict(row.value) if row and isinstance(row.value, dict) else {}
    finally:
        db.close()
    if "research" in targets:
        if not (cfg.get("claude_token")
                or os.environ.get("ARUI_CLAUDE_BIN")):
            results["research"] = {"ok": False,
                                    "error": "no Claude token configured — "
                                             "onboarding not complete"}
        else:
            subprocess.run(["tmux", "kill-session", "-t", "agent"],
                           capture_output=True, timeout=5)
            try:
                results["research"] = {"ok": True,
                                        "info": realrun.start_real(cfg)}
            except Exception as e:                              # noqa: BLE001
                results["research"] = {"ok": False, "error": str(e)}
    if "author" in targets:
        try:
            from . import author_agent
            subprocess.run(["tmux", "kill-session", "-t", "author"],
                           capture_output=True, timeout=5)
            results["author"] = {"ok": True, "info": author_agent.start()}
        except Exception as e:                                  # noqa: BLE001
            results["author"] = {"ok": False, "error": str(e)}
    ok = all(v.get("ok") for v in results.values())
    return {"ok": ok, "targets": targets, "results": results}


@router.post("/paper/author/restart")
async def paper_author_restart():
    """Re-launch the author agent (paper mode counterpart of /agent/restart)."""
    from . import author_agent
    subprocess.run(["tmux", "kill-session", "-t", "author"],
                   capture_output=True, timeout=5)
    info = author_agent.start()
    return {"ok": True, "info": info}


# ──────────── reset + onboarding ─────────────────────────────────────────────

@router.post("/reset")
async def reset_all():
    """Wipe everything and return the instance to the onboarding state."""
    orchestrator.stop()
    realrun.stop()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    metrics.reset()
    return {"status": "reset"}


@router.get("/onboarding")
def get_onboarding(db: Session = Depends(get_session)):
    """Return the saved onboarding config with secret fields masked.

    SECURITY: this endpoint is served over the public cloudflared tunnel,
    so it must NEVER return raw API keys. Internal callers (council, PI,
    lit-agent, agent restart) read the secrets straight from the DB
    Setting row via SessionLocal — they do not depend on this HTTP
    response — so masking here has no functional cost. Masking uses the
    same SECRET_FIELDS set as GET /settings (defined just below); the
    name resolves at call time, by which point the module is imported.
    """
    row = db.query(Setting).filter(Setting.key == "onboarding").first()
    cfg = dict(row.value) if row and isinstance(row.value, dict) else {}
    return {k: ("••••••••" if (k in SECRET_FIELDS and v) else v)
            for k, v in cfg.items()}


@router.get("/onboarding/defaults")
def onboarding_defaults():
    """Defaults for editable onboarding fields. The frontend pre-fills these
    so the user sees how the agent is configured by default and can override."""
    # Lazy imports so circular-import safety: council and pi import db/models.
    from . import council as _c
    from . import pi as _p
    out = {"agent_instructions": realrun.DEFAULT_AGENT_INSTRUCTIONS}
    out.update(_c.DEFAULTS)
    out.update(_p.DEFAULTS)
    # research agent (Claude) default model
    out.setdefault("research_agent_model", "claude-opus-4-6")
    # Free-text "kill any run that's been training longer than this"
    # policy. Parsed in backend/app/kill_criteria.py — see that module
    # for the supported phrasings (the default below is the simplest).
    out.setdefault("kill_criteria", "1 hour")
    return out


# Secret / token fields are blanked in /api/settings responses so the UI
# doesn't display the saved value. PUTting blank means "leave unchanged".
SECRET_FIELDS = {"claude_token", "gemini_token", "openai_token",
                 "github_token", "gmail_app_pw", "passcode"}


@router.get("/settings")
def get_settings(db: Session = Depends(get_session)):
    """Return the saved onboarding config (= live settings), with secret
    fields masked. The Settings tab uses this to populate its form."""
    row = db.query(Setting).filter(Setting.key == "onboarding").first()
    cfg = dict(row.value) if row and isinstance(row.value, dict) else {}
    masked = {k: ("••••••••" if (k in SECRET_FIELDS and v) else v)
              for k, v in cfg.items()}
    return masked


@router.put("/settings")
async def put_settings(request: Request):
    """Merge updates into the onboarding config. Secret fields are only
    updated when the user provides a non-empty, non-mask value (blanks
    mean 'leave alone'). Returns the new (masked) settings."""
    updates = await request.json()
    if not isinstance(updates, dict):
        return {"status": "error", "detail": "expected an object"}
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(Setting.key == "onboarding").first()
        cur = dict(row.value) if row and isinstance(row.value, dict) else {}
        for k, v in updates.items():
            # don't clobber a saved secret with a blank or mask
            if k in SECRET_FIELDS:
                if not v or str(v).strip() in ("", "••••••••"):
                    continue
            cur[k] = v
        if row:
            row.value = cur
        else:
            db.add(Setting(key="onboarding", value=cur))
        db.commit()
    finally:
        db.close()
    # Sync newly-saved tokens into os.environ so council/PI/lit-agent
    # pick them up immediately — no backend restart required.
    try: _apply_tokens_to_env()
    except Exception as e:                              # noqa: BLE001
        print(f"[api] apply_tokens_to_env error: {e}", flush=True)
    return {"status": "ok"}


@router.get("/diag")
def diag():
    """Self-diagnostic for deployment debugging. Tells you in one call
    whether the running code has the recent UI changes, whether Claude
    Code's settings.json is pre-seeded, and what the agent's tmux is
    showing right now. Open in a browser or curl — both work.

    Public (auth gate excluded) so you can hit it BEFORE figuring out
    the passcode."""
    import os as _os
    out: dict = {}
    # 1. git sha of what's running on this node
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"],
                           cwd=str(ROOT), capture_output=True, text=True,
                           timeout=4)
        out["git_sha"] = r.stdout.strip()[:12] if r.returncode == 0 else None
        r = subprocess.run(["git", "log", "-1", "--format=%s"],
                           cwd=str(ROOT), capture_output=True, text=True,
                           timeout=4)
        out["git_subject"] = r.stdout.strip()[:120] if r.returncode == 0 else ""
    except Exception as e:                              # noqa: BLE001
        out["git_error"] = str(e)
    # 2. does the served app.js have the new xterm wiring?
    try:
        appjs = (ROOT / "backend" / "app" / "static" / "app.js").read_text()
        out["app_js_size"] = len(appjs)
        out["has_xterm"] = "createRailTerm" in appjs
        out["has_xterm_apiwiring"] = "/agent/keys" in appjs
        # /api/agent/raw streaming wiring — the upgrade that gives the
        # rail terminal real ANSI rendering + low-latency typing.
        out["has_raw_stream"] = ("/agent/raw" in appjs
                                 and "startStream" in appjs)
        idx = (ROOT / "backend" / "app" / "static" / "index.html").read_text()
        out["index_has_xterm_cdn"] = "xterm@" in idx
        out["index_cache_bust"] = (idx.split("app.js?v=", 1)[1].split('"', 1)[0]
                                    if "app.js?v=" in idx else None)
    except Exception as e:                              # noqa: BLE001
        out["static_error"] = str(e)
    # 3. Claude config pre-seeded?
    claude_files = {}
    for p in (_os.path.expanduser("~/.claude.json"),
              _os.path.expanduser("~/.claude/settings.json")):
        try:
            if _os.path.exists(p):
                with open(p) as f:
                    txt = f.read()
                claude_files[p] = {
                    "size": len(txt),
                    # Negative-signal check: apiKeyHelper should NOT be
                    # present after our scrub (Claude 2.1.159 warns
                    # when both apiKeyHelper and ANTHROPIC_API_KEY env
                    # are set). Surfaces as True if a stale config
                    # still has it.
                    "has_stale_apiKeyHelper": "apiKeyHelper" in txt,
                    "has_bypass_accepted": (
                        "bypassPermissionsModeAccepted" in txt
                        or "dangerouslySkipPermissionsModeAccepted" in txt),
                    "has_approved_key_truncation": (
                        "customApiKeyResponses" in txt
                        and "approved" in txt),
                }
            else:
                claude_files[p] = {"missing": True}
        except Exception as e:                          # noqa: BLE001
            claude_files[p] = {"error": str(e)}
    out["claude_config"] = claude_files
    # 4. agent tmux state — captured with -J so wrapped URLs are intact
    try:
        r = subprocess.run(
            ["tmux", "capture-pane", "-t", "agent", "-p", "-J", "-S", "-40"],
            capture_output=True, text=True, timeout=4)
        out["agent_tmux_alive"] = (r.returncode == 0)
        out["agent_tmux_tail"] = (r.stdout or "")[-2400:]
    except Exception as e:                              # noqa: BLE001
        out["agent_tmux_error"] = str(e)
    # 5. per-session raw byte stream sizes — these are what the rail
    # xterm.js subscribes to. Non-zero == pipe-pane is plumbed.
    try:
        from . import pane_stream
        out["pane_stream"] = {
            "agent":  pane_stream.size("agent"),
            "author": pane_stream.size("author"),
        }
    except Exception as e:                              # noqa: BLE001
        out["pane_stream_error"] = str(e)
    # 6. anthropic key visible to the backend?
    out["env_has_anthropic_key"] = bool(_os.environ.get("ANTHROPIC_API_KEY"))
    out["env_has_gemini_key"]    = bool(_os.environ.get("GEMINI_API_KEY"))
    out["env_has_openai_key"]    = bool(_os.environ.get("OPENAI_API_KEY"))
    return out


@router.get("/passcode/check")
def passcode_check(request: Request):
    """The login screen polls this to know whether a passcode is set
    AND whether the current request already presents a valid one.

    enabled=False → no passcode set; UI shows nothing.
    enabled=True, authed=True → user already has the cookie/header/?p=
    enabled=True, authed=False → UI shows the password prompt."""
    from . import auth as _auth
    enabled = _auth.is_enabled()
    if not enabled:
        return {"enabled": False, "authed": True}
    supplied = _auth._extract_passcode(request)
    return {"enabled": True,
            "authed": bool(supplied and supplied == _auth._saved_passcode())}


@router.post("/passcode/login")
async def passcode_login(request: Request):
    """Validate a passcode. On success, set the auth cookie. UI calls this
    from the login screen with {"passcode": "..."}."""
    from fastapi.responses import JSONResponse
    from . import auth as _auth
    body = await _safe_json(request)
    supplied = str(body.get("passcode") or "").strip()
    ok, msg = _auth.login(request, supplied)
    resp = JSONResponse({"ok": ok, "detail": msg})
    if ok and _auth.is_enabled():
        resp.set_cookie(
            _auth.COOKIE_NAME, supplied,
            max_age=60 * 60 * 24 * 30, httponly=True,
            samesite="lax", path="/")
    return resp


@router.post("/passcode/logout")
def passcode_logout():
    """Clear the cookie. The user will be challenged again on next nav."""
    from fastapi.responses import JSONResponse
    from . import auth as _auth
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(_auth.COOKIE_NAME, path="/")
    return resp


@router.post("/onboarding/validate_tokens")
async def onboarding_validate_tokens(request: Request):
    """Probe each provider's auth endpoint with the user's tokens IN
    PARALLEL and report which ones are good before we actually start
    the agent. Returns a dict keyed by token name:
        {claude:  {ok, detail, latency_ms, skipped?},
         openai:  {...},
         gemini:  {...},
         github:  {...},
         gmail:   {...}}

    A token that's empty / not configured returns ok=True + skipped=True
    so the frontend can render it grey instead of red. Required tokens
    (Claude) that come back as ok=False should block the launch; optional
    tokens (Gemini, OpenAI, Gmail, GitHub) can warn but proceed."""
    from . import token_check
    body = await _safe_json(request)
    return token_check.check_all(body or {})


@router.post("/onboarding")
async def post_onboarding(request: Request):
    """Save the onboarding config and register the project.

    This does NOT run anything and shows NO demo data. The engine that
    actually researches the configured project — a real Claude Code agent on
    the GPUs (RealAgent) — is not built yet, so the dashboard stays honestly
    empty until a real agent produces real experiments.
    """
    from fastapi.responses import JSONResponse
    from . import auth as _auth
    cfg = await request.json()
    db = SessionLocal()
    row = db.query(Setting).filter(Setting.key == "onboarding").first()
    if row:
        row.value = cfg
    else:
        db.add(Setting(key="onboarding", value=cfg))
    db.commit()
    db.close()
    # Push the freshly-saved tokens into os.environ BEFORE we spawn the
    # research agent. Without this, council and PI cycles can't find the
    # keys and silently skip — the symptom that brought you here:
    #     [pi] no API key for gemini-2.5-pro; skipping cycle
    try: _apply_tokens_to_env()
    except Exception as e:                              # noqa: BLE001
        print(f"[api] apply_tokens_to_env error: {e}", flush=True)

    # If the user set a passcode in this same submission, the passcode
    # gate would IMMEDIATELY start 401'ing every subsequent dashboard
    # call (including the boot-overlay polls), and the user would see
    # 'The agent never started' even though it's running fine. Set the
    # auth cookie on this response so the frontend continues as the
    # authenticated user without needing a second login round-trip.
    new_passcode = (cfg.get("passcode") or "").strip()

    def _maybe_set_cookie(resp):
        if new_passcode:
            resp.set_cookie(
                _auth.COOKIE_NAME, new_passcode,
                max_age=60 * 60 * 24 * 30, httponly=True,
                samesite="lax", path="/")
        return resp

    # a Claude token (or the test hook) -> launch the real autonomous agent
    token = (cfg.get("claude_token") or "").strip()
    if token or os.environ.get("ARUI_CLAUDE_BIN"):
        # Scoping gate (Phase 0): instead of spawning the research agent
        # immediately, run a literature review + direction-confirmation pass
        # first. start_real() is deferred until scoping.confirm()/skip().
        from . import scoping
        if scoping.gate_enabled():
            scoping.start(cfg)
            return _maybe_set_cookie(JSONResponse({"status": "scoping"}))
        realrun.start_real(cfg)
        return _maybe_set_cookie(JSONResponse({"status": "started"}))

    # otherwise just register the project; dashboard stays honestly empty
    db = SessionLocal()
    if not db.query(Project).first():
        metric = (cfg.get("metric") or "metric").strip()
        # Heuristic for higher-is-better vs lower-is-better. The
        # explicit list is for the dropdown's well-known names, but
        # users also paste custom metrics like ``gsm8k_test_acc`` or
        # ``squad_em`` — we detect the family via substring so the
        # dashboard's "↑ best run" arrow points the right way out of
        # the box. Fallback is "minimize" (loss-like) which matches
        # the original behaviour.
        # Normalize whitespace + dashes to underscores BEFORE token
        # substring check — users paste metrics in many shapes:
        #     "gsm8k_val_acc", "GSM8K Val Acc", "gsm-8k val acc"
        # all need to resolve the same way. Without this normalization,
        # bare "acc" at the end of a tokenized phrase wouldn't match
        # any of _acc / acc_ / acc@.
        _ml = re.sub(r"[\s\-]+", "_", metric.strip().lower())
        _maximize_tokens = (
            "accuracy", "_acc", "acc_", "acc@",      # accuracy variants
            "f1", "exact_match", "em", "_em",        # NLP scores
            "bleu", "rouge", "meteor", "chrf",       # MT / summarization
            "score", "reward",                       # generic
            "auc", "map", "ndcg", "hit", "mrr",      # IR / ranking
            "pass@",                                 # code-eval
            "win", "elo",                            # competitive eval
        )
        _minimize_tokens = (
            "loss", "perplexity", "ppl", "error",
            "rmse", "mse", "mae", "bpb", "bpc",
            "fid", "kid",                            # generative
            "divergence", "regret",
        )
        if any(t in _ml for t in _minimize_tokens):
            direction = "minimize"
        elif any(t in _ml for t in _maximize_tokens):
            direction = "maximize"
        else:
            direction = "minimize"                   # original default
        db.add(Project(
            id="proj-" + (cfg.get("repo_name") or "project"),
            name=cfg.get("repo_name") or "project",
            purpose=cfg.get("purpose", ""),
            validation_metric=metric, metric_direction=direction,
            status="awaiting agent", gpu_count=0, created_at=_iso()))
        db.commit()
    db.close()
    return _maybe_set_cookie(JSONResponse({"status": "configured"}))


# ──────────── scoping gate (Phase 0: lit review + direction confirm) ────────
@router.get("/scope/status")
def scope_status():
    from . import scoping
    return scoping.state_get()


@router.post("/scope/start_preview")
async def scope_start_preview(request: Request):
    """Isolated test entry point: run the scoping sweep for an arbitrary
    purpose WITHOUT touching onboarding, the live workspace, or start_real.
    confirm/skip become dry-runs while in preview."""
    from . import scoping
    body = await request.json()
    cfg = {"purpose": body.get("purpose", ""),
           "metric": body.get("metric", ""),
           "seed_ideas": body.get("seed_ideas", ""),
           "repo_name": body.get("repo_name", "scope_preview")}
    return scoping.start(cfg, preview=True)


@router.post("/scope/chat")
async def scope_chat_ep(request: Request):
    from . import scoping
    body = await request.json()
    return scoping.chat(body.get("text", ""))


@router.post("/scope/finalize")
async def scope_finalize_ep(request: Request):
    from . import scoping
    return scoping.finalize()


@router.post("/scope/confirm")
async def scope_confirm_ep(request: Request):
    from . import scoping
    body = await request.json()
    return scoping.confirm(
        final_direction=body.get("final_direction", ""),
        keep_user=body.get("keep_user"), keep_new=body.get("keep_new"))


@router.post("/scope/skip")
async def scope_skip_ep(request: Request):
    from . import scoping
    body = await request.json()
    return scoping.skip(body.get("reason", ""))


# ──────────── file browser + editor (the Files tab) ─────────────────────────
# Backs the Files tab: a JupyterLab-style tree browser + Monaco editor. These
# routes are NOT in auth._PUBLIC_PREFIXES, so they require the passcode when one
# is set. They expose the server filesystem (read/write) by absolute path, the
# same trust level as the existing agent-terminal / sessions endpoints.

_FILE_MAX_READ = 2_000_000   # 2 MB cap — anything bigger, open it in a terminal


@router.get("/files/list")
def files_list(path: str = str(ROOT)):
    """List a directory for the file browser. Dirs first, then name."""
    try:
        p = os.path.abspath(os.path.expanduser(path or "/"))
        if not os.path.isdir(p):
            return {"ok": False, "error": "not a directory", "path": p}
        entries = []
        with os.scandir(p) as it:
            for e in it:
                try:
                    st = e.stat(follow_symlinks=False)
                    is_dir = e.is_dir(follow_symlinks=False)
                    entries.append({
                        "name": e.name, "is_dir": is_dir,
                        "size": (None if is_dir else st.st_size),
                        "mtime": dt.datetime.utcfromtimestamp(
                            st.st_mtime).isoformat() + "Z",
                    })
                except Exception:                               # noqa: BLE001
                    entries.append({"name": e.name, "is_dir": False,
                                    "size": None, "mtime": None})
        entries.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
        parent = os.path.dirname(p.rstrip("/")) or "/"
        return {"ok": True, "path": p, "parent": parent, "entries": entries}
    except PermissionError:
        return {"ok": False, "error": "permission denied", "path": path}
    except Exception as e:                                      # noqa: BLE001
        return {"ok": False, "error": str(e), "path": path}


@router.get("/files/read")
def files_read(path: str):
    """Read a text file for the editor (size + binary guarded)."""
    try:
        p = os.path.abspath(os.path.expanduser(path))
        if not os.path.isfile(p):
            return {"ok": False, "error": "not a file", "path": p}
        sz = os.path.getsize(p)
        if sz > _FILE_MAX_READ:
            return {"ok": False, "path": p, "size": sz,
                    "error": f"file is {sz} bytes (> {_FILE_MAX_READ} cap) — "
                             "open it in a terminal instead"}
        with open(p, "rb") as f:
            raw = f.read()
        if b"\x00" in raw[:8192]:
            return {"ok": False, "path": p, "binary": True,
                    "error": "binary file — cannot edit as text"}
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
        return {"ok": True, "path": p, "size": sz, "content": text}
    except PermissionError:
        return {"ok": False, "error": "permission denied", "path": path}
    except Exception as e:                                      # noqa: BLE001
        return {"ok": False, "error": str(e), "path": path}


@router.put("/files/write")
async def files_write(request: Request):
    """Write a text file from the editor. Body: {path, content}."""
    body = await _safe_json(request)
    path = (body.get("path") or "").strip()
    content = body.get("content")
    if not path or content is None:
        return {"ok": False, "error": "path and content are required"}
    try:
        p = os.path.abspath(os.path.expanduser(path))
        if os.path.isdir(p):
            return {"ok": False, "error": "path is a directory", "path": p}
        d = os.path.dirname(p)
        if d and not os.path.isdir(d):
            return {"ok": False, "error": "parent directory does not exist",
                    "path": p}
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        return {"ok": True, "path": p, "bytes": len(content.encode("utf-8"))}
    except PermissionError:
        return {"ok": False, "error": "permission denied", "path": path}
    except Exception as e:                                      # noqa: BLE001
        return {"ok": False, "error": str(e), "path": path}


# ──────────── tmux run sessions (the Sessions tab) ───────────────────────────

_INFRA_SESSIONS = {"arui", "arui-cf", "cf", "agent", "author"}
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.\-=]+$")   # run ids contain '='


@router.get("/sessions")
def list_sessions():
    """Every tmux session that is a research run (infra sessions excluded)."""
    out = subprocess.run(["tmux", "list-sessions", "-F", "#{session_name}"],
                         capture_output=True, text=True)
    names = [n.strip() for n in out.stdout.splitlines() if n.strip()]
    return {"sessions": [n for n in names if n not in _INFRA_SESSIONS]}


@router.post("/sessions/create")
async def session_create(request: Request):
    """Spawn an ad-hoc tmux session the user can attach to + type into.

    Used by the "+ new" button on the Sessions tab. The session runs a
    bare bash shell — same env as the research agent (workspace cwd,
    ARUI_INGEST_TOKEN if set) — so the user can run nvidia-smi,
    inspect logs, or kick off a manual debug run.

    Refuses to clobber an existing session, and reserves the names of
    the agent/infra sessions (agent, author, arui, arui-cf, …).

    Body: {"session": "<name>"}

    Belt-and-suspenders: the whole body is wrapped so that ANY exception
    (NameError, missing workspace dir, tmux timeout, auth import-fail,
    shlex on None, …) returns a JSON {"ok": False, "error": "…"} with
    HTTP 200 instead of a 500 HTML page. The frontend's r.json() would
    crash on a 500 HTML body ("SyntaxError: Unexpected token 'I'…").
    """
    try:
        body = await _safe_json(request)
        name = (body.get("session") or "").strip()
        if not name or not _SAFE_NAME.match(name) or len(name) > 60:
            return {"ok": False,
                    "error": "session name must be 1-60 chars "
                             "of [A-Za-z0-9_.-=]"}
        if name in _INFRA_SESSIONS:
            return {"ok": False,
                    "error": f"'{name}' is reserved (infra session)"}
        if subprocess.run(["tmux", "has-session", "-t", name],
                          capture_output=True).returncode == 0:
            return {"ok": False,
                    "error": f"session '{name}' already exists"}
        # Build env: copy passcode + the standard arui vars so the new
        # shell behaves like the agent's. Workspace cwd defaults to the
        # active project workspace if one exists, else /root.
        env_parts = ["IS_SANDBOX=1"]
        try:
            from . import auth as _auth
            pc = _auth._saved_passcode()
            if pc:
                env_parts.append(f"ARUI_INGEST_TOKEN={shlex.quote(pc)}")
        except Exception:                                   # noqa: BLE001
            pass
        env_parts.append("ARUI_INGEST_URL=http://127.0.0.1:8000")
        # Find a workspace dir if any project exists.
        cwd = "/root"
        try:
            ws = WORKSPACE_DIR
            if ws.exists():
                for entry in ws.iterdir():
                    if entry.is_dir():
                        cwd = str(entry)
                        break
        except Exception:                                   # noqa: BLE001
            pass
        cmd = f"cd {shlex.quote(cwd)} && {' '.join(env_parts)} bash"
        r = subprocess.run(
            ["tmux", "new-session", "-d", "-s", name,
             "-x", "120", "-y", "40", cmd],
            capture_output=True, text=True, timeout=8)
        if r.returncode != 0:
            return {"ok": False,
                    "error": (r.stderr or "tmux new-session failed")[:200]}
        # Hook pipe-pane so the new session's bytes flow into the rail
        # xterm.js stream the same way as the agent session.
        try:
            from . import pane_stream
            pane_stream.enable(name)
        except Exception as e:                              # noqa: BLE001
            print(f"[sessions] pane_stream.enable({name}) failed: {e}",
                  flush=True)
        return {"ok": True, "session": name, "cwd": cwd}
    except Exception as e:                                  # noqa: BLE001
        # Never let a stray exception become a 500 HTML body —
        # the frontend can't JSON.parse "Internal Server Error".
        print(f"[sessions] /sessions/create crashed: {e!r}", flush=True)
        return {"ok": False, "error": f"session create failed: {e}"}


@router.get("/sessions/{name}")
def session_output(name: str):
    """The captured stdout/stderr of one run's tmux session."""
    if not _SAFE_NAME.match(name or ""):
        return {"text": "", "alive": False}
    alive = subprocess.run(["tmux", "has-session", "-t", name],
                           capture_output=True).returncode == 0
    if not alive:
        return {"text": "", "alive": False}
    out = subprocess.run(
        ["tmux", "capture-pane", "-t", name, "-p", "-S", "-4000"],
        capture_output=True, text=True)
    return {"text": out.stdout, "alive": True}


@router.post("/sessions/{name}/attach")
def session_attach(name: str):
    """Wire `pane_stream.enable()` for ``name`` so its raw byte stream
    becomes available at ``/api/agent/raw?session=<name>``.

    Used by the Sessions tab when the user clicks a run's tab: the
    frontend POSTs here BEFORE starting its xterm.js stream so the
    interactive terminal has bytes to read from byte 1.

    For per-run sessions (``diff_*``, ``pr-*``, ``_smoke_*``, user-created
    debug shells, …) the orchestrator / agent may or may not have called
    ``pane_stream.enable`` proactively. This endpoint is idempotent —
    tmux replaces any prior ``pipe-pane`` mapping — so calling it once
    per user-click is safe.

    Refuses:
      - names that aren't ``_SAFE_NAME``,
      - the reserved infra sessions (``agent``/``author``/… — those have
        their own dedicated boot path and we don't want a misclick to
        re-truncate their raw stream),
      - sessions that don't exist (tmux ``has-session`` returns rc≠0).

    Body: empty. Returns JSON ``{"ok": true, "session": "<name>"}`` on
    success, ``{"ok": false, "error": "<msg>"}`` otherwise (always HTTP
    200 — matches the JSON-only contract of ``/sessions/create``).
    """
    try:
        if not name or not _SAFE_NAME.match(name) or len(name) > 80:
            return {"ok": False,
                    "error": "session name must be 1-80 chars "
                             "of [A-Za-z0-9_.-=]"}
        if name in _INFRA_SESSIONS:
            return {"ok": False,
                    "error": f"'{name}' is reserved (infra session)"}
        alive = subprocess.run(
            ["tmux", "has-session", "-t", name],
            capture_output=True).returncode == 0
        if not alive:
            return {"ok": False,
                    "error": f"session '{name}' does not exist"}
        from . import pane_stream
        pane_stream.enable(name)
        return {"ok": True, "session": name}
    except Exception as e:                                  # noqa: BLE001
        print(f"[sessions] /sessions/{name}/attach crashed: {e!r}",
              flush=True)
        return {"ok": False, "error": f"attach failed: {e}"}


# ──────────── run logs, kill, system stats, metric names ─────────────────────

@router.get("/runs/{run_id}/logs")
def run_logs(run_id: str, tail: int = 800):
    """A run's captured stdout/stderr — persists after the run finishes."""
    return monitor.run_log(run_id, tail)


@router.post("/runs/{run_id}/kill")
def run_kill(run_id: str):
    """Kill a run's tmux session (the monitor then marks it crashed)."""
    if not _SAFE_NAME.match(run_id or ""):
        return {"ok": False, "error": "bad run id"}
    subprocess.run(["tmux", "kill-session", "-t", run_id],
                   capture_output=True)
    return {"ok": True}


@router.get("/system")
def system():
    """Cached host telemetry — GPUs, CPU, RAM, disk, uptime + any active
    warnings (low disk, hot GPU, runaway RAM)."""
    from . import maintenance
    stats = dict(monitor.system_stats())
    stats["warnings"] = maintenance.system_warnings()
    return stats


@router.get("/runs/cleanup/preview")
def runs_cleanup_preview(min_age_days: float = 2.0,
                         bottom_pct: float = 0.5):
    """What WOULD be purged by /runs/cleanup. Powers the Settings
    confirmation modal so the user sees exactly which logs will go."""
    from . import maintenance
    return maintenance.preview(min_age_days, bottom_pct)


@router.post("/runs/cleanup")
async def runs_cleanup(request: Request):
    """Delete stdout/stderr log files for runs older than ``min_age_days``
    and in the bottom ``bottom_pct`` (per the project's metric direction).
    Keeps the Run row + its headline metric + council review intact."""
    from . import maintenance
    body = await _safe_json(request)
    age = float(body.get("min_age_days") or 2.0)
    pct = float(body.get("bottom_pct") or 0.5)
    return maintenance.purge_old_run_logs(age, pct)


@router.get("/runs/cleanup/preview_sota")
def runs_cleanup_preview_sota():
    """What WOULD be purged by /runs/cleanup_sota — every non-SOTA run's
    on-disk artifacts. The SOTA run + baselines are always kept."""
    from . import maintenance
    return maintenance.preview_keep_sota_only()


@router.post("/runs/cleanup_sota")
def runs_cleanup_sota():
    """Aggressive: keep ONLY the project-best (SOTA) run's checkpoint and
    artifacts. Every other completed run's on-disk state is purged.
    Run rows + metrics + council reviews stay intact."""
    from . import maintenance
    return maintenance.purge_keep_sota_only()


@router.post("/clientlog")
async def clientlog(request: Request):
    """Receive a browser-side JS error so frontend crashes are debuggable."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    line = (f"{_iso()} {body.get('msg', '')} | {body.get('src', '')}:"
            f"{body.get('line', '')} | {str(body.get('stack', ''))[:700]}")
    print("[clientlog]", line, flush=True)
    try:
        with open(DATA_DIR / "clientlog.txt", "a") as f:
            f.write(line + "\n")
    except OSError:
        pass
    return {"ok": True}


@router.get("/metrics/names")
def metric_names():
    return {"metrics": metrics.all_keys()}


# ─────────── Analysis v2: batched bucketed metrics + keys + panels ────────

@router.get("/metrics/keys")
def metrics_keys():
    """All metric keys ever seen on this project (Analysis 'Add panel' uses
    this). Backed by the metric_keys table, not a DISTINCT scan."""
    return {"keys": metrics.all_keys()}


@router.get("/runs/{run_id}/metric_keys")
def run_metric_keys(run_id: str):
    """Keys that THIS run has logged. Drives the drawer 'View all plots'."""
    return {"keys": metrics.run_keys(run_id)}


@router.get("/runs/{run_id}/metric_coverage")
def run_metric_coverage(run_id: str):
    """Required-default-metric coverage for a single run.

    Returns ``{logged: [...], missing: [...], required: [...]}`` so the
    drawer can render an explicit hint ("Agent didn't log this key.
    Required: arui.log({'val_loss': ...})") next to each "(not logged)"
    placeholder. The same data drives ad-hoc CLI checks.
    """
    try:
        run_keys_set = set(metrics.run_keys(run_id))
    except Exception:
        run_keys_set = set()
    required = list(REQUIRED_DEFAULT_METRICS)
    logged = [k for k in required if k in run_keys_set]
    missing = [k for k in required if k not in run_keys_set]
    return {
        "run_id": run_id,
        "required": required,
        "logged": logged,
        "missing": missing,
        "all_keys": sorted(run_keys_set),
    }


@router.get("/metrics/key_coverage")
def metrics_key_coverage(key: str = "", limit: int = 50):
    """Which runs in this project logged a given metric key.
    Drives the Analysis empty-state's 'X other runs logged this — click to
    swap' helper. Returns up to `limit` run_ids ordered newest first."""
    if not key:
        return {"key": "", "run_ids": []}
    db = SessionLocal()
    try:
        rows = (db.query(Run.id, Run.run_name, Run.created_at)
                .filter(Run.id.in_(
                    [r[0] for r in metrics._con.execute(
                        "SELECT DISTINCT run_id FROM metrics WHERE key = ?",
                        [key]).fetchall()]))
                .order_by(Run.created_at.desc())
                .limit(limit).all())
        return {"key": key,
                "run_ids": [{"id": r[0], "run_name": r[1]} for r in rows]}
    finally:
        db.close()


@router.post("/metrics/batch")
async def metrics_batch(request: Request):
    """Server-bucketed multi-run, multi-key time-series. The single endpoint
    powering the Analysis tab's panel grid. Schema in
    docs/12-analysis-v2-spec-final.md."""
    body = await request.json()
    run_ids = body.get("run_ids") or []
    keys_wanted = body.get("keys") or []
    x_key = body.get("x_key") or "step"
    x_min = body.get("x_min")
    x_max = body.get("x_max")
    bucket_count = int(body.get("bucket_count") or 500)
    if not isinstance(run_ids, list) or not isinstance(keys_wanted, list):
        return {"status": "error", "detail": "run_ids and keys must be lists"}
    # Which runs are still running? Affects cache freshness.
    db = SessionLocal()
    try:
        running = {r.id for r in db.query(Run)
                   .filter(Run.id.in_(run_ids), Run.status == "running")
                   .all()}
    finally:
        db.close()
    return metrics.batch_bucketed(
        run_ids, keys_wanted,
        x_key=x_key, x_min=x_min, x_max=x_max,
        bucket_count=bucket_count, running_set=running)


@router.get("/analysis/panels")
def analysis_panels(db: Session = Depends(get_session)):
    """Persisted panel set for the Analysis tab. Falls back to a default
    set that adapts to which metric keys this project actually logs."""
    row = db.query(Setting).filter(Setting.key == "analysis_panels").first()
    if row and isinstance(row.value, dict) and row.value.get("panels"):
        return row.value

    # Project-aware default. Always emit the spec's standard 6 slots, so the
    # UI shows them as labelled spots — but title each with what the project
    # actually logs when possible. The project metric (proj.validation_metric)
    # is the most important one and gets pinned first.
    proj = db.query(Project).first()
    project_metric = (proj.validation_metric if proj else "") or ""
    keys_in_project = set(metrics.all_keys() or [])

    def _panel(pid: str, title: str, key: str, log: bool = False,
               baseline: bool = True, width: str = "half") -> dict:
        return {"id": pid, "title": title, "y_keys": [key],
                "x_key": "step", "y_log": log, "smoothing": 0.0,
                "include_baseline": baseline, "show_band": False,
                "width": width}

    panels = []
    # Pin the project's own validation metric first if it's set and exists.
    if project_metric and project_metric in keys_in_project:
        panels.append(_panel("p_main", f"Project metric · {project_metric}",
                              project_metric, log=False, baseline=True,
                              width="full"))
    # Standard spec defaults — keep their slots even if not logged yet so
    # the user sees the empty-state message and knows what's expected.
    panels.append(_panel("p_train_loss", "Training loss", "train_loss",
                         log=True))
    panels.append(_panel("p_val_loss", "Validation loss", "val_loss",
                         log=True))
    panels.append(_panel("p_train_acc", "Training accuracy", "train_acc"))
    panels.append(_panel("p_val_acc", "Validation accuracy", "val_acc"))
    panels.append(_panel("p_lr", "Learning rate", "lr", baseline=False))
    panels.append(_panel("p_step_time", "Time per step", "time_per_step",
                         baseline=False))
    return {"panels": panels}


@router.put("/analysis/panels")
async def analysis_panels_put(request: Request):
    """Save the panel set."""
    body = await request.json()
    panels = body.get("panels") or []
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(Setting.key == "analysis_panels").first()
        if row:
            row.value = {"panels": panels}
        else:
            db.add(Setting(key="analysis_panels", value={"panels": panels}))
        db.commit()
    finally:
        db.close()
    return {"status": "ok"}


# ──────────── authorized_keys management ─────────────────────────────────────

@router.post("/extra_nodes/check")
async def extra_nodes_check(request: Request):
    """Try to ssh to each of the user's extra GPU nodes and run nvidia-smi.
    Returns a list of {target, ok, gpus, error}. Best-effort — never raises."""
    body = await request.json()
    targets = body.get("targets") or []
    if isinstance(targets, str):
        targets = [t.strip() for t in targets.splitlines() if t.strip()]
    out = []
    for t in targets[:16]:
        host = t.strip()
        if not host:
            continue
        port = "22"
        user_host = host
        if ":" in host and "@" in host.split(":")[-1] is False:  # user@h:p
            try:
                user_host, port = host.rsplit(":", 1)
                int(port)
            except Exception:
                user_host, port = host, "22"
        cmd = ["ssh", "-i", "/root/.ssh/id_ed25519", "-p", port,
               "-o", "StrictHostKeyChecking=accept-new",
               "-o", "ConnectTimeout=8",
               "-o", "BatchMode=yes",
               user_host,
               "nvidia-smi --query-gpu=name --format=csv,noheader || hostname"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if r.returncode == 0:
                gpus = [g.strip() for g in (r.stdout or "").splitlines()
                        if g.strip()]
                out.append({"target": host, "ok": True, "gpus": gpus})
            else:
                out.append({"target": host, "ok": False,
                            "error": (r.stderr or r.stdout)[:200]})
        except Exception as e:                          # noqa: BLE001
            out.append({"target": host, "ok": False, "error": str(e)[:200]})
    return {"results": out}


# ════════════════════════════════════════════════════════════════════════
# Paper Mode endpoints (doc 13). Mode flip + proposal + state + decisions
# + sections + versions + reviewer-sim + submit. Research mode is unaware
# of any of these.
# ════════════════════════════════════════════════════════════════════════


@router.get("/mode")
def get_mode(db: Session = Depends(get_session)):
    """Current project mode plus minimal paper meta for the header pill."""
    from . import paper as _paper
    mode = _paper.project_mode()
    meta_row = db.query(PaperMeta).first() if mode == "paper" else None
    return {
        "mode": mode,
        "meta": meta_row.dict() if meta_row else None,
        "days_till_deadline": _paper.days_till_deadline(),
        "budget": _paper.budget_summary() if mode == "paper" else None,
    }


# ─────────────────────── Paper-mode rebuild (2026-06-05) ─────────────────
# Phase-machine endpoints (paper_phase.py). The Author Agent posts to
# /paper/phase at every transition; the Write-the-paper view polls
# /paper/status for the assembled snapshot.


@router.get("/paper/status")
def paper_status():
    try:
        from . import paper_phase
        return paper_phase.get_status_overview()
    except Exception as e:                                  # noqa: BLE001
        # Never 500 the write-paper view — return a safe fallback.
        return {
            "phase": {"phase": "paper.error", "at": "",
                       "actor": "system", "detail": {"reason": str(e)[:240]},
                       "fallback_used": True},
            "phase_label": "Error",
            "summary": f"status unavailable: {e!s}"[:240],
            "progress": {},
            "gate": {"plan": {"status": "pending"}},
            "issues": [],
            "novelty_available": False,
        }


@router.post("/paper/phase")
async def post_paper_phase(request: Request):
    body = await request.json()
    phase = (body.get("phase") or "").strip()
    if not phase:
        return {"ok": False, "error": "phase required"}
    actor = body.get("actor") or "author"
    progress = body.get("progress")
    detail = body.get("detail") or {}
    try:
        from . import paper_phase as pp
        return pp.set_phase(phase, actor=actor, progress=progress,
                              detail=detail)
    except Exception as e:                                  # noqa: BLE001
        return {"ok": False, "error": str(e)[:240]}


@router.post("/paper/plan/request_approval")
async def paper_plan_request_approval(request: Request):
    body = {}
    try:
        body = await request.json()
    except Exception:                                       # noqa: BLE001
        pass
    note = body.get("note") or ""
    from . import paper_phase as pp
    pp.request_plan_approval(note)
    pp.set_phase("paper.operator_review", actor="author",
                  detail={"note": note})
    return {"ok": True}


@router.post("/paper/plan/approve")
async def paper_plan_approve(request: Request):
    body = {}
    try:
        body = await request.json()
    except Exception:                                       # noqa: BLE001
        pass
    note = body.get("note") or ""
    by = body.get("by") or "operator"
    from . import paper_phase as pp
    out = pp.approve_plan(by=by, note=note)
    pp.set_phase("paper.run_ablations", actor="operator",
                  detail={"approved_by": by, "queued": out["queued_count"]})
    return out


@router.post("/paper/plan/request_changes")
async def paper_plan_request_changes(request: Request):
    body = {}
    try:
        body = await request.json()
    except Exception:                                       # noqa: BLE001
        pass
    note = body.get("note") or ""
    by = body.get("by") or "operator"
    from . import paper_phase as pp
    out = pp.request_changes(by=by, note=note)
    pp.set_phase("paper.plan_ablations", actor="operator",
                  detail={"requested_by": by, "note": note})
    return out


@router.get("/paper/novelty")
def paper_novelty():
    """Return the structured novelty narrative that the 'View evidence'
    modal renders. Schema in paper_phase docstring + the
    consult_paper_openai.md plan."""
    db = SessionLocal()
    try:
        from .models import Setting as _Setting
        row = (db.query(_Setting)
               .filter(_Setting.key == "paper.novelty_v1").first())
        if row and isinstance(row.value, dict):
            return row.value
        # Bootstrap a minimal-but-non-empty payload from the council's
        # last accepted proposal so the modal isn't blank even before
        # the author runs the lit/novelty summarizer.
        from .models import PaperProposal as _PP
        prop = (db.query(_PP)
                .filter(_PP.status == "accepted")
                .order_by(_PP.created_at.desc()).first())
        if prop:
            return _bootstrap_novelty_from_proposal(prop)
        return {
            "generated_at": "",
            "overall": {
                "novelty_rating": "unclear",
                "one_sentence": ("Novelty narrative not yet generated. "
                                  "The Author Agent will produce one "
                                  "during lit_review."),
                "summary_md": "",
                "risks_md": "",
            },
            "claims": [],
        }
    finally:
        db.close()


def _bootstrap_novelty_from_proposal(prop) -> dict:
    """Tiny fallback so 'View evidence' renders SOMETHING (council's
    own summary + cited run_ids) before the proper lit_agent run."""
    import json as _json
    summary = (prop.summary or "")[:1200]
    try:
        ev = _json.loads(prop.evidence_run_ids_json or "[]")
    except Exception:                                       # noqa: BLE001
        ev = []
    return {
        "generated_at": prop.created_at or "",
        "overall": {
            "novelty_rating": "suggestive",
            "one_sentence": summary[:280],
            "summary_md": summary,
            "risks_md": "",
        },
        "claims": [{
            "claim_id": "bootstrap",
            "title": "Council-approved conclusion",
            "status": "active",
            "evidence_strength": "strong",
            "novelty_rationale_md": summary,
            "supporting_runs": [{"run_id": r,
                                  "metric_key": "",
                                  "best_value": None,
                                  "higher_is_better": False,
                                  "dataset": "",
                                  "model_size": ""}
                                 for r in ev],
            "counterevidence_runs": [],
            "related_citations": [],
        }],
    }


@router.post("/paper/novelty/rebuild")
async def paper_novelty_rebuild():
    """Force-regenerate the novelty narrative via lit_agent.
    Idempotent; safe to call repeatedly."""
    try:
        from . import lit_agent
        if hasattr(lit_agent, "summarize_novelty"):
            return {"ok": True, "novelty": lit_agent.summarize_novelty()}
    except Exception as e:                                  # noqa: BLE001
        return {"ok": False, "error": str(e)[:240]}
    return {"ok": False, "error": "summarize_novelty not implemented"}


@router.post("/paper/proposal/start")
async def paper_proposal_start():
    """Create a paper_proposal row, kick off council assessments
    asynchronously. Returns immediately with the proposal id."""
    from . import paper as _paper, council as _c
    pid = "pp-" + os.urandom(5).hex()
    db = SessionLocal()
    try:
        db.add(PaperProposal(id=pid, status="in_progress",
                              council_responses={}))
        db.commit()
    finally:
        db.close()
    # Spawn a background thread that runs ONE reviewer per available
    # provider in parallel and aggregates into the proposal row.
    threading.Thread(
        target=_run_paper_proposal_council,
        args=(pid,), daemon=True,
        name=f"paper-proposal-{pid}").start()
    return {"proposal_id": pid, "status": "in_progress"}


def _run_paper_proposal_council(proposal_id: str) -> None:
    from . import council as _c, paper as _paper
    cfg = _c._settings()
    available = _c._available_reviewers(cfg)
    if _c._claude_available(cfg) and "claude" not in available:
        available.append("claude")
    if not available:
        db = SessionLocal()
        try:
            p = db.query(PaperProposal).filter(
                PaperProposal.id == proposal_id).first()
            if p:
                p.status = "ready"
                p.council_responses = {"_no_reviewers":
                    "No council reviewers configured. Add API keys in Settings."}
                db.commit()
        finally:
            db.close()
        return
    # Build the context once
    db = SessionLocal()
    try:
        proj = db.query(Project).first()
        # lessons
        try:
            lessons_path = _paper.paper_folder(db)
            lessons_path = (lessons_path.parent / "lessons.md"
                            if lessons_path else None)
            lessons = lessons_path.read_text(errors="ignore") \
                if lessons_path and lessons_path.exists() else ""
        except Exception:
            lessons = ""
        # frontier
        every = db.query(Run).all()
        frontier_ids = _c._frontier_ids(every)
        kept = [r for r in every if r.id in frontier_ids]
        # aggregate
        agg = _c._aggregate_stats(every, proj) if proj else {}
    finally:
        db.close()
    context = {
        "project": {"name": proj.name if proj else "",
                    "purpose": proj.purpose if proj else "",
                    "metric": proj.validation_metric if proj else "",
                    "direction": proj.metric_direction if proj else ""},
        "lessons_md": (lessons or "")[-8000:],
        "frontier_runs": [{"id": r.id, "name": r.run_name,
                            "metric": r.headline_metric,
                            "config": r.config if isinstance(r.config, dict) else {}}
                           for r in kept[-30:]],
        "aggregate": agg,
    }
    user = ("You are being asked the central question of paper-writing:\n"
            "**Is this research ready to write up?**\n\n"
            "Read everything below and return JSON ONLY matching:\n"
            "{ \"claims\": [{\"title\":..., \"summary\":..., "
            "\"evidence_strength\":\"strong|suggestive|anecdotal\"}], "
            "\"novelty\":\"high|medium|low|unclear\","
            "\"novelty_rationale\":\"...\","
            "\"red_flags\": [\"...\"],"
            "\"recommendation\":\"proceed_to_paper|keep_researching|pivot\","
            "\"rationale_md\":\"...\" }\n\nBe honest. Reviewers will see "
            "through hype. If novelty is unclear, say so.\n\n"
            "=== PROJECT CONTEXT ===\n"
            + json.dumps(context, indent=2, default=str))
    results: dict[str, dict] = {}
    threads = []
    def _one(rev):
        out = _c._call_reviewer(
            rev,
            "You are a senior reviewer for an ML conference (NeurIPS-tier). "
            "Assess whether the project below is ready to write up as a paper.",
            user, cfg)
        if out is not None:
            results[rev] = out
    for rev in available:
        t = threading.Thread(target=_one, args=(rev,), daemon=True)
        t.start(); threads.append(t)
    for t in threads:
        t.join(timeout=300)
    db = SessionLocal()
    try:
        p = db.query(PaperProposal).filter(
            PaperProposal.id == proposal_id).first()
        if p:
            p.status = "ready"
            p.council_responses = results
            db.commit()
    finally:
        db.close()
    try:
        bus.publish("paper", "proposal_ready", {"id": proposal_id})
    except Exception:
        pass


def _proposal_summary(p: PaperProposal) -> dict:
    """Compact form of a PaperProposal for list views.
    Includes a recommendation tally + the top claim title so the user
    can recognize the review at a glance without expanding it."""
    responses = p.council_responses or {}
    if not isinstance(responses, dict):
        responses = {}
    reviewers = [r for r in responses.keys() if not r.startswith("_")]
    rec_counts = {"proceed_to_paper": 0, "keep_researching": 0, "pivot": 0}
    top_claim = ""
    novelty_votes: dict[str, int] = {}
    for r in reviewers:
        body = responses.get(r) or {}
        if not isinstance(body, dict):
            continue
        rec = body.get("recommendation")
        if rec in rec_counts:
            rec_counts[rec] += 1
        nov = body.get("novelty")
        if nov:
            novelty_votes[nov] = novelty_votes.get(nov, 0) + 1
        if not top_claim:
            for cl in (body.get("claims") or []):
                t = (cl.get("title") or "").strip()
                if t:
                    top_claim = t
                    break
    novelty = ""
    if novelty_votes:
        novelty = max(novelty_votes.items(), key=lambda kv: kv[1])[0]
    return {
        "id": p.id,
        "created_at": p.created_at,
        "status": p.status,
        "accepted_at": p.accepted_at or "",
        "rejected_at": p.rejected_at or "",
        "n_reviewers": len(reviewers),
        "proceed_count": rec_counts["proceed_to_paper"],
        "keep_researching_count": rec_counts["keep_researching"],
        "pivot_count": rec_counts["pivot"],
        "novelty": novelty,
        "top_claim": top_claim,
    }


@router.get("/paper/proposals")
def paper_proposals_list(db: Session = Depends(get_session)):
    """All paper-mode council reviews ever run, newest first.
    Powers the 'Past paper proposals' history table so a dismissed
    proposal isn't lost — the user can click any row to re-open the
    review and accept it later."""
    rows = (db.query(PaperProposal)
              .order_by(PaperProposal.created_at.desc()).all())
    return {"proposals": [_proposal_summary(p) for p in rows]}


@router.get("/paper/proposal/latest")
def paper_proposal_latest(db: Session = Depends(get_session)):
    p = (db.query(PaperProposal).order_by(
         PaperProposal.created_at.desc()).first())
    return p.dict() if p else {}


@router.get("/paper/proposal/{pid}")
def paper_proposal_get(pid: str, db: Session = Depends(get_session)):
    p = db.query(PaperProposal).filter(PaperProposal.id == pid).first()
    return p.dict() if p else {}


@router.post("/paper/proposal/{pid}/dismiss")
def paper_proposal_dismiss(pid: str, db: Session = Depends(get_session)):
    """User closed the council-review modal without accepting. We keep
    the row (so it shows up in the history table) and mark it
    'dismissed' rather than deleting it. Reversible — clicking the row
    later and pressing 'Proceed to Paper Mode' re-flips it to
    'accepted' via /paper/enter."""
    p = db.query(PaperProposal).filter(PaperProposal.id == pid).first()
    if not p:
        return {"ok": False, "detail": "not found"}
    if p.status == "accepted":
        # don't downgrade an accepted proposal
        return {"ok": False, "detail": "already accepted"}
    p.status = "dismissed"
    p.rejected_at = dt.datetime.now(dt.timezone.utc).isoformat()
    db.commit()
    try:
        bus.publish("paper", "proposal_dismissed", {"id": pid})
    except Exception:
        pass
    return {"ok": True, "id": pid, "status": "dismissed"}


@router.post("/paper/enter")
async def paper_enter(request: Request):
    """Flip to paper mode. Body: {meta: {venue, deadline_iso, authors, ...},
    proposal_id}. Spawns Author Agent + Paper Runner + writes mode_history."""
    from . import paper as _paper
    from . import author_agent
    from . import paper_runner
    body = await _safe_json(request)
    meta = body.get("meta") or {}
    proposal_id = body.get("proposal_id") or ""
    if _paper.project_mode() == "paper":
        return {"status": "already_in_paper_mode"}
    db = SessionLocal()
    try:
        # write PaperMeta
        m = db.query(PaperMeta).first()
        if not m:
            m = PaperMeta(id="pm-" + os.urandom(4).hex(),
                          venue=meta.get("venue") or "NeurIPS 2026",
                          style_id=meta.get("style_id") or "neurips_2025",
                          deadline_iso=meta.get("deadline_iso") or "",
                          anonymize=bool(meta.get("anonymize", True)),
                          authors_json=meta.get("authors") or [],
                          gpu_budget_hours=float(meta.get("gpu_budget_hours")
                                                  or 800),
                          llm_budget_daily_usd=float(
                              meta.get("llm_budget_daily_usd") or 20),
                          title_preference=meta.get("title_preference")
                                          or "auto",
                          phase="scaffold")
            db.add(m)
        else:
            for k, v in meta.items():
                if v is None:
                    continue
                if hasattr(m, k):
                    setattr(m, k, v)
            m.phase = "scaffold"
        # mode_history snapshot of current state
        db.add(ModeHistory(
            id="mh-" + os.urandom(4).hex(),
            from_mode="research", to_mode="paper",
            reason_md="user accepted paper proposal",
            snapshot_json={"proposal_id": proposal_id}))
        # Mark the selected proposal as accepted so the history table
        # remembers which review we acted on (the rest become
        # 'superseded'). When the user later clicks an old dismissed
        # proposal in the history and re-accepts, this fires again.
        now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
        if proposal_id:
            chosen = db.query(PaperProposal).filter(
                PaperProposal.id == proposal_id).first()
            if chosen:
                chosen.status = "accepted"
                chosen.accepted_at = now_iso
            for other in (db.query(PaperProposal)
                          .filter(PaperProposal.id != proposal_id)
                          .filter(PaperProposal.status.in_(
                              ("ready", "in_progress"))).all()):
                other.status = "superseded"
                other.rejected_at = now_iso
        db.commit()
    finally:
        db.close()
    _paper.set_project_mode("paper")
    # Convert council claims → PaperClaim. The Author Agent then OWNS
    # the ablation queue — we do not seed default ablations here.
    claims_added = _paper.populate_claims_from_proposal(proposal_id)
    runs_added   = 0
    # PAUSE the autonomous research loop so it doesn't keep launching
    # diff_* experiments and starve the Paper Runner of GPUs. We kill the
    # tmux session and disable PI's "fill the idle GPUs" nags. In-flight
    # training runs are NOT killed — they finish naturally and the Paper
    # Runner picks up GPUs as they free.
    try:
        subprocess.run(["tmux", "kill-session", "-t", "agent"],
                       capture_output=True, timeout=5)
    except Exception:
        pass
    try:
        subprocess.run(["tmux", "kill-session", "-t", "coord"],
                       capture_output=True, timeout=5)
    except Exception:
        pass
    # PI stays ENABLED in paper mode — pi.cycle() detects the mode and
    # switches to nagging the author agent instead of the research one.
    _set_setting("pi_agent_enabled", True)
    # Paper mode → switch the email cadence to ONCE A DAY by default. The
    # research-mode hourly digest is too noisy for writing weeks — daily
    # is the rhythm authors actually want. Don't override 'off' (user
    # explicitly disabled email) or anything already 24h+.
    try:
        db2 = SessionLocal()
        try:
            row = db2.query(Setting).filter(
                Setting.key == "onboarding").first()
            cfg2 = dict(row.value) if row and isinstance(row.value, dict) \
                else {}
            cur_cad = str(cfg2.get("cadence") or "").strip().lower()
            if cur_cad in ("", "immediate", "1h", "4h", "12h"):
                _set_setting("cadence", "24h")
                print(f"[paper] auto-switched cadence "
                      f"{cur_cad!r} → '24h' for paper mode", flush=True)
        finally:
            db2.close()
    except Exception as e:                              # noqa: BLE001
        print(f"[paper] cadence auto-switch skipped: {e}", flush=True)
    # Spawn agent + runner. Author Agent reads the proposal.
    ar = author_agent.start(proposal_id=proposal_id)
    paper_runner.start()
    # Lit Agent fire-and-forget (network call → don't block enter())
    import threading as _th
    _th.Thread(target=_paper.kickoff_lit_discover, daemon=True,
               name="lit-discover-initial").start()
    # Seed the phase machine so the Write-the-paper view has a non-
    # empty status from the very first poll. The Author Agent will
    # override this within its first prompt cycle.
    try:
        from . import paper_phase as _pp
        _pp.set_phase("paper.whittle_claims", actor="system",
                       progress={
                           "claims": {"active": claims_added},
                           "lit": {"citations": 0, "approved": 0,
                                    "pending": 0},
                       },
                       detail={"trigger": "paper.enter"})
    except Exception as e:                                  # noqa: BLE001
        print(f"[paper] phase seed failed: {e}", flush=True)
    return {"status": "entered_paper_mode", "author_agent": ar,
            "claims_added": claims_added, "runs_added": runs_added}


# ──────────────────────────────────────────────────────────────────────────
# Author-agent autonomous control surface.
# These endpoints are what the Author Agent calls (via plain HTTP from its
# Claude Code bash tools) when it wants to queue/kill/inspect ablation
# runs on its own — no human-in-the-loop approval needed for run mechanics.
# The decision queue stays reserved for STRATEGIC items only (cite_paper,
# kill_claim, approve_text, approve_figure).
# ──────────────────────────────────────────────────────────────────────────


@router.post("/paper/runs/queue")
async def paper_runs_queue(request: Request):
    """Author-agent calls this to add a paper_run directly. The Paper Runner
    bin-packs it onto an idle GPU on the next tick.

    Body (all optional except `cmd` or `train_args`):
      {
        name:        "headline_v3_s1",        # human label
        claim_id:    "pc-abcd1234",           # which claim this supports
        role:        "headline|ablation|baseline|seed",
        cmd:         "cd … && python train.py …",   # explicit shell command
        train_args:  "--mode diff --name foo --seed 7 …",  # OR these args;
                                              # we wrap them in the standard
                                              # `python train.py` invocation
        n_seeds:     1,
        gpus_required: 1,
        est_time_sec: 5400,
        depends_on:  ["pr-…", "pr-…"],
        figure_id:   "pf-…",
      }
    """
    from . import paper as _paper
    body = await _safe_json(request)
    name = (body.get("name") or "").strip() or ("auto-" + os.urandom(2).hex())
    claim_id = body.get("claim_id") or ""
    figure_id = body.get("figure_id") or ""
    role = body.get("role") or "ablation"
    cmd = body.get("cmd") or ""
    train_args = body.get("train_args") or ""
    n_seeds = max(1, int(body.get("n_seeds") or 1))
    gpus_required = max(1, int(body.get("gpus_required") or 1))
    est_time_sec = int(body.get("est_time_sec") or 5400)
    depends_on = body.get("depends_on") or []
    # If no explicit cmd but train_args given, wrap the standard invocation
    # the same way _default_run_cmd_for_project does — so PYTHONPATH/ARUI
    # env vars are set and import arui works.
    if not cmd and train_args:
        folder = _paper.paper_folder()
        if folder:
            from .config import ROOT
            workspace = str(folder.parent)
            cmd = (f"cd {workspace} && "
                   f"PYTHONPATH={ROOT}:${{PYTHONPATH:-}} "
                   f"ARUI_INGEST_URL=http://127.0.0.1:8000/api/track "
                   f"ARUI_PROJECT={folder.parent.name} "
                   f"python train.py {train_args}")
    if not cmd:
        return {"ok": False,
                "detail": "Either cmd or train_args is required"}
    db = SessionLocal()
    try:
        rid = "pr-" + os.urandom(5).hex()
        db.add(Run(
            id=rid, run_name=name,
            status="queued", context="paper",
            paper_claim_id=claim_id,
            paper_figure_id=figure_id,
            paper_role=role,
            n_seeds=n_seeds,
            config={"cmd": cmd, "queued_by": "author_agent",
                    "train_args": train_args},
            gpus_required=gpus_required,
            est_time_sec=est_time_sec,
            depends_on=depends_on if isinstance(depends_on, list) else [],
        ))
        db.commit()
    finally:
        db.close()
    bus.publish("paper", "run_queued",
                {"run_id": rid, "queued_by": "author_agent"})
    return {"ok": True, "id": rid, "name": name}


@router.post("/paper/runs/queue_batch")
async def paper_runs_queue_batch(request: Request):
    """Convenience: queue many runs in one call. Body: {runs: [<same shape as queue>, ...]}."""
    body = await _safe_json(request)
    runs_in = body.get("runs") or []
    if not isinstance(runs_in, list):
        return {"ok": False, "detail": "runs must be a list"}
    queued = []
    for r in runs_in:
        try:
            req = type("R", (), {})()
            async def _read(_=r):
                return _
            req.json = _read
            req.headers = {"content-length": "1"}
            resp = await paper_runs_queue(req)
            if resp.get("ok"):
                queued.append(resp["id"])
        except Exception as e:
            print(f"[paper_runs_queue_batch] error: {e}", flush=True)
    return {"ok": True, "queued_ids": queued, "n": len(queued)}


@router.get("/paper/runs/results")
def paper_runs_results(since: str = "", status: str = "",
                        db: Session = Depends(get_session)):
    """List paper_runs with their headline metric — used by the Author
    Agent to inspect what finished and decide what to queue next.
    Query: ?since=<iso>&status=running|queued|kept|crashed|all (default all).
    """
    q = db.query(Run).filter(Run.context == "paper")
    if status and status != "all":
        statuses = status.split(",")
        q = q.filter(Run.status.in_(statuses))
    if since:
        q = q.filter((Run.ended_at >= since) | (Run.started_at >= since))
    rows = q.order_by(Run.started_at.desc()).limit(500).all()
    out = []
    for r in rows:
        cfg = r.config if isinstance(r.config, dict) else {}
        out.append({
            "id": r.id, "name": r.run_name, "status": r.status,
            "claim_id": r.paper_claim_id, "figure_id": r.paper_figure_id,
            "role": r.paper_role, "seed": cfg.get("seed"),
            "started_at": r.started_at, "ended_at": r.ended_at,
            "gpu_index": r.gpu_index,
            "headline_metric": r.headline_metric,
            "cmd": cfg.get("cmd", ""),
        })
    return {"runs": out}


@router.post("/paper/decisions")
async def paper_decision_create(request: Request):
    """Author Agent files a strategic decision via this endpoint.

    Only these `kind` values are appropriate now (ablation launches are
    no longer routed through here — author agent queues runs directly):
        cite_paper, kill_claim, approve_text, approve_figure

    Body:
      {
        kind: "cite_paper",
        title: "Cite Lou 2024 SEDD in §2.1?",
        body_md: "…why this is relevant, what we'd add…",
        default_action: "approve" | "reject",
        priority: 5,
        linked_claim_id: "pc-…",
        linked_citation_key: "lou2024sedd",
      }
    """
    from . import paper as _paper
    body = await _safe_json(request)
    kind = (body.get("kind") or "").strip()
    title = (body.get("title") or "").strip()
    if not kind or not title:
        return {"ok": False, "detail": "kind and title required"}
    did = _paper.file_decision(
        source="agent",
        kind=kind,
        title=title,
        body_md=body.get("body_md") or "",
        default_action=body.get("default_action") or "approve",
        options=body.get("options") or [],
        priority=int(body.get("priority") or 0),
        linked_claim_id=body.get("linked_claim_id") or "",
        linked_figure_id=body.get("linked_figure_id") or "",
        linked_run_id=body.get("linked_run_id") or "",
        linked_citation_key=body.get("linked_citation_key") or "",
    )
    return {"ok": True, "id": did}


@router.put("/paper/claims/{cid}/update")
async def paper_claim_update(cid: str, request: Request):
    """Author Agent updates a claim's status / evidence_strength / ready /
    summary as it learns more. Body: any subset of those fields."""
    body = await _safe_json(request)
    allowed = {"status", "evidence_strength", "novelty", "ready",
               "summary_md", "rationale_md", "killed_reason"}
    db = SessionLocal()
    try:
        c = db.query(PaperClaim).filter(PaperClaim.id == cid).first()
        if not c:
            return {"ok": False, "detail": "claim not found"}
        for k, v in body.items():
            if k in allowed:
                setattr(c, k, v)
        db.commit()
    finally:
        db.close()
    bus.publish("paper", "claim_updated", {"id": cid})
    return {"ok": True}


@router.post("/paper/runs/{rid}/kill")
def paper_run_kill(rid: str):
    """Kill a diverging paper-mode run. Wraps the existing /runs/{id}/kill
    but verifies the run is in paper context and tags who killed it."""
    db = SessionLocal()
    try:
        r = db.query(Run).filter(Run.id == rid).first()
        if not r or r.context != "paper":
            return {"ok": False, "detail": "not a paper run"}
        if r.tmux_session:
            subprocess.run(["tmux", "kill-session", "-t", r.tmux_session],
                           capture_output=True, timeout=5)
        r.status = "crashed"
        r.ended_at = dt.datetime.now(dt.timezone.utc).isoformat()
        cfg = dict(r.config) if isinstance(r.config, dict) else {}
        cfg["killed_by"] = "author_agent"
        r.config = cfg
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(r, "config")
        db.commit()
    finally:
        db.close()
    bus.publish("paper", "run_killed",
                {"run_id": rid, "by": "author_agent"})
    return {"ok": True}


@router.post("/paper/pause_research")
def paper_pause_research():
    """One-shot: kill the autonomous research-agent tmux + disable PI nags.
    Use this if the research loop is still running and starving paper
    ablations of GPUs."""
    killed = []
    for sess in ("agent", "coord"):
        r = subprocess.run(["tmux", "kill-session", "-t", sess],
                           capture_output=True, timeout=5)
        if r.returncode == 0:
            killed.append(sess)
    # PI stays enabled — it'll nag the author agent in paper mode now.
    _set_setting("pi_agent_enabled", True)
    bus.publish("paper", "research_paused", {"sessions_killed": killed})
    return {"ok": True, "killed": killed,
            "pi_enabled": False,
            "note": "in-flight training runs were NOT killed; they finish "
                    "naturally and the Paper Runner picks up freed GPUs."}


@router.post("/paper/scaffold")
def paper_scaffold():
    """One-shot: populate claims / queue ablations / kick off Lit Agent.
    Safe to call repeatedly while in paper mode."""
    from . import paper as _paper
    if _paper.project_mode() != "paper":
        return {"ok": False, "detail": "not in paper mode"}
    claims = _paper.populate_claims_from_proposal()
    # Backfill missing cmd on previously-queued/failed paper runs and
    # re-queue any that failed for "no cmd" so they pick up on next tick.
    backfilled = 0
    requeued_crashed = 0
    db = SessionLocal()
    try:
        for r in db.query(Run).filter(Run.context == "paper",
                                       Run.status.in_(
                                           ("queued", "failed", "crashed"))).all():
            cfg = r.config if isinstance(r.config, dict) else {}
            # Backfill missing cmd. ALSO patch existing cmds that lack the
            # PYTHONPATH/ARUI envs so the re-queue gets a working command.
            existing_cmd = cfg.get("cmd", "")
            needs_env_patch = (existing_cmd and
                "PYTHONPATH=" not in existing_cmd)
            if existing_cmd and not needs_env_patch:
                continue
            claim_title = ""
            if r.paper_claim_id:
                c = db.query(PaperClaim).filter(
                    PaperClaim.id == r.paper_claim_id).first()
                claim_title = (c.title if c else "") or ""
            new_cmd = _paper._default_run_cmd_for_project(
                r.paper_role or "ablation",
                cfg.get("ablation") or "default",
                int(cfg.get("seed") or 1),
                claim_title)
            if new_cmd:
                from sqlalchemy.orm.attributes import flag_modified
                cfg["cmd"] = new_cmd
                r.config = dict(cfg)              # new object — but JSON column
                flag_modified(r, "config")        # …needs explicit mark too
                # Re-queue anything that was previously failed OR crashed —
                # the patched cmd may succeed now.
                if r.status in ("failed", "crashed"):
                    if r.status == "crashed":
                        requeued_crashed += 1
                    r.status = "queued"
                    r.started_at = ""
                    r.ended_at = ""
                backfilled += 1
        if backfilled:
            db.commit()
    finally:
        db.close()
    # NOTE: we no longer auto-queue default ablations here. The Author
    # Agent now owns the ablation queue end-to-end and decides what to
    # run based on the claims it just imported. We do still backfill
    # cmds on any legacy queued/failed runs above so they remain valid.
    runs = 0
    import threading as _th
    _th.Thread(target=_paper.kickoff_lit_discover, daemon=True,
               name="lit-discover-scaffold").start()
    return {"ok": True, "claims_added": claims, "runs_added": runs,
            "backfilled_cmd": backfilled,
            "requeued_crashed": requeued_crashed,
            "lit_discover": "started in background",
            "note": "Author Agent now owns ablation queueing — see "
                    "/paper/runs/queue. We no longer seed default ablations."}


@router.post("/paper/author/restart")
def paper_author_restart():
    """Restart the Author Agent tmux (used when it crashed or you want a
    fresh session). Idempotent."""
    from . import author_agent
    from .db import SessionLocal as _SL
    db = _SL()
    try:
        p = db.query(PaperProposal).filter(
            PaperProposal.status.in_(("accepted", "ready"))).order_by(
            PaperProposal.created_at.desc()).first()
        pid = p.id if p else ""
    finally:
        db.close()
    if author_agent.is_running():
        author_agent.stop()
    return author_agent.start(proposal_id=pid)


@router.get("/paper/author/terminal")
def paper_author_terminal(tail: int = 200):
    from . import author_agent
    return {"running": author_agent.is_running(),
            "text": author_agent.terminal_tail(tail)}


@router.post("/paper/author/send")
async def paper_author_send(request: Request):
    """Send a message to the Author Agent (typed by the user in the rail)."""
    from . import author_agent
    body = await _safe_json(request)
    text = (body.get("text") or "").strip()
    if not text:
        return {"ok": False, "detail": "text required"}
    ok = author_agent.send(text)
    return {"ok": bool(ok), "sent": text[:120]}


@router.post("/paper/revert")
async def paper_revert(request: Request):
    """Flip back to research mode. Body: {reason}. Kills Author Agent,
    pauses paper_runs, captures Paper Snapshot."""
    from . import paper as _paper
    from . import author_agent
    body = await _safe_json(request)
    reason = (body.get("reason") or "").strip()
    if not reason or len(reason) < 5:
        return {"status": "error",
                "detail": "reason required (1+ sentence)"}
    snap = _paper.take_snapshot()
    db = SessionLocal()
    try:
        db.add(ModeHistory(
            id="mh-" + os.urandom(4).hex(),
            from_mode="paper", to_mode="research",
            reason_md=reason, snapshot_json=snap))
        # Pause running paper_runs
        for r in db.query(Run).filter(
                Run.context == "paper",
                Run.status.in_(("running", "queued"))).all():
            if r.status == "running":
                r.status = "paused"
        meta = db.query(PaperMeta).first()
        if meta:
            meta.phase = "archived"
        db.commit()
    finally:
        db.close()
    author_agent.stop()
    _paper.set_project_mode("research")
    # Resume the autonomous research loop we paused on /paper/enter.
    _set_setting("pi_agent_enabled", True)
    try:
        from . import orchestrator
        from .config import ROOT
        from pathlib import Path
        # Pick the project's existing workspace dir if one's known.
        proj = SessionLocal().query(Project).first()
        repo = (proj.name if proj else "project")
        # Restart the research orchestrator/agent in 'agent' tmux. If a
        # session is somehow still alive (shouldn't be), this is a no-op
        # because orchestrator.start handles that.
        workspace = WORKSPACE_DIR / repo
        if workspace.exists():
            orchestrator.start(str(workspace), name="resume", n_slots=10)
    except Exception as e:                              # noqa: BLE001
        print(f"[paper/revert] could not restart research agent: {e}",
              flush=True)
    bus.publish("paper", "mode_reverted", {"reason": reason})
    return {"status": "reverted"}


@router.get("/paper/state")
def paper_state(db: Session = Depends(get_session)):
    """Single payload: meta, claims, figures, paper_runs, sections,
    decisions(pending), versions, citations, budget, build_status."""
    from . import paper as _paper, paper_compile
    meta = db.query(PaperMeta).first()
    claims = [c.dict() for c in db.query(PaperClaim).order_by(
        PaperClaim.idx).all()]
    figures = [f.dict() for f in db.query(PaperFigure).all()]
    paper_runs = [r.dict() for r in db.query(Run).filter(
        Run.context == "paper").all()]
    sections = [s.dict() for s in db.query(PaperSection).all()]
    decisions = [d.dict() for d in db.query(PaperDecision).filter(
        PaperDecision.status == "pending").order_by(
        PaperDecision.priority.desc(),
        PaperDecision.created_at.asc()).all()]
    versions = [v.dict() for v in db.query(PaperVersion).order_by(
        PaperVersion.created_at.desc()).all()]
    citations = [c.dict() for c in db.query(PaperCitation).all()]
    return {
        "mode": _paper.project_mode(),
        "meta": meta.dict() if meta else None,
        "claims": claims, "figures": figures, "paper_runs": paper_runs,
        "sections": sections, "decisions": decisions,
        "versions": versions, "citations": citations,
        "budget": _paper.budget_summary(),
        "build_status": paper_compile.status(),
        "days_till_deadline": _paper.days_till_deadline(),
    }


@router.get("/paper/today")
def paper_today(db: Session = Depends(get_session)):
    """The Today view's content (more compact than /paper/state)."""
    from . import paper as _paper, paper_compile
    decisions = [d.dict() for d in db.query(PaperDecision).filter(
        PaperDecision.status == "pending").order_by(
        PaperDecision.priority.desc(),
        PaperDecision.created_at.asc()).limit(20).all()]
    running = [r.dict() for r in db.query(Run).filter(
        Run.context == "paper",
        Run.status == "running").all()]
    sections = [s.dict() for s in db.query(PaperSection).all()]
    folder = _paper.paper_folder(db)
    # If the section table is empty but the Author Agent has scaffolded
    # files on disk, synthesize a row per file so the user sees something
    # in the Today view instead of "no sections yet".
    if not sections and folder and (folder / "sections").exists():
        for tex in sorted(folder.glob("sections/*.tex")):
            if tex.name.endswith(".user.tex"):
                continue
            slug = tex.stem
            title = (slug.split("_", 1)[-1] if "_" in slug else slug
                     ).replace("_", " ").title()
            mtime = tex.stat().st_mtime
            # "writing" if modified in last hour; "draft" otherwise.
            status = ("writing"
                       if (dt.datetime.now().timestamp() - mtime) < 3600
                       else "draft")
            sections.append({
                "id": "syn-" + slug,
                "slug": slug,
                "title": title,
                "file_path": f"sections/{tex.name}",
                "status": status,
                "blocked_on_claim_id": "",
                "blocked_on_run_id": "",
                "last_agent_pass_at": dt.datetime.fromtimestamp(
                    mtime, tz=dt.timezone.utc).isoformat(),
                "last_user_edit_at": "",
                "agent_notes_md": "",
            })
    commits = _paper.list_commits(folder, limit=8) if folder else []
    # last-N completed paper_runs for the "overnight" line
    recent = (db.query(Run).filter(
        Run.context == "paper",
        Run.status.in_(("kept", "success", "done", "crashed", "failed")))
        .order_by(Run.ended_at.desc()).limit(15).all())
    return {
        "mode": _paper.project_mode(),
        "decisions": decisions,
        "running_runs": running,
        "recent_runs": [r.dict() for r in recent],
        "sections": sections,
        "commits": commits,
        "budget": _paper.budget_summary(),
        "days_till_deadline": _paper.days_till_deadline(),
        "build_status": paper_compile.status(),
    }


@router.get("/paper/decisions")
def paper_decisions(status: str = "pending",
                    db: Session = Depends(get_session)):
    q = db.query(PaperDecision)
    if status and status != "all":
        q = q.filter(PaperDecision.status == status)
    rows = q.order_by(PaperDecision.priority.desc(),
                      PaperDecision.created_at.asc()).all()
    return {"decisions": [d.dict() for d in rows]}


@router.post("/paper/decisions/{did}/resolve")
async def paper_decision_resolve(did: str, request: Request):
    from . import paper as _paper
    body = await _safe_json(request)
    action = body.get("action") or "approve"
    note = body.get("note") or ""
    ok = _paper.resolve_decision(did, action=action, note=note)
    return {"ok": bool(ok)}


@router.post("/paper/recompile")
async def paper_recompile(request: Request):
    from . import paper_compile
    body = await _safe_json(request)
    force = bool(body.get("force"))
    status = paper_compile.build(force=force)
    return status


@router.get("/paper/pdf")
def paper_pdf():
    from . import paper_compile
    data = paper_compile.pdf_bytes()
    if not data:
        return {"ok": False, "detail": "no pdf yet"}
    from fastapi.responses import Response
    return Response(content=data, media_type="application/pdf")


@router.get("/paper/build_log")
def paper_build_log():
    from . import paper_compile
    return paper_compile.status()


@router.get("/paper/tex")
def paper_tex(db: Session = Depends(get_session)):
    """Concatenated main.tex + sections/*.tex for the LaTeX viewer.
    Marks per-file boundaries so the frontend can split them."""
    from . import paper as _paper
    folder = _paper.paper_folder(db)
    if not folder or not (folder / "main.tex").exists():
        return {"files": []}
    files = []
    for p in [folder / "main.tex"] + sorted(folder.glob("sections/*.tex")):
        try:
            text = p.read_text(errors="ignore")
        except OSError:
            text = ""
        files.append({"path": str(p.relative_to(folder)), "content": text,
                       "user_owned": p.name.endswith(".user.tex")})
    return {"files": files}


@router.post("/paper/section/save")
async def paper_section_save(request: Request):
    """Persist a *.user.tex override file (the only kind of LaTeX edit
    we accept from the user in v1)."""
    from . import paper as _paper
    body = await _safe_json(request)
    path = body.get("path") or ""
    content = body.get("content") or ""
    if not path.endswith(".user.tex"):
        return {"ok": False, "detail":
                "only *.user.tex files are user-editable in v1"}
    folder = _paper.paper_folder()
    if not folder:
        return {"ok": False, "detail": "no paper folder"}
    target = (folder / path).resolve()
    if not str(target).startswith(str(folder.resolve())):
        return {"ok": False, "detail": "path traversal"}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    _paper.commit_paper_changes(folder, f"user edit: {path}",
                                author="Researcher")
    return {"ok": True}


@router.post("/paper/lit/search")
async def paper_lit_search(request: Request):
    from . import lit_agent
    body = await _safe_json(request)
    q = (body.get("query") or "").strip()
    if not q:
        return {"results": []}
    return {"results": lit_agent.search(q, limit=int(body.get("limit", 15)))}


@router.post("/paper/lit/auto_discover")
async def paper_lit_auto():
    from . import lit_agent
    n = lit_agent.auto_discover_for_claims(max_per_claim=5)
    return {"filed": n}


@router.get("/paper/versions")
def paper_versions(db: Session = Depends(get_session)):
    rows = db.query(PaperVersion).order_by(
        PaperVersion.created_at.desc()).all()
    return {"versions": [v.dict() for v in rows]}


@router.post("/paper/versions/pin")
async def paper_versions_pin(request: Request):
    from . import paper as _paper
    body = await _safe_json(request)
    label = (body.get("label") or "").strip() or \
        ("v-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M"))
    snap = _paper.take_snapshot()
    folder = _paper.paper_folder()
    sha = (_paper._run_git(folder, "rev-parse", "HEAD") if folder
            and (folder / ".git").exists() else "")
    vid = "pv-" + os.urandom(4).hex()
    db = SessionLocal()
    try:
        db.add(PaperVersion(
            id=vid, label=label, latex_commit_sha=sha,
            snapshot_json=snap, claims_summary_md=""))
        db.commit()
    finally:
        db.close()
    return {"id": vid, "label": label, "sha": sha}


@router.post("/paper/reviewer_sim/run")
async def paper_reviewer_sim_run():
    """Council role-plays venue reviewers on the current paper. Returns
    immediately; results land in paper_review_sim rows asynchronously."""
    threading.Thread(target=_run_reviewer_sim, daemon=True,
                     name="reviewer-sim").start()
    return {"status": "started"}


def _run_reviewer_sim():
    """Each council reviewer reads the current paper and emits a fake
    review with suggested defensive ablations as decisions."""
    from . import council as _c, paper as _paper, paper_compile
    cfg = _c._settings()
    available = _c._available_reviewers(cfg)
    if not available:
        return
    folder = _paper.paper_folder()
    if not folder:
        return
    # Read the current paper.
    tex = ""
    for p in [folder / "main.tex"] + sorted(folder.glob("sections/*.tex")):
        try:
            tex += f"\n\n% === {p.name} ===\n" + p.read_text(errors="ignore")
        except OSError:
            pass
    if not tex.strip():
        return
    prompt = (
        "You are a strict, skeptical NeurIPS-tier reviewer. Read the paper "
        "below and write your honest review. Find weaknesses, missing "
        "experiments, and unconvincing claims. Output JSON ONLY:\n"
        "{\"strengths\":[\"...\"], \"weaknesses\":[\"...\"], "
        "\"questions\":[\"...\"], \"score\":1-10, "
        "\"suggested_ablations\":[{\"title\":\"\",\"why\":\"\","
        "\"est_gpu_hours\":0,\"target_claim\":\"\"}]}\n\n=== PAPER ===\n"
        + tex[:60000])
    for rev in available:
        out = _c._call_reviewer(
            rev,
            "You are a strict NeurIPS reviewer. Be honest, not polite.",
            prompt, cfg)
        if not out:
            continue
        sid = "rs-" + os.urandom(4).hex()
        db = SessionLocal()
        try:
            db.add(PaperReviewSim(
                id=sid, model=rev,
                content_md=json.dumps(out, indent=2),
                suggested_decisions_json=out.get("suggested_ablations", [])))
            db.commit()
        finally:
            db.close()
        # File a decision per suggested ablation
        for sa in (out.get("suggested_ablations") or [])[:5]:
            _paper.file_decision(
                source="reviewer_sim", kind="add_ablation",
                title=f"[{rev}] Add ablation: {sa.get('title','')[:80]}",
                body_md=(f"**Why (reviewer):** {sa.get('why','')}\n\n"
                         f"**Targets claim:** {sa.get('target_claim','')}\n\n"
                         f"**Est GPU-hours:** {sa.get('est_gpu_hours','?')}"),
                default_action="approve",
                priority=8)
    bus.publish("paper", "reviewer_sim_finished", {})


# ────────────────────────────────────────────────────────────────────────
# Paper Mode v3 — missing endpoint completion
# (sections, citations, version diff, submission helpers, share token,
#  rebuttal parsing, anti-pattern nudges)
# ────────────────────────────────────────────────────────────────────────


@router.get("/paper/sections")
def paper_sections(db: Session = Depends(get_session)):
    rows = db.query(PaperSection).order_by(PaperSection.slug).all()
    return {"sections": [r.dict() if hasattr(r, "dict") else
            {c.name: getattr(r, c.name) for c in r.__table__.columns}
            for r in rows]}


@router.put("/paper/sections/{slug}/status")
async def paper_section_status(slug: str, request: Request):
    body = await _safe_json(request)
    new_status = (body.get("status") or "").strip()
    allowed = {"draft", "writing", "blocked", "ready", "needs_review"}
    if new_status not in allowed:
        return {"ok": False, "detail": f"status must be one of {sorted(allowed)}"}
    db = SessionLocal()
    try:
        s = db.query(PaperSection).filter(PaperSection.slug == slug).first()
        if not s:
            return {"ok": False, "detail": "section not found"}
        s.status = new_status
        s.last_user_edit_at = dt.datetime.now(dt.timezone.utc).isoformat()
        db.commit()
    finally:
        db.close()
    bus.publish("paper", "section_status_changed", {"slug": slug, "status": new_status})
    return {"ok": True}


@router.get("/paper/citations")
def paper_citations(db: Session = Depends(get_session)):
    rows = db.query(PaperCitation).order_by(PaperCitation.year.desc()).all()
    return {"citations": [
        {c.name: getattr(r, c.name) for c in r.__table__.columns}
        for r in rows]}


@router.post("/paper/citations/{key}/approve")
def paper_citation_approve(key: str):
    db = SessionLocal()
    try:
        r = db.query(PaperCitation).filter(PaperCitation.key == key).first()
        if not r:
            return {"ok": False, "detail": "citation not found"}
        r.user_approved_at = dt.datetime.now(dt.timezone.utc).isoformat()
        db.commit()
    finally:
        db.close()
    return {"ok": True}


@router.get("/paper/versions/{vid}/diff")
def paper_version_diff(vid: str, against: str = ""):
    """Diff between two pinned versions (by id). Per-file unified diff,
    keyed by relative paper/ path. Empty `against` → diff against HEAD."""
    from . import paper as _paper
    folder = _paper.paper_folder()
    if not folder:
        return {"ok": False, "detail": "no paper folder"}
    db = SessionLocal()
    try:
        v_a = db.query(PaperVersion).filter(PaperVersion.id == vid).first()
        v_b = (db.query(PaperVersion).filter(PaperVersion.id == against).first()
               if against else None)
    finally:
        db.close()
    if not v_a:
        return {"ok": False, "detail": "version not found"}
    sha_a = v_a.latex_commit_sha
    sha_b = (v_b.latex_commit_sha if v_b else "HEAD")
    if not sha_a:
        return {"ok": False, "detail": "version has no commit sha"}
    raw = _paper.diff(folder, sha_a, sha_b)
    # Split into per-file chunks for the UI.
    files: list[dict] = []
    cur_path = None
    cur_lines: list[str] = []
    for ln in raw.splitlines():
        if ln.startswith("diff --git "):
            if cur_path:
                files.append({"path": cur_path, "diff": "\n".join(cur_lines)})
            cur_lines = [ln]
            try:
                cur_path = ln.split(" b/")[-1]
            except IndexError:
                cur_path = ln
        else:
            cur_lines.append(ln)
    if cur_path:
        files.append({"path": cur_path, "diff": "\n".join(cur_lines)})
    return {"ok": True, "sha_a": sha_a, "sha_b": sha_b, "files": files}


# ── Submission helper ───────────────────────────────────────────────────


_DEANONYMIZE_PATTERNS = [
    (re.compile(r"\\author\s*\{[^}]+\}", re.I), "author block in LaTeX"),
    (re.compile(r"\\affil[a-z]*\s*\{[^}]+\}", re.I), "affiliation block"),
    (re.compile(r"\\thanks\s*\{[^}]+\}", re.I), "\\thanks footnote"),
    (re.compile(r"https?://github\.com/[^\s'\"\}]+"), "GitHub URL"),
    (re.compile(r"https?://(www\.)?gitlab\.[^\s'\"\}]+"), "GitLab URL"),
    (re.compile(r"\bORCID\b[^\n]{0,40}"), "ORCID line"),
]


@router.post("/paper/submit/anonymize_check")
def paper_anonymize_check():
    """Scan paper/ for de-anonymizing content: author names from PaperMeta,
    affiliation lines, ORCID, GitHub URLs, \\thanks footnotes."""
    from . import paper as _paper
    folder = _paper.paper_folder()
    if not folder:
        return {"ok": False, "detail": "no paper folder"}
    db = SessionLocal()
    try:
        meta = db.query(PaperMeta).first()
    finally:
        db.close()
    # Build the name list from authors_json (must redact in anon mode).
    name_patterns: list[tuple] = []
    if meta and isinstance(meta.authors_json, list):
        for a in meta.authors_json:
            name = (a.get("name") or "").strip() if isinstance(a, dict) else ""
            aff  = (a.get("affiliation") or "").strip() if isinstance(a, dict) else ""
            if name and len(name) >= 3:
                name_patterns.append((re.compile(
                    r"\b" + re.escape(name) + r"\b", re.I), f"author name '{name}'"))
            if aff and len(aff) >= 4:
                name_patterns.append((re.compile(
                    r"\b" + re.escape(aff) + r"\b", re.I), f"affiliation '{aff}'"))
    findings: list[dict] = []
    files_scanned = 0
    for p in list(folder.rglob("*.tex")) + list(folder.rglob("*.bib")):
        # Skip user override files? No — they go in the bundle too.
        try:
            text = p.read_text(errors="ignore")
        except OSError:
            continue
        files_scanned += 1
        for i, ln in enumerate(text.splitlines(), 1):
            for pat, label in (_DEANONYMIZE_PATTERNS + name_patterns):
                m = pat.search(ln)
                if m:
                    findings.append({
                        "path": str(p.relative_to(folder)),
                        "line": i, "match": m.group(0)[:100],
                        "kind": label,
                    })
                    if len(findings) >= 200:   # safety
                        break
            if len(findings) >= 200:
                break
        if len(findings) >= 200:
            break
    return {"ok": len(findings) == 0,
            "files_scanned": files_scanned,
            "findings": findings}


@router.post("/paper/submit/bundle")
def paper_submit_bundle():
    """Build paper/submission/<project>-<ts>.zip with PDF + .tex sources +
    refs.bib + supplementary stub. Auto-pin as v-submitted on success."""
    import zipfile
    from . import paper as _paper
    folder = _paper.paper_folder()
    if not folder:
        return {"ok": False, "detail": "no paper folder"}
    pdf = folder / "build" / "main.pdf"
    if not pdf.exists():
        return {"ok": False, "detail": "no PDF compiled yet"}
    sub_dir = folder / "submission"
    sub_dir.mkdir(exist_ok=True)
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    zip_path = sub_dir / f"submission-{ts}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(pdf, arcname="main.pdf")
        for p in [folder / "main.tex"]:
            if p.exists():
                zf.write(p, arcname=p.name)
        for p in folder.glob("sections/*.tex"):
            zf.write(p, arcname=f"sections/{p.name}")
        for p in folder.glob("figures/*"):
            zf.write(p, arcname=f"figures/{p.name}")
        for n in ("refs.bib",):
            if (folder / n).exists():
                zf.write(folder / n, arcname=n)
    # Auto-pin
    db = SessionLocal()
    try:
        sha = _paper._run_git(folder, "rev-parse", "HEAD") if (folder / ".git").exists() else ""
        vid = "pv-" + os.urandom(4).hex()
        db.add(PaperVersion(
            id=vid, label=f"v-submitted-{ts}",
            latex_commit_sha=sha, snapshot_json=_paper.take_snapshot(),
            frozen_pdf_path=str(pdf.resolve())))
        db.commit()
    finally:
        db.close()
    return {"ok": True, "zip": str(zip_path.relative_to(folder.parent)),
            "size_bytes": zip_path.stat().st_size,
            "version_id": vid}


@router.get("/paper/submit/page_count")
def paper_submit_page_count():
    """Use pdfinfo if available; otherwise estimate from log."""
    from . import paper as _paper
    folder = _paper.paper_folder()
    if not folder:
        return {"pages": 0}
    pdf = folder / "build" / "main.pdf"
    if not pdf.exists():
        return {"pages": 0}
    try:
        r = subprocess.run(["pdfinfo", str(pdf)],
                           capture_output=True, text=True, timeout=8)
        for ln in (r.stdout or "").splitlines():
            if ln.startswith("Pages:"):
                return {"pages": int(ln.split(":", 1)[1].strip())}
    except Exception:
        pass
    return {"pages": 0}


# ── Share token (read-only collaborator view) ───────────────────────────


def _share_token_key() -> str:
    return "paper_share_token"


@router.post("/paper/share/token")
def paper_share_create():
    """Generate (or rotate) the read-only share token. The token grants
    no write access; only the assembled state payload is returned."""
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(
            Setting.key == _share_token_key()).first()
        token = os.urandom(16).hex()
        if row:
            row.value = {"token": token, "created_at":
                          dt.datetime.now(dt.timezone.utc).isoformat()}
        else:
            db.add(Setting(key=_share_token_key(),
                           value={"token": token, "created_at":
                                  dt.datetime.now(dt.timezone.utc).isoformat()}))
        db.commit()
    finally:
        db.close()
    return {"token": token, "url": f"/p/{token}"}


@router.delete("/paper/share/token")
def paper_share_revoke():
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(
            Setting.key == _share_token_key()).first()
        if row:
            db.delete(row)
            db.commit()
    finally:
        db.close()
    return {"ok": True}


@router.get("/paper/share/{token}")
def paper_share_view(token: str):
    """Read-only paper state for a share-token holder. Returns the
    same shape as /paper/state minus internal IDs; PDF is fetched via
    /paper/share/<token>/pdf so the UI doesn't need an auth header."""
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(
            Setting.key == _share_token_key()).first()
    finally:
        db.close()
    if not row or not isinstance(row.value, dict) or \
            row.value.get("token") != token:
        return {"ok": False, "detail": "invalid token"}
    # Build a redacted state payload.
    from . import paper as _paper
    db = SessionLocal()
    try:
        meta = db.query(PaperMeta).first()
        claims = db.query(PaperClaim).order_by(PaperClaim.idx).all()
        decs = db.query(PaperDecision).filter(
            PaperDecision.status == "pending").all()
        sections = db.query(PaperSection).order_by(PaperSection.slug).all()
    finally:
        db.close()
    return {
        "ok": True,
        "venue": meta.venue if meta else "",
        "days_till_deadline": _paper.days_till_deadline(),
        "claims": [{"title": c.title, "summary_md": c.summary_md,
                    "status": c.status} for c in claims],
        "decisions": [{"title": d.title, "kind": d.kind,
                       "source": d.source, "body_md": d.body_md[:600]}
                      for d in decs[:50]],
        "sections": [{"slug": s.slug, "title": s.title, "status": s.status}
                     for s in sections],
        "has_pdf": (_paper.paper_folder() and
                    (_paper.paper_folder() / "build" / "main.pdf").exists()),
    }


@router.get("/paper/share/{token}/pdf")
def paper_share_pdf(token: str):
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(
            Setting.key == _share_token_key()).first()
    finally:
        db.close()
    if not row or not isinstance(row.value, dict) or \
            row.value.get("token") != token:
        return {"ok": False, "detail": "invalid token"}
    from . import paper_compile
    data = paper_compile.pdf_bytes()
    if not data:
        return {"ok": False, "detail": "no pdf yet"}
    from fastapi.responses import Response
    return Response(content=data, media_type="application/pdf")


# ── Rebuttal sub-mode ───────────────────────────────────────────────────


@router.post("/paper/rebuttal/start")
def paper_rebuttal_start():
    """Transition paper_phase → 'rebuttal'. Idempotent."""
    db = SessionLocal()
    try:
        meta = db.query(PaperMeta).first()
        if not meta:
            return {"ok": False, "detail": "not in paper mode"}
        prev = meta.phase
        meta.phase = "rebuttal"
        meta.updated_at = dt.datetime.now(dt.timezone.utc).isoformat()
        db.commit()
    finally:
        db.close()
    bus.publish("paper", "phase_changed",
                {"from": prev, "to": "rebuttal"})
    return {"ok": True, "from": prev, "to": "rebuttal"}


@router.post("/paper/rebuttal/parse")
async def paper_rebuttal_parse(request: Request):
    """Take pasted reviewer reviews → call the council to extract
    concerns → file one decision per concern. Returns the count."""
    from . import council as _c, paper as _paper
    body = await _safe_json(request)
    reviews = body.get("reviews") or []
    if not isinstance(reviews, list) or not reviews:
        return {"ok": False, "detail": "reviews must be a non-empty list"}
    filed = 0
    cfg = _c._settings()
    available = _c._available_reviewers(cfg)
    if not available:
        return {"ok": False, "detail": "no council reviewers configured"}
    for i, rv in enumerate(reviews):
        text = (str(rv).strip())[:8000]
        if not text:
            continue
        rev = available[0]    # cheap path: one reviewer extracts
        prompt = (
            "You are helping a researcher triage a peer review. Read the "
            "review and extract the top 3-6 actionable concerns. Each "
            "concern should be a single specific item that, if addressed, "
            "would satisfy the reviewer. Output JSON ONLY:\n"
            "{\"concerns\":[{\"title\":\"\",\"why\":\"\","
            "\"suggested_action\":\"\",\"est_gpu_hours\":0,"
            "\"category\":\"experiment|rewrite|cite|clarify\"}]}\n\n"
            "=== REVIEW " + str(i + 1) + " ===\n" + text)
        out = _c._call_reviewer(
            rev, "Extract concerns from the review. Be honest, not polite.",
            prompt, cfg)
        if not out:
            continue
        for cn in (out.get("concerns") or [])[:6]:
            cat = (cn.get("category") or "").lower()
            kind = ("add_ablation" if cat == "experiment"
                    else "approve_text" if cat == "rewrite"
                    else "cite_paper" if cat == "cite"
                    else "approve_text")
            _paper.file_decision(
                source="reviewer_sim",  # reuse the source enum
                kind=kind,
                title=f"[Rebuttal R{i+1}] {cn.get('title','')[:80]}",
                body_md=(f"**Reviewer's concern:** {cn.get('why','')}\n\n"
                         f"**Suggested action:** {cn.get('suggested_action','')}\n\n"
                         f"**Est GPU-hours:** {cn.get('est_gpu_hours','?')}"),
                default_action="approve",
                priority=9)
            filed += 1
    return {"ok": True, "filed": filed}


# ── Anti-pattern watcher (manual fire endpoint; scheduler kicks it too) ─


@router.post("/paper/anti_patterns/run")
def paper_antipatterns_run():
    """Inspect the project state and file low-priority decision-queue
    nudges for any anti-patterns detected. Returns the count filed."""
    from . import paper as _paper
    filed = 0
    db = SessionLocal()
    try:
        meta = db.query(PaperMeta).first()
        if not meta:
            return {"filed": 0}
        # Pattern 1 — many failed paper_runs in last 24h
        cutoff = (dt.datetime.now(dt.timezone.utc) -
                  dt.timedelta(hours=24)).isoformat()
        try:
            recent = db.query(Run).filter(
                Run.context == "paper",
                Run.started_at >= cutoff).all()
        except Exception:
            recent = []
        if len(recent) >= 5:
            failed = sum(1 for r in recent if (r.status or "") in
                         ("crashed", "failed", "error"))
            if failed / len(recent) > 0.3:
                filed += _file_nudge(
                    title=f"⚠ {failed}/{len(recent)} paper runs failed in 24h",
                    body=("That's a higher-than-usual failure rate. Often "
                          "this is infra (CUDA OOM, disk full, kernel) "
                          "rather than research. Worth investigating before "
                          "queuing more."))
        # Pattern 2 — paper-runs queued without recent progress
        try:
            queued = db.query(Run).filter(
                Run.context == "paper",
                Run.status == "queued").count()
        except Exception:
            queued = 0
        if queued > 8:
            filed += _file_nudge(
                title=f"⚠ {queued} paper runs queued",
                body=("Paper Runner has a long queue. Consider "
                      "deprioritising lower-value ablations or "
                      "increasing concurrency."))
        # (GPU budget pattern intentionally dropped — runs run until done.)
        # Pattern 4 — reviewer sim never run on current draft
        sims = db.query(PaperReviewSim).count()
        folder = _paper.paper_folder()
        commits = _paper.list_commits(folder, limit=5) if folder else []
        if commits and sims == 0:
            filed += _file_nudge(
                title="ℹ Run Reviewer Sim before submitting",
                body=("You haven't run the reviewer simulator on the "
                      "current paper. It typically surfaces 5-15 "
                      "defensive ablations that materially improve "
                      "acceptance odds."))
        # Pattern 5 — deadline close, no PDF compiled
        days_left = _paper.days_till_deadline()
        from . import paper_compile
        pdf_ready = (paper_compile.status() or {}).get("pdf_exists")
        if days_left is not None and days_left < 7 and not pdf_ready:
            filed += _file_nudge(
                title=f"⚠ Deadline in {days_left:.0f} days; no PDF yet",
                body=("Less than a week to deadline and no compiled draft. "
                      "The Author Agent should prioritise scaffolding NOW."))
    finally:
        db.close()
    return {"filed": filed}


def _file_nudge(title: str, body: str) -> int:
    """File a nudge ONLY if a near-duplicate (same title) isn't already
    pending — prevents flooding the queue on repeated watcher ticks.
    Returns 1 if filed, 0 if deduped."""
    from . import paper as _paper
    db = SessionLocal()
    try:
        existing = db.query(PaperDecision).filter(
            PaperDecision.status == "pending",
            PaperDecision.title == title).first()
        if existing:
            return 0
    finally:
        db.close()
    _paper.file_decision(
        source="system", kind="approve_text",
        title=title, body_md=body,
        default_action="approve",
        priority=3)
    return 1


@router.get("/lessons")
def lessons():
    """Parse workspace/<repo>/lessons.md (auto-written by the council) into a
    structured list: each entry has ts, reviewer, supporting_run, text, and
    any other run-ids the council mentioned in the body (evidence)."""
    from . import council as _c
    p = _c._lessons_path()
    if not p or not p.exists():
        return {"lessons": [], "path": str(p) if p else ""}
    try:
        text = p.read_text(errors="ignore")
    except OSError:
        return {"lessons": [], "path": str(p)}
    # Line format we write:
    #   - [YYYY-MM-DD HH:MM · <reviewer> on <run_name>] <text>
    pat = re.compile(
        r"^-\s*\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2})\s*·\s*"
        r"(?P<rev>[^·\]]+?)\s*on\s+(?P<run>[^\]]+?)\]\s*(?P<text>.*)$")
    # Build the set of known run_ids so we can pull "evidence" mentions out
    # of the lesson body.
    db = SessionLocal()
    try:
        known = {r.id for r in db.query(Run).all()}
        run_names = {r.run_name for r in db.query(Run).all() if r.run_name}
    finally:
        db.close()
    known_all = known | run_names
    out = []
    for ln in text.splitlines():
        m = pat.match(ln.strip())
        if not m:
            continue
        body = m.group("text").strip()
        # find any other run ids mentioned in the body (evidence beyond the
        # primary supporting run)
        evidence = []
        for name in sorted(known_all, key=lambda s: -len(s)):
            if name and name in body and name != m.group("run"):
                evidence.append(name)
                if len(evidence) >= 6:
                    break
        out.append({
            "ts": m.group("ts"),
            "reviewer": m.group("rev").strip(),
            "supporting_run": m.group("run").strip(),
            "text": body,
            "evidence": evidence,
        })
    return {"lessons": out, "path": str(p), "count": len(out)}


@router.get("/authkeys/pubkey")
def authkeys_pubkey():
    """This node's SSH public key (generated on first call if missing) — so
    the user can paste it into another GPU server's authorized_keys to attach
    that server as an additional GPU node."""
    return authkeys.local_pubkey()


@router.get("/authkeys")
def authkeys_list(db: Session = Depends(get_session)):
    """Authorized keys + a real SSH connect command for this node.
    Auto-detects user / public IP / sshd port; Settings overrides win
    (node_ssh_user, node_ssh_host, node_ssh_port)."""
    info = authkeys.detect_ssh_info()
    row = db.query(Setting).filter(Setting.key == "onboarding").first()
    cfg = dict(row.value) if row and isinstance(row.value, dict) else {}
    user = (cfg.get("node_ssh_user") or info.get("user") or "root").strip()
    host = (cfg.get("node_ssh_host") or info.get("host") or "").strip()
    port = str(cfg.get("node_ssh_port") or info.get("port") or "22").strip()
    overridden = bool(cfg.get("node_ssh_port"))
    hint = ""
    if host and port == "22" and not overridden:
        hint = (" — note: this is the INTERNAL sshd port. On RunPod / "
                "vast.ai the public port is usually a high port (e.g. "
                "22149); set node_ssh_port in Settings to override.")
    cmd = (f"ssh {user}@{host} -p {port}" if host
           else f"ssh {user}@<node-ip> -p {port}  (set node_ssh_host in "
                "Settings — public IP auto-detect failed)")
    return {"keys": authkeys.list_keys(), "ssh": cmd, "ssh_hint": hint,
            "ssh_user": user, "ssh_host": host, "ssh_port": port,
            "ssh_detected": info}


@router.post("/authkeys")
async def authkeys_add(request: Request):
    body = await request.json()
    return authkeys.add_key(body.get("key", ""))


@router.post("/authkeys/delete")
async def authkeys_delete(request: Request):
    body = await request.json()
    return authkeys.delete_key(body.get("fingerprint", ""))


# ──────────── notifications: config + test ───────────────────────────────────

_NOTIFY_KEYS = ("email", "cadence", "email_recipients", "gmail_app_pw",
                "dashboard_url", "resend_api_key", "notify_from",
                "smtp_host", "smtp_port", "smtp_user", "smtp_pass")


@router.post("/notify/config")
async def notify_config(request: Request):
    """Merge email / cadence / transport settings into the onboarding config
    without re-running onboarding (so live runs can enable email)."""
    body = await request.json()
    db = SessionLocal()
    row = db.query(Setting).filter(Setting.key == "onboarding").first()
    cfg = dict(row.value) if row and isinstance(row.value, dict) else {}
    for k in _NOTIFY_KEYS:
        if k in body:
            cfg[k] = body[k]
    if row:
        row.value = cfg
    else:
        db.add(Setting(key="onboarding", value=cfg))
    db.commit()
    db.close()
    return {"status": "ok", "cadence": cfg.get("cadence", "off")}


@router.post("/notify/test")
async def notify_test(request: Request):
    """Send a one-off email (or a full digest) to confirm delivery works."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if body.get("digest"):
        ok = notify.send_digest_now()
    else:
        ok = notify.send(
            "autoresearcherUI - test email",
            "This is a test from autoresearcherUI. If you can read this, "
            "email notifications are working.\n\n- autoresearcherUI")
    return {"sent": ok}


# ──────────── archive & restore ──────────────────────────────────────────────

@router.get("/archive/info")
def archive_info():
    """Sizes of the research state — drives the Archive modal."""
    return archive.info()


@router.get("/archive")
def archive_download(profile: str = "full"):
    """Stream the whole research state as a .tar.gz (full or slim)."""
    fname = archive.archive_filename(profile)
    return StreamingResponse(
        archive.stream(profile), media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@router.post("/archive/email")
async def archive_email():
    """Email the user how to grab their archive (the tarball itself is far too
    big to attach — email carries the instructions, not the gigabytes)."""
    info = archive.info()
    body = (
        "Your autoresearcherUI research archive is ready to pull off this "
        "server.\n\n"
        f"Full state:  {info['full_bytes'] / 1e6:.0f} MB\n"
        f"Slim state:  {info['slim_bytes'] / 1e6:.0f} MB "
        f"(no checkpoints / datasets)\n\n"
        "Two ways to get it:\n\n"
        "1. Dashboard — click Archive, then Download (full or slim).\n\n"
        "2. Server-to-server (best for large state) — run this on the NEW "
        "server:\n"
        f"   {info['rsync']}\n\n"
        "On the new server: install autoresearcherUI and, on the onboarding "
        "screen, choose 'Resume from archive' — upload the tarball (or point "
        "it at the rsync'd data/ folder). The agent picks the research back "
        "up where it left off.\n\n- autoresearcherUI")
    ok = notify.send("autoresearcherUI — your research archive is ready", body)
    return {"sent": ok}


@router.post("/restore")
async def restore(file: UploadFile = File(...)):
    """Onboarding: upload a research archive, restore it, resume the agent."""
    tmp = DATA_DIR / "_upload.tar.gz"
    with open(tmp, "wb") as fh:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            fh.write(chunk)
    try:
        result = archive.restore(str(tmp))
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    db = SessionLocal()
    row = db.query(Setting).filter(Setting.key == "onboarding").first()
    cfg = dict(row.value) if row and isinstance(row.value, dict) else {}
    db.close()
    if cfg.get("claude_token") or os.environ.get("ARUI_CLAUDE_BIN"):
        try:
            realrun.start_real(cfg, resume=True)
            result["agent"] = "resumed"
        except Exception as e:                       # noqa: BLE001
            result["agent"] = f"not resumed: {e}"
    return result
