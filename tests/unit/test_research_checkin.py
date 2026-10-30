"""The hourly reminder must honor halt and avoid duplicate/ambiguous delivery."""
import datetime as dt
import importlib.util
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "research_checkin", Path(__file__).parents[2] / "bin/arui_research_checkin.py")
checkin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checkin)
real_collect = checkin.collect


@pytest.fixture
def setup_checkin(tmp_path, monkeypatch):
    db = tmp_path / "db.sqlite"
    cfg = {"passcode": "secret-do-not-print", "research_oversight_enabled": True}
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE setting (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO setting VALUES ('onboarding',?)", (json.dumps(cfg),))
    work = tmp_path / "work"
    work.mkdir()
    sent = []
    monkeypatch.setattr(checkin, "collect", lambda *a: {"reports": []})
    monkeypatch.setattr(checkin, "deliver", lambda *a: sent.append(a[2]) or {"ok": True, "delivery": "queued"})
    now = dt.datetime(2026, 10, 30, 22, 0, tzinfo=dt.timezone.utc)
    return db, work, tmp_path / "out", now, sent


@pytest.mark.parametrize("field", ["research_paused", "research_halted"])
def test_operator_stop_prevents_message(setup_checkin, field):
    db, work, out, now, sent = setup_checkin
    with sqlite3.connect(db) as conn:
        cfg = checkin.read_config(db)
        cfg[field] = True
        conn.execute("UPDATE setting SET value=?", (json.dumps(cfg),))
    assert checkin.run(db, work, out, "http://localhost", now=now)["status"] == "skipped"
    assert not sent


def test_halt_directive_prevents_message(setup_checkin):
    db, work, out, now, sent = setup_checkin
    (work / "directives.jsonl").write_text(json.dumps({"type": "HALT", "status": "open"}))
    assert checkin.run(db, work, out, "http://localhost", now=now)["reason"] == "halt_directive"
    assert not sent


def test_one_message_per_hour_and_secret_free_receipt(setup_checkin):
    db, work, out, now, sent = setup_checkin
    assert checkin.run(db, work, out, "http://localhost", now=now)["status"] == "delivered"
    assert checkin.run(db, work, out, "http://localhost", now=now)["reason"] == "recent_attempt"
    assert checkin.run(db, work, out, "http://localhost", now=now + dt.timedelta(hours=1))["status"] == "delivered"
    assert len(sent) == 2
    assert "secret-do-not-print" not in (out / "delivery_state.json").read_text()


def test_ambiguous_delivery_does_not_retry_or_expose_secret(setup_checkin, monkeypatch):
    db, work, out, now, sent = setup_checkin
    def timeout(*args):
        raise TimeoutError("secret-do-not-print")
    monkeypatch.setattr(checkin, "deliver", timeout)
    result = checkin.run(db, work, out, "http://localhost", now=now)
    assert result["status"] == "delivery_uncertain"
    assert "secret-do-not-print" not in json.dumps(result)
    assert checkin.run(db, work, out, "http://localhost", now=now)["reason"] == "recent_attempt"


def test_dry_run_does_not_send_or_reserve_hour(setup_checkin):
    db, work, out, now, sent = setup_checkin
    assert checkin.run(db, work, out, "http://localhost", now=now, dry_run=True)["status"] == "dry_run"
    assert not sent and not (out / "delivery_state.json").exists()


def test_pause_during_collection_prevents_delivery(setup_checkin, monkeypatch):
    db, work, out, now, sent = setup_checkin
    def collect(*args):
        with sqlite3.connect(db) as conn:
            cfg = checkin.read_config(db)
            cfg["research_paused"] = True
            conn.execute("UPDATE setting SET value=?", (json.dumps(cfg),))
        return {}
    monkeypatch.setattr(checkin, "collect", collect)
    assert checkin.run(db, work, out, "http://localhost", now=now)["status"] == "skipped"
    assert not sent


def test_overlap_and_window_clipping_do_not_double_count_wall_time():
    start = dt.datetime(2026, 10, 30, 22, tzinfo=dt.timezone.utc)
    end = start + dt.timedelta(hours=1)
    def row(a, b):
        return {"status": "kept_novel", "started_at": (start + dt.timedelta(minutes=a)).isoformat(),
                "ended_at": (start + dt.timedelta(minutes=b)).isoformat()}
    result = checkin.interval_summary([row(-10, 20), row(10, 30), row(50, 70)], start, end)
    assert result["minutes_with_any_registered_job"] == 40
    assert result["minutes_without_registered_job"] == 20
    assert result["registered_worker_hours"] == pytest.approx(50 / 60)


def test_campaign_charges_multiple_gpus_and_ignores_prior_campaign(setup_checkin, monkeypatch):
    db, work, out, now, sent = setup_checkin
    campaign_start = now - dt.timedelta(hours=2)
    with sqlite3.connect(db) as conn:
        cfg = checkin.read_config(db)
        cfg.update(research_campaign_started_at=campaign_start.isoformat(), research_campaign_gpu_hour_cap=1)
        conn.execute("UPDATE setting SET value=?", (json.dumps(cfg),))
        conn.execute("CREATE TABLE run (id TEXT,status TEXT,started_at TEXT,ended_at TEXT,headline_metric REAL,gpus_required INT)")
        conn.executemany("INSERT INTO run VALUES (?,?,?,?,?,?)", [
            ("old", "kept_novel", (now-dt.timedelta(hours=3)).isoformat(), campaign_start.isoformat(), None, 10),
            ("new", "kept_novel", (now-dt.timedelta(minutes=50)).isoformat(), (now-dt.timedelta(minutes=20)).isoformat(), None, 2),
        ])
    monkeypatch.setattr(checkin.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout="0, 0, 0\n"))
    report = work / "research" / "new_campaign" / "final_report.md"
    report.parent.mkdir(parents=True)
    report.write_text("Current campaign evidence")
    result = real_collect(db, work, now)
    assert result["campaign"]["charged_gpu_hours"] == 1
    assert result["campaign"]["budget_exhausted"] is True
    assert result["latest_reports"][0]["path"] == str(report)
    assert "secret-do-not-print" not in json.dumps(result)
