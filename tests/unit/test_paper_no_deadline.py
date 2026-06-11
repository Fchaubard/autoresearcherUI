"""Paper mode is quality-gated, not time-gated: there is NO conference
deadline concept. days_till_deadline() must always be None, even if a stale
deadline_iso is set on PaperMeta, so no countdown / progress bar ever renders.
"""


def test_days_till_deadline_is_always_none(arui_env, db_session):
    from backend.app import paper
    from backend.app.models import PaperMeta
    # even with a deadline_iso explicitly set, it must report None
    db_session.add(PaperMeta(id="pm-1", venue="NeurIPS 2026",
                             deadline_iso="2026-12-01T00:00:00+00:00"))
    db_session.commit()
    assert paper.days_till_deadline() is None
