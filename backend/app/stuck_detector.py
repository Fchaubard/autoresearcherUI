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
# absence of every signal; the other five are emitted by compute_state.
HEALTHY = "healthy"
NEEDS_DIRECTION = "needs_direction"   # info, NOT a problem to fix
NAGGED = "nagged"
STALLED = "stalled"
LOOPING = "looping"
DEGRADED = "degraded"
DRY = "dry"

# Severity ordering — higher is worse. Used when we have multiple
# triggers firing at once so we surface the most actionable one.
# NEEDS_DIRECTION sits BELOW healthy in severity because it's a normal
# operating state ("agent has nothing in mandate, awaiting human") — we
# still want to surface it to the user but never want it to drown out
# a real fault like ``looping`` or ``stalled``. The dashboard pill
# treats it as a BLUE info colour (see static/style.css .rh-needs_direction).
_SEVERITY = {HEALTHY: 0, NEEDS_DIRECTION: 0,
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


def compute_state() -> dict:
    """Compute the current health of the research loop.

    Returns ``{"state": "...", "details": {...}, "reason": "..."}`` —
    never raises into the caller. Pure read against the DB and the
    workspace's ideas.md proxy.
    """
    details: dict = {}
    reasons: list[tuple[str, str]] = []          # (state, human reason)

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

    # ── looping vs needs_direction: distinguish "agent is currently
    # loop-launching duplicate configs" from "historical dups but agent
    # has correctly stopped". We use two RIGHT-NOW signals:
    #
    #   1) novelty rejections in the last hour. The /api/track/run gate
    #      emits a ``novelty_rejected`` Event every time it returns 409.
    #      If that count is 0 the agent has stopped trying to launch
    #      duplicates — the historical collision rate is stale data.
    #
    #   2) the agent's tmux pane text. If it contains a hold cue
    #      ("holding", "awaiting", "no novel", etc) we have strong
    #      evidence the agent is intentionally idle, not crashed.
    #
    # The state-classifier therefore reads:
    #
    #   recent_rejections == 0  AND  agent_state == "holding"
    #     -> needs_direction (BLUE / info — the GOOD outcome)
    #   recent_rejections > 0   AND  coll > 30 %  AND  no novel kept
    #     -> looping (RED / amber — the BAD outcome we used to fire
    #        on stale history)
    #   recent_rejections > 0   AND  novel kept run in window
    #     -> degraded (AMBER — partial loop with progress, warn only)
    #   coll > 30 %             AND  rejections == 0 but agent NOT holding
    #     -> looping (defensive: collisions but no exoneration signal)
    loop_runs = _recent_runs(LOOPING_WINDOW)
    coll = _collision_rate(loop_runs)
    details["collision_rate"] = round(coll, 3)
    details["collision_window"] = LOOPING_WINDOW

    pane_text = _agent_pane_text()
    agent_state = _classify_agent_state(pane_text)
    details["agent_state"] = agent_state

    recent_rejections = _recent_novelty_rejections()
    details["recent_novelty_rejections"] = recent_rejections
    details["recent_rejection_window_sec"] = RECENT_REJECTION_WINDOW_SEC

    # Novel kept runs in the dry window — used by both the dry classifier
    # below AND the degraded vs looping split here.
    dry_runs = _recent_runs(DRY_WINDOW)
    novel = _kept_novel_count(dry_runs)
    details["kept_novel_in_window"] = novel
    details["kept_novel_window"] = DRY_WINDOW

    if coll > LOOPING_COLLISION_RATE:
        # Pick which sub-state actually applies. The recent-rejection
        # signal (last hour) ALWAYS dominates the historical collision
        # rate (last 20 runs across all of time): if the agent has
        # STOPPED launching duplicates, the system isn't "looping"
        # even if it spent the last 30 minutes looping before stopping.
        # That distinction is the whole point of the
        # needs_direction state.
        if recent_rejections == 0:
            # No recent dup launches — agent has stopped (whether the
            # tmux pane reads "holding", is offline, or is empty).
            # Historical collision rate is stale; show needs_direction.
            tail = (" Agent pane suggests it's holding."
                    if agent_state == "holding" else "")
            reasons.append((NEEDS_DIRECTION,
                "Agent has stopped launching duplicate configs. "
                "Historical collision rate was "
                f"{int(coll * 100)}% but no novelty-gate rejection "
                f"in the last {RECENT_REJECTION_WINDOW_SEC // 60} min. "
                "Provide a new directive via the agent terminal "
                "(right rail) or pause research." + tail))
        elif novel > 0:
            reasons.append((DEGRADED,
                f"Agent is launching some novel configs but the "
                f"novelty gate is also rejecting duplicates "
                f"({recent_rejections} in the last "
                f"{RECENT_REJECTION_WINDOW_SEC // 60} min)."))
        else:
            reasons.append((LOOPING,
                f"{int(coll * 100)}% of the last {LOOPING_WINDOW} "
                f"finished runs are duplicate configurations AND the "
                f"agent is still trying to launch dups "
                f"({recent_rejections} novelty-gate rejections in the "
                f"last {RECENT_REJECTION_WINDOW_SEC // 60} min)."))
    else:
        # Collision rate is fine — but we may still want to surface
        # needs_direction if the agent is explicitly holding (GPUs
        # idle, no rejections, hold cue present). This catches the
        # case where the dashboard would otherwise read "healthy"
        # while the human is confused that nothing is running.
        if recent_rejections == 0 and agent_state == "holding":
            reasons.append((NEEDS_DIRECTION,
                "Agent is holding — it has no novel directive to run. "
                "Provide a new directive via the agent terminal "
                "(right rail) or pause research."))

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
        if not (state == NEEDS_DIRECTION and prev_state != NEEDS_DIRECTION):
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
        ev_type = "research_stuck" if state == STALLED \
            else "research_health"
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
        snap = compute_state()
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
