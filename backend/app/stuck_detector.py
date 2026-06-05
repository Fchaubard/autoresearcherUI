"""Honest "stuck" detector for the autonomous research loop (PLAN item #8).

This module is the missing accountability signal in the research loop. Today
the strategic review fires every N=GPU-count finished runs and emits the
same blocker over and over while the agent ignores it. Nothing on the
dashboard or in the digest *counts* those ignored verdicts — so the user
can't see "we've been stuck for 40 batches" without scrolling through chat.

`compute_state()` is the single source of truth. It returns one of:

  - ``healthy``  — nothing to worry about
  - ``nagged``   — same top open directive for >=3 strategic reviews
  - ``stalled``  — same top open directive for >=5 strategic reviews
                 (this also emits ESCALATION_HALT downstream)
  - ``looping``  — >30 % of recent runs are duplicate configurations
  - ``dry``      — no novel "kept" runs in the last 50 launched runs

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
# absence of every signal; the other four are emitted by compute_state.
HEALTHY = "healthy"
NAGGED = "nagged"
STALLED = "stalled"
LOOPING = "looping"
DRY = "dry"

# Severity ordering — higher is worse. Used when we have multiple
# triggers firing at once so we surface the most actionable one.
_SEVERITY = {HEALTHY: 0, NAGGED: 1, LOOPING: 2, DRY: 3, STALLED: 4}

# Thresholds from RESEARCH_IMPROVEMENT_PLAN.md section 8.
NAGGED_REVIEW_COUNT = 3
STALLED_REVIEW_COUNT = 5
LOOPING_WINDOW = 20
LOOPING_COLLISION_RATE = 0.30
DRY_WINDOW = 50

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

    # ── looping: novelty-hash collision rate over the last 20 finished
    # runs.  Uses headline_metric within 1% as the duplicate proxy.
    loop_runs = _recent_runs(LOOPING_WINDOW)
    coll = _collision_rate(loop_runs)
    details["collision_rate"] = round(coll, 3)
    details["collision_window"] = LOOPING_WINDOW
    if coll > LOOPING_COLLISION_RATE:
        reasons.append((LOOPING,
            f"{int(coll * 100)}% of the last {LOOPING_WINDOW} finished "
            f"runs are duplicate configurations."))

    # ── dry: no novel kept run in the last 50 launched runs.
    dry_runs = _recent_runs(DRY_WINDOW)
    novel = _kept_novel_count(dry_runs)
    details["kept_novel_in_window"] = novel
    details["kept_novel_window"] = DRY_WINDOW
    if dry_runs and novel == 0:
        reasons.append((DRY,
            f"No novel kept runs in the last {len(dry_runs)} launched "
            f"runs (frontier hasn't moved)."))

    # Resolve overlapping signals: surface the most severe one to the
    # user (e.g., stalled > dry > looping > nagged > healthy).
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
            "warning" if state in (NAGGED, LOOPING, DRY) else "info")
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
