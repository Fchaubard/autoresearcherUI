"""Honest "stuck" detector for the autonomous research loop (PLAN item #8).

This module is the missing accountability signal in the research loop. Today
the strategic review fires every N=GPU-count finished runs and emits the
same blocker over and over while the agent ignores it. Nothing on the
dashboard or in the digest *counts* those ignored verdicts — so the user
can't see "we've been stuck for 40 batches" without scrolling through chat.

`compute_state()` is the single source of truth. It returns one of:

  - ``healthy``         — nothing to worry about
  - ``needs_direction`` — agent is holding because its current mandate has
                          no remaining novel work. The novelty-hash gate
                          is firing zero rejections recently AND the
                          agent tmux pane is signalling "holding/idle/
                          awaiting". This is INFORMATIONAL — the agent
                          is doing the right thing, the human needs to
                          provide a new directive (or pause research).
  - ``nagged``          — same top open directive for >=3 strategic reviews
  - ``stalled``         — same top open directive for >=5 strategic reviews
                          (this also emits ESCALATION_HALT downstream)
  - ``looping``         — >30 % of recent runs are duplicate configurations
                          AND the novelty gate is STILL rejecting fresh
                          launches (the agent is actively loop-launching
                          dups right now, not just past history).
  - ``degraded``        — novelty gate firing AND new novel configs being
                          produced — agent is partially looping but also
                          making forward progress; warn but don't block.
  - ``dry``             — no novel "kept" runs in the last 50 launched runs

The ordering above is the severity ordering: a project that is both
``looping`` and ``stalled`` reports ``stalled`` (most actionable for the
human). The plan calls for first-class directives + novelty hashes +
``kept_novel`` status (items #1, #3, #4). Those subsystems are not in
place yet, so this module deliberately operates on PROXIES that detect
the same production failure mode (the 40-batch diffusion-ensemble loop):

  - "top open directive" -> the first pending row in ``ideas.md`` for the
    onboarded repo, falling back to the most recent strategic-review
    ``Event`` blob.
  - "consecutive reviews on the same directive" -> consecutive
    ``strategic_review`` events with the same top idea / blocker text.
  - "novelty hash collision" -> proxy: more than one ``kept`` run sharing
    the same headline_metric within 1 % over the last 20 finished runs is
    treated as a likely duplicate config (matches the observed failure
    where the agent re-launched the same 5-way ensemble five times).
  - "kept_novel" -> proxy: any ``kept`` run whose headline metric differs
    from every prior ``kept`` run by >1 % is treated as novel-and-real.

This is intentionally conservative — false positives are worse than
false negatives because every transition emits a chat bubble + Event +
(for stalled) an immediate email. The thresholds match the spec in
RESEARCH_IMPROVEMENT_PLAN.md section 8.
"""
from __future__ import annotations

import datetime as dt
import os
import threading

from .bus import bus
from .db import SessionLocal
from .models import ChatMessage, Event, Run, Setting

# The user-visible state names in worst-first order. ``healthy`` is the
# absence of every signal; the other states are emitted by compute_state.
HEALTHY = "healthy"
SETTING_UP = "setting_up"             # info — agent is mid-SOP, healthy
NEEDS_DIRECTION = "needs_direction"   # info, NOT a problem to fix
NAGGED = "nagged"
STALLED = "stalled"
LOOPING = "looping"
DEGRADED = "degraded"
DRY = "dry"
# Conclusion-flow states. The agent has POSTed /api/research/conclude;
# while the council reviews we surface ``awaiting_completion_review``
# (purple/indigo info pill — research is paused on purpose, not a
# fault). When the council returns APPROVED we flip to ``complete``,
# a celebratory GREEN/trophy pill with the "Write the paper" CTA.
AWAITING_COMPLETION_REVIEW = "awaiting_completion_review"
COMPLETE = "complete"

# Severity ordering — higher is worse. Used when we have multiple
# triggers firing at once so we surface the most actionable one.
# NEEDS_DIRECTION sits BELOW healthy in severity because it's a normal
# operating state ("agent has nothing in mandate, awaiting human") — we
# still want to surface it to the user but never want it to drown out
# a real fault like ``looping`` or ``stalled``. The dashboard pill
# treats it as a BLUE info colour (see static/style.css .rh-needs_direction).
#
# AWAITING_COMPLETION_REVIEW and COMPLETE both sit at severity 0 with
# healthy — neither is a problem to fix — but compute_state() returns
# them DIRECTLY (short-circuiting the rest of the checks) when the
# conclusion-state row says so. They are NOT meant to lose to a stale
# nagged/looping signal: when the agent has declared done, that is the
# foreground story.
_SEVERITY = {HEALTHY: 0, SETTING_UP: 0, NEEDS_DIRECTION: 0,
             AWAITING_COMPLETION_REVIEW: 0, COMPLETE: 0,
             NAGGED: 1, LOOPING: 2, DEGRADED: 2, DRY: 3, STALLED: 4}

# Thresholds from RESEARCH_IMPROVEMENT_PLAN.md section 8.
NAGGED_REVIEW_COUNT = 3
STALLED_REVIEW_COUNT = 5
LOOPING_WINDOW = 20
LOOPING_COLLISION_RATE = 0.30
DRY_WINDOW = 50

# "Recent" window for novelty rejections — used by the needs_direction
# vs looping classifier. 1 hour matches the typical run duration on the
# pod (a fresh, on-mandate launch should rotate well inside 60 minutes).
RECENT_REJECTION_WINDOW_SEC = 3600

# How long ALL GPUs must sit idle (util < 5% AND vram < 1 GB) before
# we'll flip the dashboard to needs_direction. 10 min is long enough
# that a brief between-runs gap doesn't trip it, short enough that a
# real "agent gave up" situation surfaces quickly.
NEEDS_DIRECTION_IDLE_SEC = 10 * 60

# Minimum dwell time before the dashboard pill can flip again. Without
# this, the 8 s SSE tick and the sliding 20-run window combine to make
# the pill alternate healthy ↔ needs_direction every cycle, which the
# user reasonably called "kindof dumb". 2 min is enough that one tick
# of bad-data doesn't visibly flap the UI; a real persistent problem
# will still surface after this window.
STATE_TRANSITION_DEBOUNCE_SEC = 120

# Setting key that holds "since when have all GPUs been idle?". Shared
# with pi.py's _idle_gpu_escalation so the email + the dashboard pill
# agree on a single answer.
_IDLE_WINDOW_KEY = "stuck_idle_window_since"

# Setting key for the debounced state — protects against rapid flapping.
_DEBOUNCE_KEY = "stuck_debounce"


def _idle_window_state(idle_now: bool) -> str | None:
    """Track when the all-GPUs-idle window began. Returns the ISO start
    timestamp while we're inside an idle window, or ``None`` if any GPU
    is currently working. Persists in a Setting so this survives across
    cycle ticks (and across backend restarts)."""
    try:
        db = SessionLocal()
        try:
            row = (db.query(Setting)
                   .filter(Setting.key == _IDLE_WINDOW_KEY).first())
            if not idle_now:
                if row is not None:
                    db.delete(row); db.commit()
                return None
            if row is None:
                now_iso = _iso()
                db.add(Setting(key=_IDLE_WINDOW_KEY, value={"since": now_iso}))
                db.commit()
                return now_iso
            return (row.value or {}).get("since")
        finally:
            db.close()
    except Exception:                                       # noqa: BLE001
        return None


def _debounce_filter(snap: dict) -> dict:
    """Suppress rapid state flapping. Returns the snap unchanged on a
    cold start; on subsequent ticks, if the proposed new state differs
    from the persisted state AND the persisted state is younger than
    ``STATE_TRANSITION_DEBOUNCE_SEC``, returns the persisted snap so
    the dashboard pill stays stable. Once the debounce window expires
    the new state takes over."""
    try:
        prev = _last_state()
        prev_state = prev.get("state") or HEALTHY
        prev_at = prev.get("at") or ""
        if prev_state == snap["state"]:
            return snap
        try:
            age = (dt.datetime.now(dt.timezone.utc).timestamp()
                   - dt.datetime.fromisoformat(prev_at).timestamp())
        except Exception:
            return snap
        if age < STATE_TRANSITION_DEBOUNCE_SEC:
            # Hold the previous state; merge the new details for forensics.
            held = dict(prev)
            new_details = dict(snap.get("details") or {})
            new_details["debounced_proposed_state"] = snap["state"]
            new_details["debounced_proposed_reason"] = snap.get("reason", "")
            held["details"] = new_details
            return held
        return snap
    except Exception:                                       # noqa: BLE001
        return snap

# Keywords that, when seen in the trailing window of the agent's tmux
# pane, indicate the agent is INTENTIONALLY holding (the novelty gate +
# new prompt are working as designed). Treated as a strong signal for
# the ``needs_direction`` classifier: agent is awaiting human input, not
# stuck. Lowercased before matching.
_HOLD_KEYWORDS = (
    "holding",
    "i won't launch",       # exact phrase from the current pod's pane
    "i will not launch",
    "awaiting",
    "awaiting direction",
    "no novel",
    "no sound on-mandate",
    "gpus stay idle",
    "no remaining novel",
    "queue is empty",
    "nothing to launch",
    "no directive",
    "standing by",
    "idle — ",
    "idle--",
)

# Keywords that indicate the agent is in the SETUP phase — scaffolding code,
# running preflight checks, requesting bless. This is a NORMAL state for a
# brand-new project and must NOT trip needs_direction (no runs yet but the
# agent is actively working, so the operator shouldn't be told to "provide
# a directive"). Detected via tmux pane content because the project's
# directives.jsonl and runs table are both empty at this stage.
_SETUP_KEYWORDS = (
    "preflight",
    "static overfit",
    "static-batch overfit",
    "uniform init",
    "init probe",
    "scaffold",
    "scaffolding",
    "council bless",
    "request bless",
    "writing train.py",
    "writing prepare.py",
    "writing program.md",
    "writing ideas.md",
    "writing directives",
    "build m_bad",                  # current pod's task-list line
    "passing preflight",
)

# How many trailing lines of the agent pane to look at when classifying
# its current state. 200 captures roughly the last screenful + scroll.
_AGENT_PANE_LINES = 200

# How close two headline metrics must be to count as a "novelty collision"
# in the proxy. 1 % matches the spec language ("same metric value within
# 1 %").
NOVELTY_TOLERANCE = 0.01

# Setting key that persists the last reported state so on_state_transition
# only fires on a worsening change.
_STATE_KEY = "stuck_detector_state"

_LOCK = threading.Lock()


# ─────────────────────────── small helpers ────────────────────────────


def _iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _last_state() -> dict:
    """Last persisted compute_state() result (used to detect transitions)."""
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(Setting.key == _STATE_KEY).first()
        if row and isinstance(row.value, dict):
            return dict(row.value)
        return {"state": HEALTHY, "details": {}, "reason": ""}
    finally:
        db.close()


def _save_state(snap: dict) -> None:
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(Setting.key == _STATE_KEY).first()
        if row:
            row.value = snap
        else:
            db.add(Setting(key=_STATE_KEY, value=snap))
        db.commit()
    finally:
        db.close()


def _top_idea_signature() -> str:
    """Best-effort signature of the top open directive.

    Proxy: read the first pending row from ``ideas.md`` for the onboarded
    project. The line itself is the signature — when this string changes,
    the council successfully reranked or the agent implemented the top
    blocker. When it doesn't change across reviews, we know the agent
    ignored the council.
    """
    try:
        from .config import DATA_DIR  # local import to keep this file cheap

        db = SessionLocal()
        try:
            row = (db.query(Setting)
                   .filter(Setting.key == "onboarding").first())
            if not row or not isinstance(row.value, dict):
                return ""
            name = (row.value.get("repo_name") or "").strip()
        finally:
            db.close()
        if not name:
            return ""
        path = DATA_DIR / "workspace" / name / "ideas.md"
        if not path.exists():
            return ""
        for ln in path.read_text(errors="ignore").splitlines():
            s = ln.strip()
            if not s or not s.startswith("|"):
                continue
            cells = [c.strip() for c in s.strip("|").split("|")]
            if len(cells) < 2:
                continue
            if cells[0].lower() in ("status", "state"):
                continue
            if all(set(c) <= set("-: ") for c in cells):
                continue   # markdown separator row
            status_cell = cells[0].lower()
            if any(w in status_cell for w in
                   ("pending", "todo", "queued", "planned", "next")):
                # Use the idea id + body so HP tweaks still register as
                # "the same blocker" when only a column moves.
                return " | ".join(cells[1:3])[:160]
        return ""
    except Exception:                                       # noqa: BLE001
        return ""


def _strategic_reviews(limit: int = 10) -> list[Event]:
    """The most recent strategic_review Event rows, newest-first."""
    db = SessionLocal()
    try:
        return (db.query(Event)
                .filter(Event.type == "strategic_review")
                .order_by(Event.created_at.desc())
                .limit(limit).all())
    finally:
        db.close()


def _consecutive_review_streak(top_sig: str) -> int:
    """How many consecutive recent strategic reviews fired while the top
    directive signature was ``top_sig``.

    PROXY: we treat the council message itself as the signature when no
    explicit directive id is available. A streak is "the same top idea
    text appears in the trailing strategic_review messages". This is
    deliberately strict — small wording changes break the streak, which
    matches the spec's intent (we want to catch verbatim repetition,
    which is the production failure mode).

    When ``top_sig`` is empty (no ideas.md is present, which is the
    common case in fresh test/dev environments), we fall back to
    counting the trailing run of byte-identical council messages — the
    production smoking gun was "the council issued the SAME verdict 40
    times in a row", so verbatim repetition by itself is a stuck
    signal worth surfacing."""
    evs = _strategic_reviews(limit=STALLED_REVIEW_COUNT + 5)
    if not evs:
        return 0
    streak = 0
    # Path A: explicit directive signature appears in the message text.
    if top_sig:
        for e in evs:
            msg = (e.message or "").strip()
            if top_sig.lower() in msg.lower():
                streak += 1
                continue
            break
        if streak > 0:
            return streak

    # Path B: trailing verbatim repetition. The production failure mode
    # is "same council verdict over and over" — we count how many of the
    # most recent reviews carry the same message as the very latest one.
    first = (evs[0].message or "").strip()
    if not first:
        return 0
    for e in evs:
        if (e.message or "").strip() == first:
            streak += 1
        else:
            break
    return streak


def _recent_runs(window: int) -> list[Run]:
    """Most recent ``window`` runs by created_at (newest first)."""
    db = SessionLocal()
    try:
        return (db.query(Run)
                .order_by(Run.created_at.desc())
                .limit(window).all())
    finally:
        db.close()


def _collision_rate(runs: list[Run]) -> float:
    """Novelty-collision rate over a window of finished runs.

    Primary signal: when item #3 (novelty.py) is in play, each run's
    config carries a ``novelty_hash``. We count repeated hashes
    directly. When the hash field is missing (older runs, mocks) we
    fall back to a metric-equivalence proxy: two runs with finite
    headline_metrics inside ``NOVELTY_TOLERANCE`` are treated as the
    same config — this matches the observed failure mode (the same
    5-way ensemble re-launched five times produces five identical
    metrics).

    Returns 0.0 when there's no usable signal — never raises."""
    # Path A: real novelty_hash from config (preferred — directly measures
    # what the plan calls for).
    hashes: list[str] = []
    for r in runs:
        cfg = r.config if isinstance(r.config, dict) else {}
        h = cfg.get("novelty_hash") if isinstance(cfg, dict) else None
        if h:
            hashes.append(str(h))
    if len(hashes) >= 2:
        seen_h: set[str] = set()
        colliding = 0
        for h in hashes:
            if h in seen_h:
                colliding += 1
            else:
                seen_h.add(h)
        return colliding / max(len(hashes), 1)

    # Path B: metric-equivalence proxy.
    metrics = [r.headline_metric for r in runs
               if r.headline_metric is not None
               and r.status in ("kept", "kept_novel", "kept_replicate",
                                 "discarded", "crashed",
                                 "success", "success_smoke", "failed")]
    if len(metrics) < 2:
        return 0.0
    colliding = 0
    seen: list[float] = []
    for m in metrics:
        if any(_within_tol(m, prev) for prev in seen):
            colliding += 1
        seen.append(m)
    return colliding / max(len(metrics), 1)


def _within_tol(a: float, b: float, rel: float = NOVELTY_TOLERANCE) -> bool:
    """Two metrics counted as 'same config' under the proxy."""
    if a is None or b is None:
        return False
    if a == 0 and b == 0:
        return True
    denom = max(abs(a), abs(b), 1e-12)
    return abs(a - b) / denom < rel


def _agent_pane_text(lines: int = _AGENT_PANE_LINES) -> str:
    """Capture the trailing ``lines`` lines of the research agent's tmux
    pane. Returns "" on any failure (tmux not installed, session missing,
    permission denied, etc) — this signal is best-effort and must never
    crash the health probe.

    The session name is the same one ``pi._agent_tail`` uses ("agent").
    Lowercased here so downstream keyword matching is case-insensitive
    without each caller having to remember.
    """
    try:
        import subprocess
        out = subprocess.run(
            ["tmux", "capture-pane", "-t", "agent",
             "-p", "-S", str(-lines)],
            capture_output=True, text=True, timeout=4)
        return (out.stdout or "").lower()
    except Exception:                                       # noqa: BLE001
        return ""


def _classify_agent_state(pane_text: str) -> str:
    """One-word label for the agent's current intent, derived from the
    last few lines of its tmux pane.

    Returns one of:
      - ``holding``  — pane contains an explicit hold/idle/awaiting cue
                       (see ``_HOLD_KEYWORDS``). The agent is correctly
                       NOT launching because its mandate is exhausted.
      - ``working``  — pane is non-empty but no hold cue — assume the
                       agent is actively thinking / launching.
      - ``unknown``  — pane couldn't be captured (tmux not running, in
                       tests, etc).

    The classifier is intentionally coarse: production only needs to
    distinguish "agent says it's holding" from "agent is doing something
    else". Any new state machine (e.g. detecting "council waiting" or
    "compiling smoke") can be added later without changing the public
    contract."""
    if not pane_text:
        return "unknown"
    txt = pane_text.lower()
    # Check setup keywords FIRST — a pane that mentions both 'preflight'
    # (setup) and 'holding' (hold) is in the middle of setup and the setup
    # signal wins. The compute_state logic also uses 0-runs as a guard.
    for kw in _SETUP_KEYWORDS:
        if kw in txt:
            return "setting_up"
    for kw in _HOLD_KEYWORDS:
        if kw in txt:
            return "holding"
    return "working"


def _recent_novelty_rejections(seconds: int = RECENT_REJECTION_WINDOW_SEC
                                ) -> int:
    """Count ``novelty_rejected`` Event rows in the trailing window.

    This is the RIGHT-NOW signal that distinguishes "agent is currently
    loop-launching dups" from "agent USED to loop-launch but has now
    stopped (the new gate + prompt are working)". Without this signal
    the collision_rate alone is a stale, lagging indicator: 40 % of the
    last 20 finished runs can be dups while the agent has been holding
    for the last hour, and the dashboard would still scream LOOPING.

    Returns 0 on any DB error — health probe must never crash."""
    try:
        cutoff = (dt.datetime.now(dt.timezone.utc)
                  - dt.timedelta(seconds=seconds)).isoformat()
        db = SessionLocal()
        try:
            return (db.query(Event)
                    .filter(Event.type == "novelty_rejected")
                    .filter(Event.created_at >= cutoff)
                    .count())
        finally:
            db.close()
    except Exception:                                       # noqa: BLE001
        return 0


def _kept_novel_count(runs: list[Run]) -> int:
    """How many runs in the window are "kept and novel".

    Path A (preferred — uses item #4's taxonomy): count runs whose
    ``status == 'kept_novel'`` directly. When the taxonomy is in use
    every novel kept run gets this status at finish-time, so the
    counter is exact.

    Path B (proxy): when no run has the new status (older databases,
    mocks), fall back to "kept-class status + metric >1% away from every
    prior kept-class metric in the same window"."""
    # Path A: direct status check.
    real = [r for r in runs if r.status == "kept_novel"]
    if real:
        return len(real)
    # Path B: metric-equivalence proxy.
    kept = [r for r in runs if r.status in ("kept", "success")
            and r.headline_metric is not None]
    if not kept:
        return 0
    # Walk oldest -> newest so "novel" is judged against history.
    kept = list(reversed(kept))
    novel = 0
    prior: list[float] = []
    for r in kept:
        m = r.headline_metric
        if not any(_within_tol(m, p) for p in prior):
            novel += 1
        prior.append(m)
    return novel


# ─────────────────────────── public API ───────────────────────────────


def _conclusion_snapshot() -> dict:
    """Best-effort read of the ``research_conclusion`` Setting row.

    Returns the dict (with at least ``status``) or ``{"status": "none"}``
    on any error. Pure DB read — never raises into compute_state."""
    try:
        db = SessionLocal()
        try:
            row = (db.query(Setting)
                   .filter(Setting.key == "research_conclusion").first())
            if row and isinstance(row.value, dict):
                out = dict(row.value)
                out.setdefault("status", "none")
                return out
            return {"status": "none"}
        finally:
            db.close()
    except Exception:                                       # noqa: BLE001
        return {"status": "none"}


def compute_state() -> dict:
    """Compute the current health of the research loop.

    Returns ``{"state": "...", "details": {...}, "reason": "..."}`` —
    never raises into the caller. Pure read against the DB and the
    workspace's ideas.md proxy.
    """
    details: dict = {}
    reasons: list[tuple[str, str]] = []          # (state, human reason)

    # ── conclusion-flow short-circuit ─────────────────────────────────
    # When the agent has declared the project purpose answered, that is
    # the dominant story on the dashboard — even if the queue happens to
    # look ``nagged`` or ``looping`` from a stale prior window. The
    # operator wants to see either "council is reviewing" or "complete,
    # write the paper" prominently, NOT a yellow nagged pill that was
    # accurate ten minutes ago.
    cs = _conclusion_snapshot()
    cs_status = (cs.get("status") or "none").lower()
    if cs_status == "pending":
        verdict = cs.get("council_verdict") or {}
        details["conclusion"] = {
            "status": cs_status,
            "summary": (cs.get("summary") or "")[:600],
            "answer_to_purpose": cs.get("answer_to_purpose"),
            "evidence": cs.get("evidence") or [],
            "recommendation": cs.get("recommendation"),
            "conclude_at": cs.get("conclude_at"),
            "council_verdict": verdict,
        }
        return {"state": AWAITING_COMPLETION_REVIEW,
                "details": details,
                "reason": ("Agent declared research complete — council "
                           "reviewing evidence.")}
    if cs_status == "approved":
        verdict = cs.get("council_verdict") or {}
        details["conclusion"] = {
            "status": cs_status,
            "summary": (cs.get("summary") or "")[:600],
            "answer_to_purpose": cs.get("answer_to_purpose"),
            "evidence": cs.get("evidence") or [],
            "recommendation": cs.get("recommendation"),
            "conclude_at": cs.get("conclude_at"),
            "council_verdict": verdict,
        }
        short = (cs.get("summary") or "").strip().split("\n", 1)[0][:240]
        return {"state": COMPLETE,
                "details": details,
                "reason": (f"Research complete: {short}. Ready to write "
                           f"the paper.")}
    # ``rejected`` falls through — the agent should resume work; the
    # health probe goes back to its normal classification (so a rejected
    # conclusion + a stale looping signal can both surface). The agent
    # prompt tells the agent to read missing_evidence and upsert a new
    # SCIENCE directive.

    # ── setting_up short-circuit: brand-new project where the agent is
    # mid-SOP (scaffolding train.py, running preflight, requesting
    # bless). The dashboard would otherwise flag "needs_direction" because
    # there are zero kept runs AND the agent's tmux pane might match
    # generic hold cues — but the agent IS working, this is the normal
    # post-onboarding spinup. We require BOTH:
    #   • zero finished runs (project has never produced output), AND
    #   • either preflight is not blessed yet, OR the agent pane
    #     contains a setup keyword (init_probe.py, scaffolding,
    #     "passing preflight", etc).
    # This must run BEFORE the rejection/looping/needs_direction logic
    # below — otherwise a fresh project trips needs_direction on its
    # first hour and the operator sees a misleading "Provide a directive"
    # message while the agent is actually building the repo.
    try:
        from .models import Run as _Run, Setting as _Setting, Project as _Project
        _db = SessionLocal()
        try:
            total_kept = (_db.query(_Run)
                          .filter(_Run.status.in_(("kept_novel",
                                                   "kept_replication",
                                                   "kept",
                                                   "running"))).count())
            total_any = _db.query(_Run).count()
            # preflight bless state: api.py persists into Setting rows.
            bless_row = (_db.query(_Setting)
                         .filter(_Setting.key == "preflight_blessed").first())
            blessed = bool(bless_row and bless_row.value
                            and bless_row.value.get("blessed"))
            # "Has the operator actually onboarded?" — without a Project
            # row with a purpose, the agent isn't doing anything and
            # there's nothing to set up. Fresh-DB unit tests rely on
            # this guard returning "healthy" (the default reason).
            proj_row = _db.query(_Project).first()
            has_onboarded = bool(proj_row
                                  and (proj_row.purpose or "").strip())
        finally:
            _db.close()
    except Exception:                                       # noqa: BLE001
        total_kept = total_any = 0
        blessed = False
        has_onboarded = False

    # Peek at agent pane EARLY so the setting_up branch can use it.
    _pane_text_early = _agent_pane_text()
    _agent_state_early = _classify_agent_state(_pane_text_early)
    details["agent_state"] = _agent_state_early    # may be overwritten below

    # The setting_up gate trips in two cases:
    #   (a) Truly fresh: onboarded but no runs at all yet — definitely
    #       in SOP.
    #   (b) Pre-bless setup phase: agent pane explicitly shows setup
    #       activity (init_probe / overfit_smoke / scaffolding /
    #       preflight) AND preflight hasn't been blessed yet. The agent
    #       may have launched probe runs to verify the code works —
    #       those count as runs but are NOT real experiments. We do not
    #       want to call this "needs_direction" while the agent is
    #       actively building the rig.
    if has_onboarded and not blessed and (
            total_any == 0
            or _agent_state_early == "setting_up"):
        details["total_runs"] = total_any
        details["preflight_blessed"] = blessed
        return {"state": SETTING_UP,
                "details": details,
                "reason": ("Agent is in initial SOP — scaffolding code "
                           "and running preflight checks. This typically "
                           "takes 5–15 minutes; the dashboard will turn "
                           "green once preflight is blessed and the "
                           "first real experiment is launched.")}

    # ── stalled / nagged: count consecutive reviews on the same top dir.
    top_sig = _top_idea_signature()
    streak = _consecutive_review_streak(top_sig)
    details["top_directive"] = top_sig
    details["consecutive_unimplemented_reviews"] = streak
    if streak >= STALLED_REVIEW_COUNT:
        reasons.append((STALLED,
            f"{streak} consecutive strategic reviews on the same top "
            f"directive — escalating."))
    elif streak >= NAGGED_REVIEW_COUNT:
        reasons.append((NAGGED,
            f"{streak} consecutive strategic reviews on the same top "
            f"directive."))

    # ── needs_direction / looping (REWRITTEN 2026-06-05) ─────────────
    # The old detector tracked a "historical collision rate" (% of the
    # last 20 finished runs whose novelty_hash matched another run) and
    # flipped to needs_direction whenever the rate was high. That signal
    # is bad: deliberate replication, seed-replicate sweeps, and HP
    # ablations all SHARE configs by design, so the rate spikes high
    # any time the agent does its job. The pill then flapped
    # healthy↔needs_direction as the 20-run window slid.
    #
    # The new contract uses only RIGHT-NOW evidence:
    #
    #   • GPU work signal  — are any GPUs at >=5% util RIGHT NOW?
    #     (read from the same Gpu rows the monitor writes.) If yes,
    #     research is healthy regardless of pane text.
    #
    #   • idle-too-long    — all GPUs at <5% util for >=10 minutes AND
    #     agent pane shows a hold cue ("holding"/"awaiting"/...).
    #     That is the ONLY needs_direction trigger.
    #
    #   • looping (kept as a state but reframed) — there are >=3
    #     novelty-gate REJECTIONS in the last hour AND zero kept_novel
    #     runs in that same hour. The novelty *gate* is fine and useful
    #     (it returns 409 on identical-config submissions and the agent
    #     should retry with a tweak); but if the agent keeps banging on
    #     the same dup over and over without any novel run going
    #     through, that's a real loop. This needs RECENT rejections,
    #     not historical collisions.
    #
    # Collision rate is recorded in `details` for forensic value only —
    # it never drives a state transition.
    pane_text = _pane_text_early
    agent_state = _agent_state_early

    # Historical collision rate — purely informational, no longer drives
    # the state. Cheap to keep for the modal's diagnostics.
    try:
        _loop_runs = _recent_runs(LOOPING_WINDOW)
        details["collision_rate"] = round(_collision_rate(_loop_runs), 3)
        details["collision_window"] = LOOPING_WINDOW
    except Exception:                                       # noqa: BLE001
        details["collision_rate"] = 0.0
        details["collision_window"] = LOOPING_WINDOW

    recent_rejections = _recent_novelty_rejections()
    details["recent_novelty_rejections"] = recent_rejections
    details["recent_rejection_window_sec"] = RECENT_REJECTION_WINDOW_SEC

    dry_runs = _recent_runs(DRY_WINDOW)
    novel = _kept_novel_count(dry_runs)
    details["kept_novel_in_window"] = novel
    details["kept_novel_window"] = DRY_WINDOW

    # Read live GPU util / VRAM. If any GPU is doing meaningful work
    # right now, research is HEALTHY by definition — every other signal
    # is a forensic detail. The dashboard pill must reflect "is work
    # happening?", not "did the last 20 runs share novelty hashes?"
    try:
        from .models import Gpu as _Gpu
        _db = SessionLocal()
        try:
            _gpus = _db.query(_Gpu).all()
            gpus_total = len(_gpus)
            gpus_working = sum(
                1 for g in _gpus
                if (g.util_pct or 0) >= 5 or (g.vram_used_mb or 0) >= 1024)
        finally:
            _db.close()
    except Exception:                                       # noqa: BLE001
        gpus_total = 0
        gpus_working = 0
    details["gpus_total"] = gpus_total
    details["gpus_working"] = gpus_working

    # idle-too-long tracker (kept in the same Setting the email-alert
    # uses, so they agree on "since when") — only fires after 10 min
    # so a brief between-runs lull never trips needs_direction.
    idle_now = (gpus_total > 0 and gpus_working == 0)
    idle_since = _idle_window_state(idle_now)
    details["idle_since"] = idle_since
    idle_sec = 0
    if idle_since:
        try:
            idle_sec = max(
                0,
                int(dt.datetime.now(dt.timezone.utc).timestamp()
                    - dt.datetime.fromisoformat(idle_since).timestamp()))
        except Exception:
            idle_sec = 0
    details["idle_for_sec"] = idle_sec

    # Looping: recent rejections >= 3 AND zero novel runs in the same
    # hour. Drops the historical collision-rate dependency.
    if recent_rejections >= 3 and novel == 0:
        reasons.append((LOOPING,
            f"Novelty gate has rejected {recent_rejections} "
            f"duplicate-config submissions in the last "
            f"{RECENT_REJECTION_WINDOW_SEC // 60} min and no novel run "
            "has been kept — the agent is stuck on the same idea. "
            "Add a new directive or pause research."))
    # needs_direction: all GPUs idle for >= 10 min AND the pane shows a
    # hold cue. Both must be true — a brief pause between runs is OK.
    elif (gpus_total > 0 and gpus_working == 0
          and idle_sec >= NEEDS_DIRECTION_IDLE_SEC
          and agent_state == "holding"):
        mins = idle_sec // 60
        reasons.append((NEEDS_DIRECTION,
            f"All {gpus_total} GPU(s) have been idle for {mins} min and "
            "the agent's tmux pane shows it's holding (no directive to "
            "run). Open the agent terminal in the right rail and either "
            "send a new directive or pause research."))

    # ── dry: no novel kept run in the last 50 launched runs.
    if dry_runs and novel == 0:
        reasons.append((DRY,
            f"No novel kept runs in the last {len(dry_runs)} launched "
            f"runs (frontier hasn't moved)."))

    # Resolve overlapping signals: surface the most severe one to the
    # user (e.g., stalled > dry > looping > nagged > healthy). When the
    # ONLY signal is needs_direction we surface it — but it ties with
    # healthy on severity, so any real fault wins.
    if not reasons:
        return {"state": HEALTHY, "details": details,
                "reason": "All systems nominal."}
    reasons.sort(key=lambda r: _SEVERITY[r[0]], reverse=True)
    state, reason = reasons[0]
    return {"state": state, "details": details, "reason": reason}


def on_state_transition(state: str, prev_state: str,
                         snap: dict | None = None) -> None:
    """Side-effects when the health worsens: chat bubble + Event +
    (if escalation) immediate email.

    Only fires on a WORSENING transition — going from ``stalled`` back to
    ``healthy`` is a relief and we let the digest carry that signal so
    we don't ping the user twice. ``snap`` is the full compute_state()
    output, used to enrich the chat bubble.
    """
    if _SEVERITY.get(state, 0) <= _SEVERITY.get(prev_state, 0):
        # Worsening severity is the canonical trigger — BUT we always
        # want the user to see a transition INTO needs_direction at
        # least once, because it's a "GPUs are idle on purpose" signal
        # the human needs to act on (provide a new directive or pause
        # the loop). Without this exception the pill would silently
        # stay green/amber and the user would never learn why nothing
        # is running.
        #
        # The conclusion-flow transitions (awaiting_completion_review
        # and complete) carry the same exception — both are positive
        # info-events the user MUST see immediately ("agent says it's
        # done", "council blessed it — go write the paper"). They sit
        # at severity 0 alongside healthy so without this branch a
        # healthy→complete transition would be silently swallowed.
        info_state_first_entry = (
            state in (NEEDS_DIRECTION, AWAITING_COMPLETION_REVIEW, COMPLETE)
            and prev_state != state)
        if not info_state_first_entry:
            return  # transition didn't worsen — nothing to do.
    snap = snap or {"state": state, "details": {}, "reason": ""}
    reason = snap.get("reason") or f"Research health is now {state}."

    # 1) ChatMessage — shows up in the Summary feed as a researcher bubble.
    db = SessionLocal()
    try:
        bubble = (f"[Research health · {state.upper()}]  {reason}")[:1200]
        db.add(ChatMessage(id="cm-" + os.urandom(4).hex(),
                           role="agent", content=bubble,
                           created_at=_iso()))
        sev = "critical" if state == STALLED else (
            "warning" if state in (NAGGED, LOOPING, DEGRADED, DRY)
            else "info")
        if state == STALLED:
            ev_type = "research_stuck"
        elif state == AWAITING_COMPLETION_REVIEW:
            ev_type = "research_awaiting_completion_review"
        elif state == COMPLETE:
            ev_type = "research_complete"
        else:
            ev_type = "research_health"
        db.add(Event(id="ev-" + os.urandom(4).hex(),
                     type=ev_type, severity=sev, actor="stuck_detector",
                     message=(f"{state}: {reason}")[:280],
                     created_at=_iso()))
        db.commit()
    finally:
        db.close()

    # 2) Live bus event so the dashboard pill updates without a refresh.
    try:
        bus.publish("events", "research_health", {"state": state,
                                                    "reason": reason})
    except Exception:                                      # noqa: BLE001
        pass

    # 3) Email-immediate escalation when we've crossed into stalled.
    if state == STALLED:
        try:
            from . import notify
            subject = "[autoresearcherUI] research STALLED — manual intervention"
            text = (
                f"The research loop is STALLED.\n\n{reason}\n\n"
                f"Top directive: {snap['details'].get('top_directive', '?')}\n"
                f"Consecutive ignored reviews: "
                f"{snap['details'].get('consecutive_unimplemented_reviews', '?')}\n\n"
                "Open the dashboard and inspect the Council health section "
                "of the latest digest.\n\n- autoresearcherUI"
            )
            notify.send(subject, text)
        except Exception as e:                              # noqa: BLE001
            print(f"[stuck_detector] escalation email failed: {e}",
                  flush=True)


def tick() -> dict:
    """Compute the current state and, if it worsened since last call,
    fire on_state_transition. Returns the current snapshot.

    Designed to be called from ``pi.cycle()`` every PI tick — cheap, pure
    DB reads + one file read. Thread-safe across pi.cycle and api calls.
    """
    with _LOCK:
        raw_snap = compute_state()
        # Debounce rapid state flips. Without this the 8 s SSE tick
        # combined with the sliding GPU-util window could make the
        # dashboard pill alternate healthy ↔ needs_direction every
        # cycle (Francois 2026-06-05).
        snap = _debounce_filter(raw_snap)
        prev = _last_state()
        prev_state = prev.get("state") or HEALTHY
        if snap["state"] != prev_state:
            on_state_transition(snap["state"], prev_state, snap)
        # Always update the persisted snapshot — even healthy → healthy —
        # so the timestamp on the dashboard pill is fresh.
        snap_to_save = dict(snap)
        snap_to_save["at"] = _iso()
        try:
            _save_state(snap_to_save)
        except Exception as e:                              # noqa: BLE001
            print(f"[stuck_detector] save_state failed: {e}", flush=True)
        return snap
