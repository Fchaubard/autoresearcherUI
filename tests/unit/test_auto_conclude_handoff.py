"""Fully-autonomous research→paper handoff (operator chose 'auto-conclude +
enter Paper').

Two bridges:
  A. pi._maybe_auto_conclude() files the formal completion review when the
     agent has gone idle well past the propose-next-move window.
  B. council._auto_enter_paper_after_approval() flips into paper mode when a
     conclusion is APPROVED with a WRITE_PAPER recommendation.

The demanding completion council remains the real gate, so these tests focus
on the GATING + wiring, not on second-guessing the reviewers.
"""
from __future__ import annotations

import datetime as dt


def _iso(min_ago: float = 0) -> str:
    return (dt.datetime.now(dt.timezone.utc)
            - dt.timedelta(minutes=min_ago)).isoformat()


# ── Bridge A: pi._maybe_auto_conclude gating ────────────────────────────────

def test_auto_conclude_payload_none_without_kept_runs(arui_env, make_project,
                                                      make_run):
    from backend.app import pi
    make_project()
    make_run(status="discarded", headline_metric=0.5)
    make_run(status="success_smoke", headline_metric=0.1)
    assert pi._auto_conclude_payload() is None


def test_auto_conclude_payload_picks_best_kept(arui_env, make_project,
                                               make_run):
    from backend.app import pi
    make_project(metric_direction="minimize", validation_metric="loss")
    make_run(id="r-bad", status="kept_novel", headline_metric=0.9)
    make_run(id="r-best", status="kept_replicate", headline_metric=0.2)
    out = pi._auto_conclude_payload()
    assert out is not None
    summary, evidence_ids, answer, rec = out
    assert evidence_ids[0] == "r-best"          # best (min) first
    assert answer == "YES_PARTIAL" and rec == "WRITE_PAPER"
    assert "0.2" in summary


def test_auto_conclude_skips_when_not_idle(arui_env, make_project, make_run,
                                           monkeypatch):
    from backend.app import pi
    make_project()
    make_run(status="kept_novel", headline_metric=0.3)
    # health snapshot says healthy (not needs_direction) -> must NOT conclude
    monkeypatch.setattr(pi, "_last_stuck_state", lambda: {"state": "healthy"})
    fired = {"n": 0}
    monkeypatch.setattr(pi.council, "review_completion_async",
                        lambda **k: fired.__setitem__("n", fired["n"] + 1))
    assert pi._maybe_auto_conclude() is False
    assert fired["n"] == 0


def test_auto_conclude_skips_when_conclusion_pending(arui_env, make_project,
                                                     make_run, monkeypatch):
    from backend.app import pi
    make_project()
    make_run(status="kept_novel", headline_metric=0.3)
    monkeypatch.setattr(pi, "_last_stuck_state",
                        lambda: {"state": "needs_direction"})
    monkeypatch.setattr(pi, "_needs_direction_since", lambda: None)
    # even with a stale 'since', a pending conclusion short-circuits first
    monkeypatch.setattr(pi.council, "conclusion_state",
                        lambda: {"status": "pending"})
    fired = {"n": 0}
    monkeypatch.setattr(pi.council, "review_completion_async",
                        lambda **k: fired.__setitem__("n", fired["n"] + 1))
    assert pi._maybe_auto_conclude() is False
    assert fired["n"] == 0


def test_auto_conclude_fires_after_long_idle(arui_env, make_project, make_run,
                                             monkeypatch):
    from backend.app import pi
    make_project(metric_direction="maximize", validation_metric="acc")
    make_run(id="r1", status="kept_novel", headline_metric=0.8)
    monkeypatch.setattr(pi, "_last_stuck_state",
                        lambda: {"state": "needs_direction"})
    # idle for 90 minutes — past the 45m auto-conclude threshold
    monkeypatch.setattr(pi, "_needs_direction_since",
                        lambda: dt.datetime.now(dt.timezone.utc)
                        - dt.timedelta(minutes=90))
    monkeypatch.setattr(pi.council, "conclusion_state",
                        lambda: {"status": "none"})
    captured = {}
    monkeypatch.setattr(pi.council, "review_completion_async",
                        lambda **k: captured.update(k))
    assert pi._maybe_auto_conclude() is True
    assert captured.get("evidence_run_ids") == ["r1"]
    assert captured.get("recommendation") == "WRITE_PAPER"


def test_auto_conclude_respects_pause(arui_env, make_project, make_run,
                                      monkeypatch):
    from backend.app import pi, notify
    make_project()
    make_run(status="kept_novel", headline_metric=0.3)
    monkeypatch.setattr(notify, "research_paused", lambda: True)
    monkeypatch.setattr(pi, "_last_stuck_state",
                        lambda: {"state": "needs_direction"})
    monkeypatch.setattr(pi, "_needs_direction_since",
                        lambda: dt.datetime.now(dt.timezone.utc)
                        - dt.timedelta(minutes=90))
    assert pi._maybe_auto_conclude() is False


# ── Bridge B: council._auto_enter_paper_after_approval gating ───────────────

def test_auto_enter_paper_skips_non_write_recommendation(arui_env,
                                                         monkeypatch):
    from backend.app import council, paper
    called = {"n": 0}
    monkeypatch.setattr(paper, "enter_paper_mode",
                        lambda **k: called.__setitem__("n", called["n"] + 1))
    council._auto_enter_paper_after_approval("NEED_ORTHOGONAL_DIRECTION", "x")
    council._auto_enter_paper_after_approval("NEED_MORE_DATA", "x")
    assert called["n"] == 0


def test_auto_enter_paper_fires_for_write_paper(arui_env, monkeypatch):
    from backend.app import council, paper
    monkeypatch.setattr(paper, "project_mode", lambda: "research")
    called = {}
    monkeypatch.setattr(paper, "enter_paper_mode",
                        lambda **k: called.update(k) or {"status": "entered_paper_mode"})
    council._auto_enter_paper_after_approval("WRITE_PAPER", "great result")
    assert called.get("reason", "").startswith("auto-handoff")


def test_auto_enter_paper_noop_when_already_paper(arui_env, monkeypatch):
    from backend.app import council, paper
    monkeypatch.setattr(paper, "project_mode", lambda: "paper")
    called = {"n": 0}
    monkeypatch.setattr(paper, "enter_paper_mode",
                        lambda **k: called.__setitem__("n", called["n"] + 1))
    council._auto_enter_paper_after_approval("WRITE_PAPER", "x")
    assert called["n"] == 0
