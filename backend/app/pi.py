"""PI agent — hourly oversight of the autonomous research agent.

The PI (Principal Investigator) wakes up every hour (configurable), reads
the current state of the project, and writes a short message to the
research agent's tmux session — exactly as if a real PI walked past and
said "hey, the queue's wrong / your runs are diverging / GPU 3 is idle /
please plot X".

What the PI looks at:
  - GPU saturation (any idle / underused?)
  - The most recent ~12 runs — anyone clearly plateaued or diverging?
    A run is "plateaued" if its loss curve hasn't decreased meaningfully
    over its last N steps. "Diverging" if loss is increasing.
  - The top of ideas.md vs what the agent is currently running — is the
    agent following the council's reranking?
  - The last hour of the agent's tmux output (so we don't blindly nag).

What the PI does:
  - Drafts 1-3 short messages and types them into the agent's tmux session
    (the same channel the user uses).
  - Persists a "concerns" summary into chat_message so it shows in the
    Summary feed as a researcher-voice bubble.
  - Emits a 'pi_intervention' event.

Model selection comes from Settings ('pi_agent_model'). Cadence comes from
Settings ('pi_cadence_minutes', default 60). Enable/disable via
'pi_agent_enabled'.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request

from . import council, metrics
from .bus import bus
from .config import DATA_DIR
from .db import SessionLocal
from .models import ChatMessage, Event, Gpu, Run, Setting

_STARTED = False
_LOCK = threading.Lock()
_LAST_RUN = 0.0


DEFAULTS = {
    "pi_agent_enabled": True,
    "pi_agent_model": "gemini-2.5-pro",   # provider chosen from model string
    "pi_cadence_minutes": 60,
}


def _settings() -> dict:
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


def _iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


# ── what model handles the PI? routes by model-name prefix ─────────────
def _provider_for(model: str) -> str | None:
    m = (model or "").lower()
    if m.startswith("gemini"):
        return "gemini" if os.environ.get("GEMINI_API_KEY") else None
    if m.startswith("gpt") or m.startswith("o3") or m.startswith("o4"):
        return "openai" if os.environ.get("OPENAI_API_KEY") else None
    if m.startswith("claude"):
        return "claude" if os.environ.get("ANTHROPIC_API_KEY") else None
    return None


# ── context for the PI ─────────────────────────────────────────────────
def _agent_tail(n: int = 60) -> str:
    """Last few lines of the research agent's tmux output."""
    try:
        out = subprocess.run(
            ["tmux", "capture-pane", "-t", "agent", "-p", "-S", str(-n)],
            capture_output=True, text=True, timeout=8)
        return out.stdout.strip()
    except Exception:
        return ""


def _plateau_signal(run_id: str) -> dict | None:
    """If a run's headline-ish metric has stagnated or risen over its last
    window of points, flag it."""
    try:
        m = metrics.query(run_id, None) or {}
    except Exception:
        return None
    out = None
    for k, pts in m.items():
        if not pts or len(pts) < 12:
            continue
        ys = [p[1] for p in pts[-30:] if isinstance(p, (list, tuple))
              and len(p) >= 2 and p[1] is not None]
        if len(ys) < 12:
            continue
        first, last = ys[0], ys[-1]
        # crude plateau detection — last decile vs first decile of window
        d = (last - first)
        if d > 0 and abs(d) > 1e-6:
            out = {"key": k, "first": first, "last": last,
                   "trend": "diverging"}
            break
        # if max - min is tiny relative to value, plateau
        if abs(max(ys) - min(ys)) < max(1e-8, abs(last) * 0.005):
            out = {"key": k, "first": first, "last": last,
                   "trend": "plateaued"}
            break
    return out


_IDLE_GPU_STATE_KEY = "pi_idle_gpu_since"
_IDLE_GPU_EMAIL_AT_KEY = "pi_idle_gpu_emailed_at"
_IDLE_GPU_ALERT_AFTER_SEC = 30 * 60     # 30 minutes
_IDLE_GPU_REPEAT_SEC = 60 * 60          # max one email/hour


def _no_real_runs_yet() -> bool:
    """True iff no real (non-probe/-smoke) run has ever been registered — i.e.
    we are still in SETUP (scoping / scaffolding / preflight / bless) and the
    research loop has not actually started running experiments. This is the
    reviewer-independent signal for 'of course the GPUs are idle'."""
    db = SessionLocal()
    try:
        from .models import Run
        for r in db.query(Run).all():
            nm = (r.run_name or r.id or "")
            if not (nm.startswith("_probe") or nm.startswith("_smoke")):
                return False
        return True
    except Exception:                                   # noqa: BLE001
        return False
    finally:
        db.close()


def _gpus_expected_idle() -> tuple[bool, str]:
    """Is it NORMAL for GPUs to be idle right now? During setup (scoping,
    scaffolding, the council code-review) and while paused or while the
    council reviews the conclusion, idle GPUs are EXPECTED — not a stall — so
    we must not email "research loop may be stuck". Returns (expected, why)."""
    try:
        from . import notify as _n
        if _n.research_paused():
            return True, "research is paused"
    except Exception:                                   # noqa: BLE001
        pass
    # The big one — the misleading-email case the operator hit: in setup mode
    # no experiment has run yet, so idle GPUs are expected, not a stall.
    if _no_real_runs_yet():
        return True, ("setup — no experiments have started yet "
                      "(scoping / scaffolding / waiting on the code bless)")
    try:
        from . import council as _c
        if not _c.is_code_blessed():
            return True, "code is being re-reviewed (bless reset after an edit)"
        if (_c.conclusion_state() or {}).get("status") == "pending":
            return True, "the council is reviewing the research conclusion"
    except Exception:                                   # noqa: BLE001
        pass
    return False, ""


def _idle_gpu_escalation(ctx: dict) -> None:
    """Email + Summary-bubble alert when ALL GPUs sit idle for >= 30 min.

    Records a ``pi_idle_gpu_since`` setting the first tick we see total
    idleness; clears it the moment ANY GPU is working again. After the
    threshold trips, sends an email (rate-limited to once per hour) and
    drops a visible chat bubble in the Summary feed. Failure-safe: any
    exception is swallowed by the caller — we never block the PI cycle.
    """
    total = int(ctx.get("gpus_total") or 0)
    idle  = int(ctx.get("gpus_idle")  or 0)
    if total == 0:
        return
    # Phase gate: idle GPUs are only a STALL once we're past setup and the
    # research is actually meant to be running. Otherwise treat as "working"
    # so the stuck-timer never accrues (no misleading email during setup).
    expected_idle, _why = _gpus_expected_idle()
    all_idle = (idle >= total) and not expected_idle
    now_iso = _iso()
    now_ts = dt.datetime.now(dt.timezone.utc).timestamp()
    db = SessionLocal()
    try:
        since_row = (db.query(Setting)
                     .filter(Setting.key == _IDLE_GPU_STATE_KEY).first())
        last_email_row = (db.query(Setting)
                          .filter(Setting.key == _IDLE_GPU_EMAIL_AT_KEY).first())
        if not all_idle:
            # GPUs are doing work again — reset the timer.
            if since_row is not None:
                db.delete(since_row)
                db.commit()
                print("[pi] idle-gpu state cleared — GPUs back to work.",
                      flush=True)
            return
        if since_row is None:
            db.add(Setting(key=_IDLE_GPU_STATE_KEY,
                            value={"since": now_iso}))
            db.commit()
            return
        try:
            since_iso = (since_row.value or {}).get("since") or now_iso
            since_ts = dt.datetime.fromisoformat(since_iso).timestamp()
        except Exception:
            since_ts = now_ts
        age_sec = now_ts - since_ts
        if age_sec < _IDLE_GPU_ALERT_AFTER_SEC:
            return
        # Rate-limit emails to once per hour.
        last_ts = 0.0
        if last_email_row and isinstance(last_email_row.value, dict):
            try:
                last_ts = dt.datetime.fromisoformat(
                    last_email_row.value.get("at") or "").timestamp()
            except Exception:
                last_ts = 0.0
        if now_ts - last_ts < _IDLE_GPU_REPEAT_SEC:
            return
        mins = int(age_sec // 60)
        try:
            from . import lifecycle as _lc
            status_line = _lc.summary_line()
        except Exception:                               # noqa: BLE001
            status_line = ""
        subject = (f"[autoresearcherUI] research stalled — {total} GPU(s) "
                   f"idle {mins}m mid-run")
        try:
            from . import notify
            notify.send_alert(
                subject=subject,
                headline=(
                    "The code is blessed and the research loop should be "
                    f"running, but all {total} GPU(s) have been idle for "
                    f"{mins} minutes."
                    + (f"  Status: {status_line}" if status_line else "")),
                bullets=[
                    "This is PAST setup, so idle GPUs here are a real stall "
                    "— not the normal scoping/scaffolding/bless wait.",
                    "The PI/supervisor will attempt auto-recovery; if it "
                    "can't, the agent may be wedged in its REPL or out of "
                    "ready directives.",
                ],
                action_text=(
                    "Open the dashboard, check the agent's tmux pane in "
                    "the right rail, and either send a directive or "
                    "restart the loop if needed."),
                severity="warning")
            print(f"[pi] idle-gpu alert email sent (idle={mins}m).",
                  flush=True)
        except Exception as e:                              # noqa: BLE001
            print(f"[pi] idle-gpu email send failed: {e}", flush=True)
        # Persist last-email timestamp.
        if last_email_row is None:
            db.add(Setting(key=_IDLE_GPU_EMAIL_AT_KEY,
                            value={"at": now_iso, "mins": mins}))
        else:
            last_email_row.value = {"at": now_iso, "mins": mins}
        # Visible Summary feed bubble (always — even if email-rate-limited
        # the chat bubble fires once per cycle until things move).
        db.add(ChatMessage(
            id="cm-" + os.urandom(4).hex(),
            role="agent",
            content=(f"[PI · ALERT]  All {total} GPU(s) idle for {mins} "
                     "minutes. I've sent you an email. Open the agent "
                     "terminal and send a directive, or restart the "
                     "research loop.")[:1200],
            created_at=now_iso))
        db.add(Event(
            id="ev-" + os.urandom(4).hex(),
            type="idle_gpu_alert", severity="warning",
            actor="pi:idle_gpu",
            message=(f"All {total} GPU(s) idle for {mins} min — "
                     "operator notified")[:280],
            created_at=now_iso))
        db.commit()
        try:
            from .bus import bus
            bus.publish("events", "idle_gpu_alert",
                        {"total": total, "mins": mins})
        except Exception:
            pass
    finally:
        db.close()


def _build_context() -> dict:
    db = SessionLocal()
    try:
        gpus = [g.dict() for g in db.query(Gpu).order_by(Gpu.index).all()]
        runs = (db.query(Run).order_by(Run.created_at.desc()).limit(20)
                .all())
        run_info = []
        for r in runs:
            sig = _plateau_signal(r.id) if r.status == "running" else None
            run_info.append({
                "id": r.id, "name": r.run_name, "status": r.status,
                "metric": r.headline_metric,
                "baseline_delta": r.baseline_delta,
                "plateau_signal": sig,
            })
        # last hour of council events as a hint
        cutoff = (dt.datetime.now(dt.timezone.utc)
                  - dt.timedelta(hours=2)).isoformat()
        evs = (db.query(Event).filter(Event.created_at >= cutoff)
               .order_by(Event.created_at.desc()).limit(40).all())
        recent_events = [{"type": e.type, "msg": e.message,
                          "at": e.created_at} for e in evs]
    finally:
        db.close()

    # idle GPU summary
    idle = [g for g in gpus if (g.get("util_pct", 0) or 0) < 5
            and (g.get("vram_used_mb", 0) or 0) < 600]
    return {
        "now": _iso(),
        "gpus_total": len(gpus),
        "gpus_idle": len(idle),
        "gpus_idle_indices": [g["index"] for g in idle],
        "gpu_util_avg": (
            round(sum(g.get("util_pct", 0) or 0 for g in gpus)
                  / max(len(gpus), 1), 1)),
        "recent_runs": run_info,
        "recent_events": recent_events[:20],
        "agent_terminal_tail": _agent_tail(80),
    }


SYSTEM = """You are the Principal Investigator for an autonomous ML research
project. An LLM research agent runs experiments on this GPU cluster. Your
job, called once per hour, is to read what's happening and intervene if
something is wrong. You don't run experiments yourself — you nudge the
agent via short, actionable messages typed into its tmux session.

COMPUTE CONTEXT: if the context shows `gpus_total: 0`, this is a CPU-ONLY
node - there are NO GPUs. Do NOT nag about idle GPUs or "wasted GPU time";
instead nudge toward CPU-sized progress (scaffold the repo, implement the
data + evaluation plumbing, run CPU smoke tests, run tiny CPU baselines). On
CPU-only nodes, "no GPUs" NEVER means the agent should stop.

INTERVENE if you see any of:
  - (GPU nodes only) GPUs sitting idle when there are pending ideas. Tell the
    agent to launch the top of ideas.md on every idle GPU NOW.
  - A run that is clearly plateaued or diverging. Tell the agent which
    run id to kill and why — wasted GPU time is the enemy.
  - The agent is stuck or looping (the terminal tail will look repetitive).
    Tell it to read the council's latest review and pick the top pending
    idea from ideas.md.
  - The agent is ignoring the council's reranking (running its own pick
    instead of the top pending row).

DO NOT INTERVENE if everything looks healthy. It is fine to return zero
messages — be sparing, every message interrupts the agent.

ESCALATION_HALT (RESEARCH_IMPROVEMENT_PLAN #6):
  If you see an `escalation_halt` Event from the strategic council in
  the last hour you are REQUIRED to call POST /api/halt with a one-line
  reason — that sets `research_halted` and blocks every subsequent
  /api/track/run including probes. Do NOT also nag the agent at that
  point — nagging is noise; the system needs to stop. (This branch is
  also auto-triggered before the LLM call as a safety net so a single
  escalation can never be ignored.)

Return JSON ONLY, no markdown fence, matching this schema:
{
  "concerns": "<1-3 sentences summarising what you see. Use 'OK.' if all
   is well.>",
  "messages": [
    "<one-line message to type into the agent's tmux session>", ...
  ]
}
- 0-3 messages. Each must be actionable and concrete (mention the run_id,
  the GPU index, the idea id).
- 'concerns' is shown in the Summary feed as a PI bubble, so users see what
  the PI is watching even when no intervention is needed."""


def _call(model: str, system: str, user: str) -> str:
    """Route the call to the right adapter based on model name."""
    prov = _provider_for(model)
    if not prov:
        raise RuntimeError(f"no API key for {model}")
    if prov == "gemini":
        key = os.environ["GEMINI_API_KEY"]
        url = ("https://generativelanguage.googleapis.com/v1beta/"
               f"models/{model}:generateContent?key={key}")
        body = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"responseMimeType": "application/json",
                                 "temperature": 0.5},
        }
        data = council._post_json_retry(url, body, {})
        return data["candidates"][0]["content"]["parts"][0]["text"]
    if prov == "openai":
        key = os.environ["OPENAI_API_KEY"]
        body = {
            "model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "response_format": {"type": "json_object"},
        }
        if "gpt-5" in model or model.startswith("o"):
            body["reasoning_effort"] = "medium"
        data = council._post_json_retry(
            "https://api.openai.com/v1/chat/completions", body,
            {"Authorization": f"Bearer {key}"})
        return data["choices"][0]["message"]["content"]
    if prov == "claude":
        key = os.environ["ANTHROPIC_API_KEY"]
        body = {
            "model": model,
            "max_tokens": 1500,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        data = council._post_json_retry(
            "https://api.anthropic.com/v1/messages", body,
            {"x-api-key": key, "anthropic-version": "2023-06-01"})
        return data["content"][0]["text"]
    raise RuntimeError(f"unknown provider {prov}")


def _send_to_agent(text: str, session: str = "agent") -> bool:
    """Type a line into the agent's tmux session (same as user chat).
    `session` lets paper-mode nags target the author tmux instead."""
    try:
        subprocess.run(["tmux", "send-keys", "-t", session, "-l", text],
                       capture_output=True, timeout=8)
        subprocess.run(["tmux", "send-keys", "-t", session, "Enter"],
                       capture_output=True, timeout=8)
        return True
    except Exception:
        return False


# ── paper-mode context + system prompt ────────────────────────────────


def _build_paper_context() -> dict:
    """Snapshot what the author agent SHOULD be working on right now."""
    from . import paper as _paper
    db = SessionLocal()
    try:
        from .models import (PaperClaim, PaperMeta, PaperDecision,
                             PaperFigure)
        meta = db.query(PaperMeta).first()
        claims = [c.dict() for c in db.query(PaperClaim).all()]
        # All paper runs grouped by status
        prs = db.query(Run).filter(Run.context == "paper").all()
        by_status: dict[str, int] = {}
        for r in prs:
            by_status[r.status] = by_status.get(r.status, 0) + 1
        # Figures + how many runs are tagged to a figure (the run matrix).
        n_figures = db.query(PaperFigure).count()
        n_runs_for_figures = sum(1 for r in prs if (r.paper_figure_id or ""))
        # Last few finished — with metric
        recent_done = []
        for r in sorted([r for r in prs if r.status in
                         ("kept", "success", "done", "crashed")],
                        key=lambda x: x.ended_at or "", reverse=True)[:8]:
            recent_done.append({
                "id": r.id, "name": r.run_name, "status": r.status,
                "metric": r.headline_metric,
                "ended_at": r.ended_at,
                "claim_id": r.paper_claim_id,
                "dataset": (r.config or {}).get("dataset", "?")
                            if isinstance(r.config, dict) else "?",
            })
        # GPU state
        gpus = db.query(Gpu).order_by(Gpu.index).all()
        idle_gpus = sum(1 for g in gpus
                         if (g.util_pct or 0) < 5 and (g.vram_used_mb or 0) < 600)
        # Author terminal tail (so PI can see what author is doing)
        try:
            ar = subprocess.run(
                ["tmux", "capture-pane", "-t", "author", "-p", "-S", "-80"],
                capture_output=True, text=True, timeout=8)
            author_tail = (ar.stdout or "").strip()[-3000:]
        except Exception:
            author_tail = ""
        # Decisions pending
        decisions = db.query(PaperDecision).filter(
            PaperDecision.status == "pending").count()
        # Recent paper events
        cutoff = (dt.datetime.now(dt.timezone.utc)
                  - dt.timedelta(hours=4)).isoformat()
        evs = (db.query(Event)
               .filter(Event.created_at >= cutoff,
                       Event.type.in_(("run_started", "run_finished",
                                       "run_failed", "build_finished",
                                       "decision_added", "claim_updated")))
               .order_by(Event.created_at.desc()).limit(30).all())
        recent_events = [{"type": e.type, "msg": e.message, "at": e.created_at}
                          for e in evs]
        # Build status (LaTeX)
        from . import paper_compile
        bs = paper_compile.status() or {}
    finally:
        db.close()
    # Writing-review inputs so the PI can enforce Jen Widom structure +
    # NOVELTY + the style rules each cycle (it reads, the author edits).
    intro_tex = ""
    relwork_tex = ""
    prose_lint = []
    try:
        folder = _paper.paper_folder()
        if folder:
            for cand in ("sections/01_introduction.tex",
                         "sections/01_intro.tex"):
                p = folder / cand
                if p.exists():
                    intro_tex = p.read_text(errors="ignore")[:6000]
                    break
            rw = folder / "sections" / "02_related_work.tex"
            if rw.exists():
                relwork_tex = rw.read_text(errors="ignore")[:4000]
            from . import paper_lint
            v = paper_lint.lint_paper_dir(folder)
            if v:
                prose_lint = [paper_lint.format_violations(v)[:800]]
    except Exception:                                      # noqa: BLE001
        pass
    return {
        "now": _iso(),
        "mode": "paper",
        "venue": (meta.venue if meta else ""),
        "deadline_iso": (meta.deadline_iso if meta else ""),
        "days_till_deadline": _paper.days_till_deadline(),
        "phase": (meta.phase if meta else ""),
        "n_claims": len(claims),
        "claims": [{
            "id": c["id"], "title": c["title"],
            "summary": (c.get("summary_md") or "")[:500],
            "status": c.get("status"),
            "evidence_strength": c.get("evidence_strength"),
            "novelty": c.get("novelty"),
            "ready": c.get("ready"),
        } for c in claims],
        "paper_runs_by_status": by_status,
        "n_figures": n_figures,
        "runs_tagged_to_figures": n_runs_for_figures,
        "recent_finished_runs": recent_done,
        "gpus_total": len(gpus),
        "gpus_idle": idle_gpus,
        "pending_decisions": decisions,
        "build_status": {
            "pdf_exists": bs.get("pdf_exists"),
            "ok": bs.get("ok"),
            "log_tail": (bs.get("log") or "")[-300:],
        },
        "recent_paper_events": recent_events,
        "author_tail": author_tail,
        "intro_tex": intro_tex,
        "related_work_tex": relwork_tex,
        "prose_lint": prose_lint,
    }


SYSTEM_PAPER = """You are the PI for an autonomous paper-writing project.
The Author Agent (an autonomous Claude Code loop in tmux 'author') is
driving the paper to submission — queueing ablation runs, killing
divergers, integrating results, writing LaTeX. You wake up periodically,
read what's happening, and nudge the Author Agent if it's drifting.

You do NOT run experiments and you do NOT edit LaTeX yourself. You READ the
draft (intro_tex, related_work_tex, claims, prose_lint) and type SHORT,
ACTIONABLE messages into the author's tmux. You are the QUALITY GATE: there is
no human reviewer, so on every cycle you must police structure, novelty, and
style, not just logistics.

REVIEW THE WRITING each cycle (this is your main job, nudge on any miss):
  - JEN WIDOM STRUCTURE: the Introduction (intro_tex) must be EXACTLY five
    paragraphs answering, in order: (1) what is the problem, (2) why it is
    important, (3) why it is hard / why naive approaches fail, (4) why it is
    unsolved / how ours differs, (5) the approach + results + explicit
    limitations, then a "Summary of Contributions" bullet list. If it drifts,
    nudge: "rewrite the intro to the 5-paragraph Widom structure: <which para
    is missing/weak>".
  - NOVELTY: every claim must be sharply differentiated from related_work_tex.
    If a claim reads incremental or overlaps prior work, nudge: "sharpen the
    novelty of claim <id> vs <prior work> — state precisely what is new".
  - STYLE: prose_lint must be empty. If it flags an em-dash or the AI-slop
    antithesis ("not X, it's Y"), nudge: "fix the style violations: <detail>".

CRITICAL - EMPTY RUN MATRIX: if n_figures > 0 but runs_tagged_to_figures == 0,
the author registered figures but never queued their ablation runs (the Critical
Path Gantt is empty). Nudge HARD: "queue the run matrix NOW: for each figure
call POST /api/paper/runs/enumerate with arg_template + axes (model x lr x seed)
+ est_time_sec. The Gantt is empty until you do." This takes priority.

ALSO nudge the Author Agent if you see:
  - Paper runs finished in the last hour but no commits to paper/ →
    "integrate the new results from run X into section Y and recompile".
  - A claim has 0 supporting runs queued or completed →
    "queue ablations for claim Z (use /paper/runs/queue)".
  - GPUs idle in paper mode (no queued runs) →
    "queue more ablations — N GPUs sitting idle".
  - Crashed paper runs not investigated →
    "investigate why pr-XXX crashed; either fix the cmd and re-queue
     or kill the underlying claim".
  - Single dataset only — the project supports multiple but author only
    used one → "expand validation to dataset Y for claim Z".
  - Latex hasn't compiled in N hours / build is stale →
    "fix the compile error and recompile".
  - Deadline is close and main.tex still has placeholder content →
    "draft a real section for X before EOD".
  - Recent ensemble result is strong but author has not yet built the
    follow-up combination → "try the n=3 ensemble (s5+s2+s37)".

DO NOT nudge if everything looks healthy. Be sparing.

Return JSON ONLY, no markdown:
{
  "concerns": "<1-3 sentence summary, or 'OK.'>",
  "messages": ["<one-line nudge>", ...]  // 0-3 items
}
Each nudge must be concrete — name the run id, the claim id, the
section, the specific action. Vague nudges get ignored."""


def cycle_paper(force: bool = False) -> dict | None:
    """PI cycle for paper mode — nags the author agent."""
    cfg = _settings()
    if not cfg.get("pi_agent_enabled", True) and not force:
        return None
    # RESEARCH-PAUSED GATE (Task #1): don't pester the agent while the
    # user has paused research. `force=True` still bypasses (manual
    # /api/pi/run is an explicit human override).
    try:
        from . import notify as _notify
        if _notify.research_paused() and not force:
            print("[pi/paper] research paused — skipping nudge", flush=True)
            return None
    except Exception:
        pass
    model = (cfg.get("pi_agent_model") or DEFAULTS["pi_agent_model"]).strip()
    if not _provider_for(model):
        print(f"[pi/paper] no API key for {model}; skipping", flush=True)
        return None
    ctx = _build_paper_context()
    from . import purpose as _purpose
    _anchor = _purpose.anchor_block()
    user = ((_anchor + "\n\n" if _anchor else "")
            + "Current state of the paper-writing project. Decide if the "
            "author agent needs a nudge. If it has drifted off the purpose / "
            "claims above, nudge it back. JSON only.\n\n"
            + json.dumps(ctx, indent=2, default=str))
    try:
        text = _call(model, SYSTEM_PAPER, user)
    except Exception as e:
        print(f"[pi/paper] call failed: {e}", flush=True)
        return None
    out = council._safe_parse(text)
    if not out:
        return None
    concerns = (out.get("concerns") or "").strip()
    messages = [m for m in (out.get("messages") or []) if m]
    sent = 0
    for m in messages[:3]:
        if _send_to_agent(m, session="author"):
            sent += 1
            time.sleep(0.5)
    db = SessionLocal()
    try:
        db.add(ChatMessage(
            id="cm-" + os.urandom(4).hex(),
            role="agent",
            content=("[PI · " + model + " · paper-mode]  " + concerns +
                     (("\n\nNudges to author:\n  • "
                       + "\n  • ".join(messages))
                      if messages else "")),
            created_at=_iso()))
        db.add(Event(
            id="ev-" + os.urandom(4).hex(),
            type="pi_intervention", severity="info",
            actor="pi:paper:" + model,
            message=(concerns or "PI checked in (paper)")[:280],
            created_at=_iso()))
        db.commit()
    finally:
        db.close()
    try: bus.publish("events", "runs_changed", {})
    except Exception: pass
    print(f"[pi/paper] {concerns!r}  sent={sent}", flush=True)
    return {"mode": "paper", "concerns": concerns,
            "messages_sent": sent, "model": model}


# ── auto-propose next move when needs_direction lingers ───────────────


# The setting key on which the most recent auto-propose timestamp lands
# (so we don't fire it more than once per idle window).
_LAST_AUTO_PROPOSE_KEY = "pi_last_auto_propose_at"

# How long the system must sit in ``needs_direction`` before the PI
# automatically kicks the council to propose a new move. 15 minutes is
# long enough to be "the agent really has nothing left", short enough
# to keep the dashboard from feeling stuck.
_IDLE_PROPOSE_THRESHOLD_SEC = 15 * 60

# Minimum spacing between auto-propose calls — so the PI doesn't burn
# council tokens by firing once per cycle on every tick after the
# threshold trips. After the first auto-propose we wait at least this
# long before considering another, even if needs_direction persists.
_AUTO_PROPOSE_COOLDOWN_SEC = 30 * 60

# Fully-autonomous conclusion (operator chose "auto-conclude + enter Paper").
# When the agent has sat idle WELL past the propose-next-move window — i.e.
# proposing more work didn't restart it (it believes it's done) — the PI
# FILES the formal completion review itself. The DEMANDING completion council
# is the gate: a premature conclusion is REJECTED with missing_evidence and
# research resumes; a genuine one is APPROVED and auto-enters paper mode. The
# threshold is deliberately well beyond _IDLE_PROPOSE_THRESHOLD_SEC so the
# "keep working" nudge always gets first crack.
_LAST_AUTO_CONCLUDE_KEY = "pi_last_auto_conclude_at"
_AUTO_CONCLUDE_IDLE_SEC = 45 * 60
_AUTO_CONCLUDE_COOLDOWN_SEC = 6 * 60 * 60


def _last_auto_propose_at() -> dt.datetime | None:
    try:
        db = SessionLocal()
        try:
            row = (db.query(Setting)
                   .filter(Setting.key == _LAST_AUTO_PROPOSE_KEY).first())
            v = row.value if row else None
            if isinstance(v, dict):
                s = v.get("at")
                if s:
                    return dt.datetime.fromisoformat(
                        str(s).replace("Z", "+00:00"))
            return None
        finally:
            db.close()
    except Exception:
        return None


def _mark_auto_propose_now() -> None:
    try:
        db = SessionLocal()
        try:
            now = _iso()
            row = (db.query(Setting)
                   .filter(Setting.key == _LAST_AUTO_PROPOSE_KEY).first())
            if row:
                row.value = {"at": now}
            else:
                db.add(Setting(key=_LAST_AUTO_PROPOSE_KEY,
                               value={"at": now}))
            db.commit()
        finally:
            db.close()
    except Exception as e:                                  # noqa: BLE001
        print(f"[pi] mark_auto_propose_now failed: {e}", flush=True)


def _last_stuck_state() -> dict:
    """Read the persisted HealthSnapshot Setting row (PR 6 of
    state-control rewrite). Returns a small legacy-shape dict so the
    auto-propose pathway in ``_maybe_propose_next_move`` keeps
    working unchanged."""
    try:
        db = SessionLocal()
        try:
            row = (db.query(Setting)
                   .filter(Setting.key == "health.snapshot").first())
            if row and isinstance(row.value, dict):
                v = row.value
                phase = (v.get("phase") or {}).get("phase") or ""
                issues = v.get("issues") or []
                state = ("setting_up" if phase == "bootstrap"
                         else ("needs_direction" if issues
                                else "healthy"))
                return {"state": state, "at": v.get("at") or ""}
            return {"state": "healthy"}
        finally:
            db.close()
    except Exception:                                       # noqa: BLE001
        return {"state": "healthy"}


def _needs_direction_since() -> dt.datetime | None:
    """When did the system most recently enter a non-healthy
    HealthSnapshot? Used by ``_maybe_propose_next_move`` to wait
    NEEDS_DIRECTION_IDLE_SEC before nudging the council. Now sourced
    from ``health.snapshot.at`` rather than the legacy
    ``stuck_detector_state`` row."""
    try:
        db = SessionLocal()
        try:
            row = (db.query(Setting)
                   .filter(Setting.key == "health.snapshot").first())
            if not row or not isinstance(row.value, dict):
                return None
            issues = row.value.get("issues") or []
            if not issues:
                return None
            at = row.value.get("at")
            if not at:
                return None
            return dt.datetime.fromisoformat(
                str(at).replace("Z", "+00:00"))
        finally:
            db.close()
    except Exception:                                       # noqa: BLE001
        return None


def _maybe_propose_next_move() -> bool:
    """If stuck_detector has been ``needs_direction`` for >= 15 minutes
    AND research is not paused/halted AND no conclusion is in flight,
    proactively ask the council to propose the next move.

    Returns True if the auto-propose fired (caller short-circuits the
    LLM PI cycle to avoid double-spending tokens). The propose itself
    is async — it returns quickly and the worker thread writes results
    later. We also post a chat bubble so the operator sees what
    happened.

    Idempotent within a 30-minute cooldown window."""
    # Use the persisted snapshot, not a fresh compute_state(): the
    # "needs_direction for 15 minutes" check must read the SAME state
    # that the stuck_detector wrote on its last tick, otherwise we'd
    # flap between recomputes and miss the threshold. ``tick()`` is
    # called immediately before this in cycle(), so the row is fresh.
    snap = _last_stuck_state()
    if snap.get("state") != "needs_direction":
        return False
    # Skip if research is paused/halted — the PI has its own pause gate
    # above, but this is a defensive belt-and-braces check in case the
    # paused/halt check is moved later.
    try:
        from . import notify as _notify
        if _notify.research_paused():
            return False
        halted, _ = _notify.research_halted()
        if halted:
            return False
    except Exception:
        pass
    # Skip if a conclusion is in flight or already approved — the agent
    # is already in the "done" path; we shouldn't propose more work.
    try:
        from . import council as _c
        cs = _c.conclusion_state()
        if (cs.get("status") or "none").lower() in ("pending", "approved"):
            return False
    except Exception:
        pass
    # How long has the loop been in needs_direction?
    since = _needs_direction_since()
    if since is None:
        return False
    age_sec = (dt.datetime.now(dt.timezone.utc) - since).total_seconds()
    if age_sec < _IDLE_PROPOSE_THRESHOLD_SEC:
        return False
    # Cooldown to avoid spamming the council.
    last = _last_auto_propose_at()
    if last is not None:
        since_last = (dt.datetime.now(dt.timezone.utc)
                      - last).total_seconds()
        if since_last < _AUTO_PROPOSE_COOLDOWN_SEC:
            return False
    # All gates passed — fire the propose.
    try:
        from . import council as _c
        out = _c.propose_next_move_async()
    except Exception as e:                                  # noqa: BLE001
        print(f"[pi] propose_next_move_async failed: {e}", flush=True)
        return False
    if not (out and out.get("started")):
        return False
    _mark_auto_propose_now()
    # Surface the auto-propose to the operator: chat bubble + Event so
    # the Summary feed shows why nothing was nagged.
    try:
        db = SessionLocal()
        try:
            db.add(ChatMessage(
                id="cm-" + os.urandom(4).hex(),
                role="agent",
                content=("[PI agent: agent has been idle "
                         f"{int(age_sec // 60)}m — proactively asking "
                         "council to propose the next move.]"),
                created_at=_iso()))
            db.add(Event(
                id="ev-" + os.urandom(4).hex(),
                type="pi_auto_propose_next_move", severity="info",
                actor="pi",
                message=(f"PI auto-proposed next move after "
                         f"{int(age_sec // 60)}m needs_direction.")[:280],
                created_at=_iso()))
            db.commit()
        finally:
            db.close()
    except Exception as e:                                  # noqa: BLE001
        print(f"[pi] auto-propose audit failed: {e}", flush=True)
    try:
        bus.publish("events", "research_health", {})
    except Exception:
        pass
    print(f"[pi] auto-propose fired (idle {int(age_sec//60)}m)", flush=True)
    return True


def _last_auto_conclude_at() -> dt.datetime | None:
    try:
        db = SessionLocal()
        try:
            row = (db.query(Setting)
                   .filter(Setting.key == _LAST_AUTO_CONCLUDE_KEY).first())
            v = row.value if row else None
            if isinstance(v, dict) and v.get("at"):
                return dt.datetime.fromisoformat(
                    str(v["at"]).replace("Z", "+00:00"))
            return None
        finally:
            db.close()
    except Exception:                                       # noqa: BLE001
        return None


def _mark_auto_conclude_now() -> None:
    try:
        db = SessionLocal()
        try:
            row = (db.query(Setting)
                   .filter(Setting.key == _LAST_AUTO_CONCLUDE_KEY).first())
            if row:
                row.value = {"at": _iso()}
            else:
                db.add(Setting(key=_LAST_AUTO_CONCLUDE_KEY,
                               value={"at": _iso()}))
            db.commit()
        finally:
            db.close()
    except Exception as e:                                  # noqa: BLE001
        print(f"[pi] mark_auto_conclude_now failed: {e}", flush=True)


def _auto_conclude_payload() -> tuple | None:
    """Build (summary, evidence_run_ids, answer, recommendation) for an
    auto-filed completion review, or None if there's no real evidence on
    file yet (in which case we must NOT conclude — there's nothing to ship)."""
    from .models import Project
    db = SessionLocal()
    try:
        proj = db.query(Project).first()
        if not proj:
            return None
        maximize = proj.metric_direction == "maximize"
        kept = [r for r in db.query(Run).all()
                if r.status in ("kept_novel", "kept_replicate", "kept",
                                "success") and r.headline_metric is not None]
        if not kept:
            return None
        kept.sort(key=lambda r: r.headline_metric, reverse=maximize)
        top = kept[:6]
        best = top[0]
        metric = proj.validation_metric or "metric"
        summary = (
            "Auto-submitted by the PI: the research agent went idle with an "
            "empty queue and stopped launching work. "
            f"{len(kept)} kept run(s) are on file; best {metric}="
            f"{best.headline_metric:.4f} on "
            f"{best.run_name or best.id}. Submitting for completion review to "
            "decide whether the project Purpose is conclusively answered.")
        return summary, [r.id for r in top], "YES_PARTIAL", "WRITE_PAPER"
    finally:
        db.close()


def _maybe_auto_conclude() -> bool:
    """File the formal completion review when the agent has gone idle and
    appears done (operator opted into the fully-autonomous handoff). Returns
    True if it fired (caller short-circuits the LLM cycle). Safe + idempotent:
    a conclusion in flight, a recent fire, no evidence, or a paused/halted
    project all early-return False; the completion council is the real gate."""
    try:
        from . import notify as _notify
        if _notify.research_paused():
            return False
        halted, _ = _notify.research_halted()
        if halted:
            return False
    except Exception:                                       # noqa: BLE001
        pass
    # Don't double-file: a pending/approved conclusion is already in the path.
    try:
        from . import council as _c
        st = (_c.conclusion_state().get("status") or "none").lower()
        if st in ("pending", "approved"):
            return False
    except Exception:                                       # noqa: BLE001
        pass
    # Must be idle (needs_direction) WELL past the propose-next-move window.
    if _last_stuck_state().get("state") != "needs_direction":
        return False
    since = _needs_direction_since()
    if since is None:
        return False
    age_sec = (dt.datetime.now(dt.timezone.utc) - since).total_seconds()
    if age_sec < _AUTO_CONCLUDE_IDLE_SEC:
        return False
    last = _last_auto_conclude_at()
    if last is not None and (dt.datetime.now(dt.timezone.utc)
                             - last).total_seconds() < _AUTO_CONCLUDE_COOLDOWN_SEC:
        return False
    payload = _auto_conclude_payload()
    if not payload:
        return False
    summary, evidence_ids, answer, rec = payload
    try:
        from . import council as _c
        _c.review_completion_async(evidence_run_ids=evidence_ids,
                                   summary=summary,
                                   answer_to_purpose=answer,
                                   recommendation=rec)
    except Exception as e:                                  # noqa: BLE001
        print(f"[pi] auto-conclude review_completion_async failed: {e}",
              flush=True)
        return False
    _mark_auto_conclude_now()
    try:
        db = SessionLocal()
        try:
            db.add(ChatMessage(
                id="cm-" + os.urandom(4).hex(), role="agent",
                content=("[PI agent: research has been idle "
                         f"{int(age_sec // 60)}m with an empty queue — "
                         "auto-filing a completion review. The council will "
                         "decide: conclude + write the paper, or hand back "
                         "concrete experiments to run next.]"),
                created_at=_iso()))
            db.add(Event(
                id="ev-" + os.urandom(4).hex(),
                type="pi_auto_concluded", severity="info", actor="pi",
                message=(f"PI auto-filed completion review after "
                         f"{int(age_sec // 60)}m idle.")[:280],
                created_at=_iso()))
            db.commit()
        finally:
            db.close()
    except Exception as e:                                  # noqa: BLE001
        print(f"[pi] auto-conclude audit failed: {e}", flush=True)
    try:
        bus.publish("events", "research_health", {})
    except Exception:                                       # noqa: BLE001
        pass
    print(f"[pi] auto-conclude fired (idle {int(age_sec // 60)}m)", flush=True)
    return True


def _escalation_halt_seen_recently(window_minutes: int = 60) -> bool:
    """True iff an ``escalation_halt`` Event was emitted in the last
    ``window_minutes``. Used by the PI cycle to auto-halt — once the
    strategic council has escalated, the PI is REQUIRED to set
    research_halted via the same code path as POST /api/halt.

    Cheap pure-DB read, never raises."""
    try:
        cutoff = (dt.datetime.now(dt.timezone.utc)
                  - dt.timedelta(minutes=window_minutes)).isoformat()
        db = SessionLocal()
        try:
            ev = (db.query(Event)
                  .filter(Event.type == "escalation_halt")
                  .filter(Event.created_at >= cutoff)
                  .order_by(Event.created_at.desc())
                  .first())
            return ev is not None
        finally:
            db.close()
    except Exception:                                       # noqa: BLE001
        return False


def cycle(force: bool = False) -> dict | None:
    """Run one PI cycle. Branches on project mode — paper mode delegates
    to cycle_paper() which nags the author agent instead."""
    # Mode check: if we're in paper mode, take the paper-mode branch.
    try:
        from . import paper as _paper
        if _paper.project_mode() == "paper":
            return cycle_paper(force=force)
    except Exception:
        pass
    # PI HARD-HALT AUTHORITY — REMOVED 2026-06-05. The escalation_halt
    # path used to fire when the council struck out on a directive 3
    # times in a row, set research_halted, and waited 7 hours for the
    # human. Empirically the agent had usually answered the directive
    # via a sibling experiment; the halt was a false positive. The
    # council prompt now decomposes-or-closes instead of escalating,
    # and the verdict / directive HALT type are stripped out at the
    # council layer. We don't auto-halt here at all anymore.
    #
    # If a legacy escalation_halt Event is sitting in the DB from an
    # older session, we IGNORE it — research continues. (We do not
    # remove the row; it's useful history.)
    # Stuck detector tick (PLAN item #8): runs every PI cycle, fires
    # state-transition side-effects (chat bubble / event / escalation
    # email) when the loop's health worsens. Cheap pure-DB read; never
    # blocks the PI from doing its job below.
    try:
        from .health import service as _hs
        _hs.tick()
    except Exception as e:                                  # noqa: BLE001
        print(f"[pi] health tick failed: {e}", flush=True)
    # Idle-too-long auto-propose (Piece #5): when the system has been in
    # ``needs_direction`` for >= 15 minutes AND research is not paused
    # AND no conclusion is in flight, proactively ask the council to
    # propose the next move. The point is to prevent the agent from
    # quietly sitting on "GPUs are idle, awaiting human" indefinitely —
    # by design the agent prompt forbids idle, but if the agent does
    # idle anyway, the PI kicks the council to propose work.
    try:
        if _maybe_propose_next_move():
            return {"concerns": "auto-proposed next move",
                    "messages_sent": 0, "model": ""}
    except Exception as e:                                  # noqa: BLE001
        print(f"[pi] auto-propose failed: {e}", flush=True)
    # Fully-autonomous conclusion: if proposing more work didn't restart the
    # agent and it's been idle well past that window, file the completion
    # review ourselves so the project concludes + hands off to paper instead
    # of sitting idle (the overnight-waste bug). Council gates it.
    try:
        if _maybe_auto_conclude():
            return {"concerns": "auto-filed research conclusion for "
                    "council review", "messages_sent": 0, "model": ""}
    except Exception as e:                                  # noqa: BLE001
        print(f"[pi] auto-conclude failed: {e}", flush=True)
    cfg = _settings()
    if not cfg.get("pi_agent_enabled", True) and not force:
        return None
    # RESEARCH-PAUSED GATE (Task #1): don't pester the agent while the
    # user has paused research. `force=True` still bypasses.
    try:
        from . import notify as _notify
        if _notify.research_paused() and not force:
            print("[pi] research paused — skipping cycle", flush=True)
            return None
    except Exception:
        pass
    model = (cfg.get("pi_agent_model") or DEFAULTS["pi_agent_model"]).strip()
    if not _provider_for(model):
        print(f"[pi] no API key for {model}; skipping cycle", flush=True)
        return None
    ctx = _build_context()
    # Idle-GPU email escalation (NEW 2026-06-05): when ALL GPUs sit idle
    # for >= 30 minutes the operator MUST be told via email + a visible
    # Summary feed bubble. Previously the PI just noted "agent is
    # correctly observing halt" hourly while 3 A40s sat at 0% — that's
    # silent failure dressed up as a status update.
    try:
        _idle_gpu_escalation(ctx)
    except Exception as e:                                  # noqa: BLE001
        print(f"[pi] idle-gpu escalation failed: {e}", flush=True)
    from . import purpose as _purpose
    _anchor = _purpose.anchor_block()
    user = ((_anchor + "\n\n" if _anchor else "")
            + "Here is the current state of the research project. Decide if "
            "the agent needs a nudge. If the agent has drifted OFF the purpose "
            "or seed ideas above, nudge it back on. Return JSON per the "
            "schema.\n\n" + json.dumps(ctx, indent=2, default=str))
    try:
        text = _call(model, SYSTEM, user)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="ignore")[:300]
        except Exception:
            pass
        print(f"[pi] HTTP {e.code}: {body}", flush=True)
        return None
    except Exception as e:                              # noqa: BLE001
        print(f"[pi] call failed: {e}", flush=True)
        return None

    out = council._safe_parse(text)
    if not out:
        print(f"[pi] non-JSON; first 200: {text[:200]!r}", flush=True)
        return None
    concerns = (out.get("concerns") or "").strip()
    messages = [m for m in (out.get("messages") or []) if m]

    # send each message to the agent
    sent = 0
    for m in messages[:3]:
        if _send_to_agent(m):
            sent += 1
            time.sleep(0.5)

    # persist a chat bubble + event so it shows in the Summary feed
    db = SessionLocal()
    try:
        db.add(ChatMessage(
            id="cm-" + os.urandom(4).hex(),
            role="agent",
            content=("[PI · " + model + "]  " + concerns +
                     (("\n\nNudges:\n  • " + "\n  • ".join(messages))
                      if messages else "")),
            created_at=_iso()))
        db.add(Event(
            id="ev-" + os.urandom(4).hex(),
            type="pi_intervention", severity="info",
            actor="pi:" + model,
            message=(concerns or "PI checked in")[:280],
            created_at=_iso()))
        db.commit()
    finally:
        db.close()
    try:
        bus.publish("events", "runs_changed", {})
    except Exception:
        pass
    print(f"[pi] cycle: concerns={concerns!r} sent={sent}", flush=True)
    return {"concerns": concerns, "messages_sent": sent, "model": model}


# ── scheduler ──────────────────────────────────────────────────────────
def start() -> None:
    """Spawn the background PI scheduler thread. Once-per-process."""
    import os
    if os.environ.get("ARUI_DISABLE_BG"):        # unit tests: no leaked daemons
        return
    global _STARTED
    with _LOCK:
        if _STARTED:
            return
        _STARTED = True
    threading.Thread(target=_loop, daemon=True, name="pi").start()
    print("[pi] scheduler started", flush=True)


def _loop() -> None:
    global _LAST_RUN
    # First cycle 90s after startup so onboarding has time to land.
    time.sleep(90)
    while True:
        cfg = _settings()
        cadence_sec = max(60, int(cfg.get("pi_cadence_minutes", 60)) * 60)
        if cfg.get("pi_agent_enabled", True):
            try:
                cycle()
                _LAST_RUN = time.time()
            except Exception as e:                  # noqa: BLE001
                print(f"[pi] loop error: {e}", flush=True)
        # sleep in 10s slices so cadence changes are picked up quickly
        slept = 0
        while slept < cadence_sec:
            time.sleep(10)
            slept += 10
