"""Unit tests for the council-health section of the research-mode digest
(PLAN item #9).

The digest must:
  - open with a "### Council health" section in the text/plain body
  - have the colour-coded HTML block in the HTML body
  - bold the status (STALLED / HEALTHY / NAGGED) so the user notices
  - bypass the cadence setting when state == 'stalled' (immediate send)
"""
from __future__ import annotations

import datetime as dt


def _iso(seconds_ago: float = 0) -> str:
    return (dt.datetime.now(dt.timezone.utc)
            - dt.timedelta(seconds=seconds_ago)).isoformat()


def _make_research_project(make_project):
    """Helper: research-mode project with a real metric for the digest
    to chew on."""
    return make_project(name="proj", validation_metric="val_acc",
                         metric_direction="maximize")


# ───────────────────────── council-health section ─────────────────────


def test_digest_includes_council_health_section_text(
        arui_env, db_session, make_project, monkeypatch):
    """text/plain digest opens with the '### Council health' header."""
    from backend.app import notify
    _make_research_project(make_project)
    # No stuck signals → council-health says HEALTHY.
    subj, text, html, _images = notify.digest_email(1.0)
    assert subj is not None
    assert "### Council health" in text
    assert "HEALTHY" in text


def test_digest_health_status_promotes_to_stalled(
        arui_env, db_session, make_project, monkeypatch):
    """When the health service surfaces a critical issue, digest body
    bolds STALLED. (Updated 2026-06-05 — PR 6 of state-control rewrite
    routes the digest's council_health snapshot through health.service
    instead of stuck_detector.)"""
    from backend.app import notify
    from backend.app.health import service as _hs, schema as _hsch
    _make_research_project(make_project)
    fake_snap = _hsch.HealthSnapshot(
        phase=_hsch.Phase(phase="watching_runs",
                           at="2026-06-05T00:00:00Z",
                           detail={}, fallback_used=False),
        summary="12 ignored reviews",
        issues=[_hsch.Issue(
            code="directives_ignored",
            severity=_hsch.SEV_CRITICAL,
            summary="12 consecutive strategic reviews on same directive",
            evidence={"streak": 12,
                       "top_signature": "build trusted_eval"},
            since="2026-06-05T00:00:00Z")],
        facts={})
    monkeypatch.setattr(_hs, "compute", lambda: fake_snap)
    subj, text, html, _images = notify.digest_email(1.0)
    assert "STALLED" in text
    assert "Status:" in html
    assert "12 consecutive strategic review" in text


def test_digest_health_lists_open_halt_count(
        arui_env, db_session, make_project, monkeypatch):
    """The Open HALT directives line is present and shows the count of
    research_stuck events as the proxy until directives.jsonl ships."""
    import os
    from backend.app import notify
    from backend.app.models import Event
    _make_research_project(make_project)
    db_session.add(Event(id=f"ev-{os.urandom(4).hex()}",
                         type="research_stuck", severity="critical",
                         actor="stuck_detector",
                         message="stalled: 5 ignored",
                         created_at=_iso()))
    db_session.commit()
    subj, text, html, _images = notify.digest_email(1.0)
    # Some integer ≥1 is listed for Open HALT directives.
    assert "Open HALT directives: 1" in text


def test_council_health_section_renders_above_baseline_cards_html(
        arui_env, db_session, make_project, monkeypatch):
    """In the HTML body, 'Council health' appears BEFORE the baseline
    progress paragraph so the user sees status first."""
    from backend.app import notify
    _make_research_project(make_project)
    subj, text, html, _images = notify.digest_email(1.0)
    pos_health = html.find("Council health")
    pos_progress = html.find("has progressed over")
    assert pos_health != -1 and pos_progress != -1
    assert pos_health < pos_progress


# ────────────────────── cadence override on stalled ──────────────────


def test_stalled_override_forces_send_regardless_of_cadence(
        arui_env, db_session, make_project, monkeypatch):
    """When the health service surfaces a critical issue the scheduler
    must call send() even though user cadence is 'off'.
    (Updated 2026-06-05 — PR 6 routes _stalled_override_active through
    health.service instead of stuck_detector.)"""
    from backend.app import notify
    from backend.app.health import service as _hs, schema as _hsch
    _make_research_project(make_project)
    fake = _hsch.HealthSnapshot(
        phase=_hsch.Phase(phase="watching_runs",
                           at="2026-06-05T00:00:00Z",
                           detail={}, fallback_used=False),
        summary="stalled",
        issues=[_hsch.Issue(
            code="directives_ignored",
            severity=_hsch.SEV_CRITICAL,
            summary="7 ignored reviews",
            evidence={"streak": 7, "top_signature": "x"},
            since="2026-06-05T00:00:00Z")],
        facts={})
    monkeypatch.setattr(_hs, "compute", lambda: fake)
    assert notify._stalled_override_active() is True


def test_healthy_override_does_not_force_send(
        arui_env, db_session, make_project, monkeypatch):
    """Healthy state does not trip the stalled override — normal
    cadence applies."""
    from backend.app import notify, stuck_detector
    _make_research_project(make_project)
    monkeypatch.setattr(stuck_detector, "compute_state",
        lambda: {"state": "healthy", "details": {}, "reason": "ok"})
    assert notify._stalled_override_active() is False


def test_last_kept_age_reports_never_when_no_kept_run(
        arui_env, db_session, make_project):
    """_last_kept_age returns 'never' when no kept run exists."""
    from backend.app import notify
    _make_research_project(make_project)
    assert notify._last_kept_age() == "never"
