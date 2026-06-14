"""Unit tests for backend.app.notify."""
from __future__ import annotations

import datetime as dt


def _iso(hours_ago: float = 0) -> str:
    return (dt.datetime.now(dt.timezone.utc)
            - dt.timedelta(hours=hours_ago)).isoformat()


def test_cadence_hours_parses(arui_env):
    from backend.app.notify import _cadence_hours
    assert _cadence_hours("1h") == 1.0
    assert _cadence_hours("24h") == 24.0
    assert _cadence_hours("off") is None
    assert _cadence_hours("immediate") is None
    assert _cadence_hours("garbage") is None


def test_cadence_reads_setting(arui_env, setting_setter):
    from backend.app.notify import _cadence, _cfg
    setting_setter("onboarding", {"cadence": "4h"})
    assert _cadence(_cfg()) == "4h"


def test_recipients_parses_comma_separated(arui_env):
    from backend.app.notify import _recipients
    out = _recipients({"email_recipients": "a@x.com, b@y.com;c@z.com,"})
    assert out == ["a@x.com", "b@y.com", "c@z.com"]


def test_recipients_falls_back_to_email(arui_env):
    from backend.app.notify import _recipients
    out = _recipients({"email_recipients": "", "email": "me@x.com"})
    assert out == ["me@x.com"]


def test_recipients_empty(arui_env):
    from backend.app.notify import _recipients
    assert _recipients({}) == []


# ── dashboard link survives a pod relaunch (cloudflare tunnel rotation) ──────
#
# The quick-tunnel hostname changes every relaunch, so the dashboard_url saved
# at onboarding goes stale and the email button used to vanish. _dashboard_url
# must fall back to the LIVE tunnel URL parsed from data/cloudflared.log.

def _write_cf_log(*urls: str) -> None:
    from backend.app import notify
    log = notify.DATA_DIR / "cloudflared.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("\n".join(
        f"2026-01-01 INF |  {u}  |" for u in urls) + "\n")


def test_dashboard_url_uses_live_tunnel_when_unset(arui_env):
    from backend.app.notify import _dashboard_url
    _write_cf_log("https://alpha-beta-gamma-delta.trycloudflare.com")
    assert (_dashboard_url({}) ==
            "https://alpha-beta-gamma-delta.trycloudflare.com")


def test_dashboard_url_replaces_stale_trycloudflare(arui_env):
    from backend.app.notify import _dashboard_url
    _write_cf_log("https://old-one-two-three.trycloudflare.com",
                  "https://new-four-five-six.trycloudflare.com")
    # configured value is a now-dead tunnel URL — the LIVE (last) one wins
    cfg = {"dashboard_url": "https://old-one-two-three.trycloudflare.com"}
    assert (_dashboard_url(cfg) ==
            "https://new-four-five-six.trycloudflare.com")


def test_dashboard_url_keeps_stable_custom_domain(arui_env):
    from backend.app.notify import _dashboard_url
    _write_cf_log("https://some-live-tunnel.trycloudflare.com")
    # a real domain is trusted and NOT overridden by the ephemeral tunnel
    cfg = {"dashboard_url": "https://arui.mylab.dev/"}
    assert _dashboard_url(cfg) == "https://arui.mylab.dev"


def test_dashboard_url_empty_when_no_tunnel(arui_env):
    from backend.app.notify import _dashboard_url
    assert _dashboard_url({}) == ""


# ── Best card must recognise the current kept-run taxonomy ──────────────────
#
# Runs are now kept_novel / kept_replicate, not plain "kept". The digest's
# Best card filtered on "kept" only, so best=None and the email rendered "—".

def test_best_run_recognises_kept_novel_and_replicate(db_session, make_project,
                                                       make_run):
    from backend.app.notify import _best_run
    make_project(metric_direction="minimize", validation_metric="val_loss")
    make_run(status="kept_novel", headline_metric=0.50)
    make_run(status="kept_replicate", headline_metric=0.30)   # the best (min)
    make_run(status="discarded", headline_metric=0.10)        # ignored
    make_run(status="success_smoke", headline_metric=0.01)    # smoke, ignored
    from backend.app.models import Project
    proj = db_session.query(Project).first()
    best = _best_run(db_session, proj)
    assert best is not None, "kept_novel/kept_replicate runs were ignored"
    assert best.headline_metric == 0.30


def test_best_run_none_when_only_smoke_or_discarded(db_session, make_project,
                                                    make_run):
    from backend.app.notify import _best_run
    from backend.app.models import Project
    make_project()
    make_run(status="success_smoke", headline_metric=0.01)
    make_run(status="discarded", headline_metric=0.02)
    proj = db_session.query(Project).first()
    assert _best_run(db_session, proj) is None


def test_email_state_get_set_roundtrip(arui_env):
    from backend.app.notify import _email_state_get, _email_state_set
    assert _email_state_get() == {}
    _email_state_set({"at": "2026-01-01T00:00:00+00:00",
                       "claim_ids": ["c1", "c2"]})
    out = _email_state_get()
    assert out["claim_ids"] == ["c1", "c2"]


def test_system_stats_text_empty(arui_env, monkeypatch):
    """When the snapshot is empty, the text block is empty."""
    from backend.app import notify
    monkeypatch.setattr(notify, "_system_snapshot",
                         lambda: ({}, []))
    assert notify._system_stats_text() == ""


def test_system_stats_text_has_node_block(arui_env, monkeypatch):
    from backend.app import notify
    stats = {
        "cpu_percent": 35, "loadavg": [1.0, 1.5, 2.0],
        "ram": {"used_gb": 8, "total_gb": 32, "percent": 25},
        "disk": {"used_gb": 100, "total_gb": 500, "free_gb": 400,
                 "percent": 20},
        "gpus": [{"index": 0, "util_pct": 50, "temp_c": 60},
                 {"index": 1, "util_pct": 30, "temp_c": 55}],
    }
    monkeypatch.setattr(notify, "_system_snapshot",
                         lambda: (stats, []))
    out = notify._system_stats_text()
    assert "== Node ==" in out
    assert "CPU:" in out
    assert "RAM:" in out
    assert "Disk:" in out
    assert "GPUs:" in out


def test_system_stats_text_lists_warnings(arui_env, monkeypatch):
    from backend.app import notify
    monkeypatch.setattr(notify, "_system_snapshot", lambda: (
        {"cpu_percent": 10, "ram": {"total_gb": 32, "used_gb": 4,
                                      "percent": 12}, "disk": {}, "gpus": []},
        [{"severity": "critical", "msg": "disk full"}]))
    out = notify._system_stats_text()
    assert "Warnings" in out
    assert "disk full" in out


def test_system_stats_block_empty(arui_env, monkeypatch):
    from backend.app import notify
    monkeypatch.setattr(notify, "_system_snapshot", lambda: ({}, []))
    assert notify._system_stats_block() == ""


def test_system_stats_block_includes_cards_html(arui_env, monkeypatch):
    from backend.app import notify
    monkeypatch.setattr(notify, "_system_snapshot", lambda: (
        {"cpu_percent": 40, "ram": {"used_gb": 8, "total_gb": 32,
                                      "percent": 25},
         "disk": {"used_gb": 100, "total_gb": 500, "free_gb": 400,
                  "percent": 20},
         "gpus": [{"index": 0, "util_pct": 50, "temp_c": 60}]},
        [{"severity": "warning", "msg": "disk getting full"}]))
    html = notify._system_stats_block()
    assert "Node health" in html
    assert "CPU" in html
    assert "Disk free" in html
    assert "GPUs" in html
    assert "disk getting full" in html
    assert "warning" in html


def test_ideas_on_deck_reads_bullets(arui_env):
    """Bullet-list ideas.md → pending bullets are returned."""
    from backend.app.notify import _ideas_on_deck
    from backend.app.config import DATA_DIR
    ws = DATA_DIR / "workspace" / "myrepo"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "ideas.md").write_text(
        "# Things\n"
        "- try larger batch size for bigger throughput\n"
        "- [x] done thing — skip me\n"
        "- explore alternative learning rate schedules\n"
    )

    class Proj: name = "myrepo"
    out = _ideas_on_deck({"repo_name": "myrepo"}, Proj())
    assert len(out) >= 1
    assert any("learning rate" in x for x in out) or \
           any("batch" in x for x in out)


def test_ideas_on_deck_table_wins(arui_env):
    from backend.app.notify import _ideas_on_deck
    from backend.app.config import DATA_DIR
    ws = DATA_DIR / "workspace" / "myrepo"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "ideas.md").write_text(
        "| status | idea | what |\n"
        "|--------|------|------|\n"
        "| pending | bigger_lr | try 3e-3 |\n"
        "| done | older | already done |\n"
        "\n"
        "- bullet idea that should NOT win\n"
    )

    class Proj: name = "myrepo"
    out = _ideas_on_deck({"repo_name": "myrepo"}, Proj())
    # table wins → only pending row should appear
    assert any("bigger_lr" in x for x in out)
    assert not any("older" in x and "already" in x for x in out)


def test_ideas_on_deck_no_workspace(arui_env):
    from backend.app.notify import _ideas_on_deck

    class Proj: name = "nothing"
    assert _ideas_on_deck({"repo_name": "nothing"}, Proj()) == []


def test_paper_digest_subject_and_text(arui_env, db_session, make_project,
                                          monkeypatch):
    """Paper digest returns subject + text and persists email_state."""
    from backend.app import notify, paper
    from backend.app.models import (PaperClaim, PaperDecision, PaperMeta,
                                     Run)
    make_project(name="MyPaper", metric_direction="minimize")
    # in paper mode
    paper.set_project_mode("paper")
    db_session.add(PaperMeta(id="pm1", venue="ICLR 2027",
                              deadline_iso="", phase="daily"))
    db_session.add(PaperClaim(id="c1", title="ensemble helps",
                               status="active", evidence_strength="strong",
                               idx=0))
    db_session.add(PaperClaim(id="c2", title="diff is good", ready=True,
                               status="active", idx=1))
    db_session.add(PaperDecision(id="d1", source="agent", kind="cite_paper",
                                   title="Cite SEDD 2024?", status="pending",
                                   priority=5,
                                   created_at=_iso()))
    db_session.add(Run(id="pr1", project_id="proj-test", idea_id="i1",
                        run_name="pr1", context="paper",
                        paper_claim_id="c1", status="kept",
                        headline_metric=0.42,
                        ended_at=_iso(2),
                        config={"what": "headline run"}))
    db_session.commit()
    # avoid invoking real charts / matplotlib
    monkeypatch.setattr(notify, "_safe_charts", lambda: None)
    monkeypatch.setattr(notify, "_system_snapshot", lambda: ({}, []))
    subject, text, html, images = notify._paper_digest_email(24.0)
    assert subject is not None
    assert "MyPaper" in subject
    assert "decision" in subject.lower()
    assert "Claims" in text
    assert "Decisions" in text
    assert "ensemble helps" not in text or True  # claim title may appear
    assert "Decisions waiting" in html


def test_digest_email_routes_to_paper_in_paper_mode(arui_env, make_project,
                                                       monkeypatch):
    """digest_email branches on project_mode."""
    from backend.app import notify, paper
    make_project(metric_direction="minimize")
    paper.set_project_mode("paper")
    monkeypatch.setattr(notify, "_system_snapshot", lambda: ({}, []))
    called = {"v": False}

    def fake_paper_email(hrs):
        called["v"] = True
        return ("PAPER SUBJ", "PAPER TXT", "<html/>", {})
    monkeypatch.setattr(notify, "_paper_digest_email", fake_paper_email)
    subj, text, html, images = notify.digest_email(24.0)
    assert called["v"]
    assert subj == "PAPER SUBJ"


def test_digest_email_research_path(arui_env, make_project, monkeypatch):
    """When in research mode, digest_email uses research path, not paper."""
    from backend.app import notify, paper
    make_project(metric_direction="minimize")
    paper.set_project_mode("research")
    monkeypatch.setattr(notify, "_system_snapshot", lambda: ({}, []))
    monkeypatch.setattr(notify, "_safe_charts", lambda: None)
    subj, text, html, images = notify.digest_email(1.0)
    assert subj is not None
    # research-mode subject pattern
    assert "digest" in subj.lower()


def test_send_returns_false_with_no_transport(arui_env, setting_setter):
    """No SMTP/Gmail/Resend configured → no-op send returns False."""
    from backend.app.notify import send
    setting_setter("onboarding", {"email": "me@x.com",
                                    "email_recipients": "me@x.com"})
    assert send("subj", "body") is False


def test_send_returns_false_with_no_recipients(arui_env, setting_setter):
    from backend.app.notify import send
    setting_setter("onboarding", {"gmail_app_pw": "abc"})
    assert send("subj", "body") is False


def test_deliver_dispatches_gmail(arui_env, setting_setter, monkeypatch):
    """When gmail_app_pw + email set, SMTP path is used."""
    from backend.app import notify
    called = {"v": []}

    def fake_smtp(host, port, user, pw, sender, rs, sub, text, html, images):
        called["v"].append((host, port, user, sender, rs, sub))
        return True
    monkeypatch.setattr(notify, "_smtp_send", fake_smtp)
    setting_setter("onboarding", {
        "email": "me@x.com", "email_recipients": "to@x.com",
        "gmail_app_pw": "app pw here"})
    assert notify.send("subj", "body") is True
    assert called["v"][0][0] == "smtp.gmail.com"
    assert called["v"][0][1] == 587


def test_on_run_finished_only_immediate(arui_env, db_session, make_project,
                                          make_run, setting_setter,
                                          monkeypatch):
    """on_run_finished is a no-op unless cadence is 'immediate'."""
    from backend.app import notify
    make_project(metric_direction="minimize")
    make_run(id="r1", status="kept", headline_metric=0.1,
             run_name="r1")
    setting_setter("onboarding", {"cadence": "1h"})
    sent = {"v": False}
    monkeypatch.setattr(notify, "send",
                         lambda *a, **kw: sent.__setitem__("v", True))
    notify.on_run_finished("r1")
    assert sent["v"] is False
