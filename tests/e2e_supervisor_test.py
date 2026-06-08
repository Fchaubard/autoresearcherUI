#!/usr/bin/env python3
"""autoresearcherUI — PI supervisor end-to-end gate.

Proves, against the REAL code paths (no mocks of council/supervisor/lifecycle),
that the research can never get permanently blocked on a council review:

  1. A normal conclusion review runs to a terminal verdict and is observable
     (lifecycle phase set + a `council_launch` Event in the feed). It does NOT
     hang — the exact failure mode that wedged the research for ~2h45m.
  2. A genuinely STUCK review (pending, no live worker — i.e. the worker
     crashed / was orphaned by a backend restart before writing a verdict) is
     detected by the supervisor and auto-recovered: supervisor.tick() re-fires
     the review and the conclusion reaches a terminal verdict again.
  3. The lifecycle status surfaces a human-readable line for the digest.

Hermetic: with no council API keys the review auto-approves (fast, terminal),
so this needs no network. Pure standard library. Exit 0 = pass.

Usage:
    ARUI_AUTORUN=0 ARUI_DATA_DIR=$(mktemp -d) python tests/e2e_supervisor_test.py
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS: list[tuple[bool, str, str]] = []


def check(ok, label, detail=""):
    RESULTS.append((bool(ok), label, str(detail)))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}"
          + (f"  ::  {detail}" if detail else ""))


def _iso_ago(seconds: float) -> str:
    return (dt.datetime.now(dt.timezone.utc)
            - dt.timedelta(seconds=seconds)).isoformat()


def _wait_until(pred, timeout=15.0, interval=0.2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return pred()


def main() -> int:
    os.environ.setdefault("ARUI_AUTORUN", "0")
    os.environ.setdefault("ARUI_DATA_DIR", tempfile.mkdtemp(prefix="arui-sup-"))
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)

    from backend.app import db
    db.init_db()
    from backend.app import council, lifecycle, supervisor
    from backend.app.db import SessionLocal
    from backend.app.models import Event, Project
    # Hermetic: scrub any keys council (re)loaded from .deploy/keys.env at
    # import time, so the review takes the deterministic "no reviewers →
    # auto-approve" terminal path with no network. Must happen AFTER the
    # council import (that import is what reloads the keys).
    for k in ("GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        os.environ.pop(k, None)

    # minimal project so the review context can build
    s = SessionLocal()
    try:
        if not s.query(Project).first():
            s.add(Project(id="proj-e2e", name="e2e-supervisor",
                          validation_metric="val_loss",
                          metric_direction="minimize", status="running",
                          gpu_count=1))
            s.commit()
    finally:
        s.close()

    # ── 1. a normal review runs to a terminal verdict + is observable ───────
    council.review_completion_async(["run-1", "run-2"], "improved val_loss",
                                    "yes", "ship it")
    check(lifecycle.status().get("phase") == lifecycle.PHASE_CONCLUSION_REVIEW,
          "lifecycle phase = conclusion review on launch")
    ok = _wait_until(
        lambda: council.conclusion_state().get("status") != "pending")
    st = council.conclusion_state()
    check(ok and st.get("status") in ("approved", "rejected", "needs_more"),
          "completion review reached a TERMINAL verdict (no hang)",
          st.get("status"))

    db2 = SessionLocal()
    try:
        launched = (db2.query(Event)
                    .filter(Event.type == "council_launch").count())
    finally:
        db2.close()
    check(launched >= 1, "council_launch Event is in the activity feed",
          f"{launched} event(s)")

    # ── 2. a STUCK review (orphaned worker) is auto-recovered ───────────────
    # Simulate the deadlock: conclusion left 'pending' long ago, no live worker
    # lease (the worker crashed / the backend restarted before writing).
    council._conclusion_state_set({
        "status": "pending", "summary": "stuck conclusion",
        "answer_to_purpose": "yes", "evidence": ["run-1"],
        "recommendation": "ship", "conclude_at": _iso_ago(99999)})
    lifecycle.lease_release("completion_review")     # no live worker
    check(council.conclusion_state().get("status") == "pending",
          "review is wedged 'pending' before the supervisor runs")

    supervisor.tick()                                # the watchdog fires
    check(lifecycle.remediation_count("completion_review") >= 1,
          "supervisor recorded a remediation for the stuck review")
    recovered = _wait_until(
        lambda: council.conclusion_state().get("status") != "pending")
    st2 = council.conclusion_state()
    check(recovered and st2.get("status") != "pending",
          "supervisor UNBLOCKED the stuck review (terminal verdict)",
          st2.get("status"))

    # ── 3. status line is human-readable for the digest ─────────────────────
    line = lifecycle.summary_line()
    check(isinstance(line, str) and len(line) > 0,
          "lifecycle.summary_line() yields a digest status line", line)

    print()
    passed = sum(1 for ok, _, _ in RESULTS if ok)
    total = len(RESULTS)
    if passed == total:
        print(f"✅  supervisor e2e: {passed}/{total} checks passed.")
        return 0
    print(f"❌  supervisor e2e: {passed}/{total} passed.")
    for ok, label, detail in RESULTS:
        if not ok:
            print(f"   FAIL  {label}  ::  {detail}")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
