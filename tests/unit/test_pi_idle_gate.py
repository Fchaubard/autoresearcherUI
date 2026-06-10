"""The idle-GPU 'research may be stuck' alert must be PHASE-AWARE.

Idle GPUs during setup (scoping / scaffolding / waiting on the council bless)
are EXPECTED, not a stall — emailing 'stuck' then is misleading. The alert
should only fire once the code is blessed and the loop is actually meant to be
running.
"""
import datetime as dt


def _ago(mins):
    return (dt.datetime.now(dt.timezone.utc)
            - dt.timedelta(minutes=mins)).isoformat()


def test_expected_idle_during_setup_unblessed(arui_env):
    from backend.app import pi
    expected, why = pi._gpus_expected_idle()
    assert expected is True
    assert "setup" in why.lower()


def test_not_expected_idle_once_running(arui_env, setting_setter,
                                        make_project, make_run):
    setting_setter("code_bless", {"status": "approved"})
    make_project()
    make_run(id="exp1", run_name="exp1", status="kept", headline_metric=0.1)
    from backend.app import pi
    expected, _why = pi._gpus_expected_idle()
    assert expected is False


def test_no_idle_email_during_setup(arui_env, setting_setter, monkeypatch):
    from backend.app import notify, pi
    sent = []
    monkeypatch.setattr(notify, "send_alert", lambda **k: sent.append(k))
    # idle for 2h, but code NOT blessed (setup) -> must NOT email
    setting_setter("pi_idle_gpu_since", {"since": _ago(120)})
    pi._idle_gpu_escalation({"gpus_total": 3, "gpus_idle": 3})
    assert sent == []


def test_idle_email_fires_when_running_and_stalled(arui_env, setting_setter,
                                                   make_project, make_run,
                                                   monkeypatch):
    from backend.app import notify, pi
    sent = []
    monkeypatch.setattr(notify, "send_alert", lambda **k: sent.append(k))
    setting_setter("code_bless", {"status": "approved"})
    make_project()
    make_run(id="exp1", run_name="exp1", status="kept", headline_metric=0.1)
    setting_setter("pi_idle_gpu_since", {"since": _ago(120)})
    pi._idle_gpu_escalation({"gpus_total": 3, "gpus_idle": 3})
    assert len(sent) == 1
    assert "stalled" in sent[0]["subject"]
    # no more "may be ... may be ..." guessing in the bullets
    joined = " ".join(sent[0].get("bullets") or [])
    assert "may be" not in joined.lower() or "real stall" in joined.lower()
