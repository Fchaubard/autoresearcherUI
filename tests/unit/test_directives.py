"""Unit tests for backend.app.directives — schema + file CRUD + gating
(RESEARCH_IMPROVEMENT_PLAN #1).
"""
from __future__ import annotations

import json
import pytest


@pytest.fixture
def directives_env(arui_env, tmp_path):
    """A clean directives.jsonl path override + the module + a session."""
    from backend.app import directives as _d
    p = tmp_path / "directives.jsonl"
    _d.set_path_override(str(p))
    return _d, p


def test_validate_directive_requires_known_type(directives_env):
    d, _ = directives_env
    ok, err = d.validate_directive({"type": "NONSENSE", "what": "x",
                                      "idea_class": "INCREMENTAL"})
    assert not ok and "type" in err


def test_validate_directive_requires_what(directives_env):
    d, _ = directives_env
    ok, err = d.validate_directive({"type": "SCIENCE", "what": "",
                                      "idea_class": "INCREMENTAL"})
    assert not ok and "what" in err


def test_validate_directive_requires_known_idea_class(directives_env):
    d, _ = directives_env
    ok, err = d.validate_directive({"type": "SCIENCE", "what": "x",
                                      "idea_class": "BOGUS"})
    assert not ok and "idea_class" in err


def test_validate_directive_accepts_minimal_valid(directives_env):
    d, _ = directives_env
    ok, err = d.validate_directive(
        {"type": "BLOCKER_INFRA", "what": "build hash registry"})
    assert ok and err == ""


def test_upsert_creates_then_updates(directives_env):
    d, p = directives_env
    stored, created = d.upsert({"type": "SCIENCE",
                                  "what": "diff-init from AR ckpt",
                                  "idea_class": "INCREMENTAL"})
    assert created is True
    assert stored["status"] == "open"
    assert stored["idea_class"] == "INCREMENTAL"
    # round-trip
    again, created2 = d.upsert({"id": stored["id"], "type": "SCIENCE",
                                  "what": "diff-init from AR ckpt v2",
                                  "idea_class": "ORTHOGONAL"})
    assert created2 is False
    assert again["what"].endswith("v2")
    assert again["idea_class"] == "ORTHOGONAL"
    # created_at preserved
    assert again["created_at"] == stored["created_at"]


def test_upsert_validation_raises(directives_env):
    d, _ = directives_env
    with pytest.raises(ValueError):
        d.upsert({"type": "WTF", "what": "x"})


def test_close_marks_done_with_evidence(directives_env):
    d, _ = directives_env
    stored, _ = d.upsert({"type": "BLOCKER_INFRA",
                            "what": "build hash registry",
                            "idea_class": "INFRA"})
    closed = d.close(stored["id"], evidence="CPU smoke test rejects dup")
    assert closed["status"] == "done"
    assert "CPU smoke test" in closed["evidence"]
    assert "closed_at" in closed


def test_close_unknown_id_returns_none(directives_env):
    d, _ = directives_env
    assert d.close("d-nope") is None


def test_open_blocker_kind_returns_first_type(directives_env):
    d, _ = directives_env
    d.upsert({"type": "SCIENCE", "what": "sci 1",
              "idea_class": "INCREMENTAL"})
    d.upsert({"type": "BLOCKER_EVAL", "what": "eval block",
              "idea_class": "INFRA"})
    assert d.open_blocker_kind() == "BLOCKER_EVAL"


def test_open_blocker_kind_none_when_closed(directives_env):
    d, _ = directives_env
    stored, _ = d.upsert({"type": "BLOCKER_INFRA", "what": "blk",
                            "idea_class": "INFRA"})
    d.close(stored["id"])
    assert d.open_blocker_kind() is None


def test_open_halt_blocks_all(directives_env):
    d, _ = directives_env
    d.upsert({"type": "HALT", "what": "stop the line",
              "idea_class": "INFRA", "priority": 9999})
    halt = d.open_halt()
    assert halt is not None and halt["type"] == "HALT"


def test_top_open_picks_highest_priority(directives_env):
    d, _ = directives_env
    d.upsert({"type": "SCIENCE", "what": "low", "priority": 100,
              "idea_class": "INCREMENTAL"})
    d.upsert({"type": "BLOCKER_INFRA", "what": "infra",
              "priority": 1000, "idea_class": "INFRA"})
    top = d.top_open()
    assert top["type"] == "BLOCKER_INFRA"


def test_counts_by_idea_class(directives_env):
    d, _ = directives_env
    d.upsert({"type": "SCIENCE", "what": "a",
              "idea_class": "INCREMENTAL"})
    d.upsert({"type": "SCIENCE", "what": "b",
              "idea_class": "INCREMENTAL"})
    d.upsert({"type": "SCIENCE", "what": "c",
              "idea_class": "ORTHOGONAL"})
    counts = d.counts_by_idea_class()
    assert counts.get("INCREMENTAL") == 2
    assert counts.get("ORTHOGONAL") == 1


def test_read_all_skips_malformed_lines(directives_env):
    d, p = directives_env
    d.upsert({"type": "SCIENCE", "what": "x",
              "idea_class": "INCREMENTAL"})
    # Append garbage
    with open(p, "a") as f:
        f.write("not json at all\n")
        f.write('{"type":"oops"}\n')   # missing what + bad type
    rows = d.read_all()
    # The good row + the malformed-but-still-JSON row (we accept any
    # dict) survive; the non-JSON line is dropped.
    assert any(r.get("what") == "x" for r in rows)


# ───── /api/track/run gate tests ─────────────────────────────────────


@pytest.fixture
def client(arui_env, fake_subprocess):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.app.api import router
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def auto_bless(setting_setter):
    """Bypass the code-bless gate so we can isolate the directive gate."""
    setting_setter("code_bless", {"status": "approved"})


@pytest.fixture
def directives_path(arui_env, tmp_path, setting_setter):
    from backend.app import directives as _d
    p = tmp_path / "directives.jsonl"
    _d.set_path_override(str(p))
    return _d, p


def test_track_run_blocked_when_blocker_directive_open(
        client, auto_bless, directives_path):
    d, _ = directives_path
    d.upsert({"type": "BLOCKER_INFRA",
              "what": "build hash registry + duplicate killer",
              "idea_class": "INFRA"})
    r = client.post("/api/track/run",
                     json={"name": "real_run_x", "config": {"lr": 1e-3}})
    assert r.status_code == 423
    body = r.json()
    assert body["reason"] == "open_blocker_directive"
    assert body["blocker_kind"] == "BLOCKER_INFRA"


def test_track_run_probe_bypasses_blocker(
        client, auto_bless, directives_path):
    d, _ = directives_path
    d.upsert({"type": "BLOCKER_INFRA", "what": "block science",
              "idea_class": "INFRA"})
    r = client.post("/api/track/run",
                     json={"name": "_probe_x", "config": {}})
    # probe must succeed even with an open blocker
    assert r.status_code == 200, r.text


def test_track_run_smoke_bypasses_blocker(
        client, auto_bless, directives_path):
    d, _ = directives_path
    d.upsert({"type": "BLOCKER_EVAL", "what": "land trusted_eval",
              "idea_class": "INFRA"})
    r = client.post("/api/track/run",
                     json={"name": "_smoke_eval", "config": {}})
    assert r.status_code == 200


def test_track_run_science_allowed_when_no_blocker(
        client, auto_bless, directives_path):
    d, _ = directives_path
    d.upsert({"type": "SCIENCE", "what": "try diff-init",
              "idea_class": "INCREMENTAL"})
    r = client.post("/api/track/run",
                     json={"name": "real_science_run",
                           "config": {"lr": 3e-4, "model": "diff_n3"}})
    assert r.status_code == 200


def test_track_run_blocked_by_halt_directive_for_real_runs(
        client, auto_bless, directives_path):
    d, _ = directives_path
    d.upsert({"type": "HALT", "what": "stop",
              "idea_class": "INFRA", "priority": 9999})
    r = client.post("/api/track/run",
                     json={"name": "real_run_with_halt", "config": {}})
    assert r.status_code == 423
    assert r.json()["reason"] == "halt_directive"


def test_track_run_halt_directive_also_blocks_probes(
        client, auto_bless, directives_path):
    """A HALT directive is the hard stop — probes don't bypass it."""
    d, _ = directives_path
    d.upsert({"type": "HALT", "what": "stop everything",
              "idea_class": "INFRA", "priority": 9999})
    r = client.post("/api/track/run",
                     json={"name": "_probe_smoke", "config": {}})
    assert r.status_code == 423


def test_directives_endpoints_round_trip(
        client, directives_path):
    d, _ = directives_path
    # 1) upsert
    r = client.post("/api/directives/upsert",
                     json={"directive": {"type": "SCIENCE",
                                          "what": "diff-init from AR ckpt",
                                          "idea_class": "INCREMENTAL"}})
    assert r.status_code == 200
    body = r.json()
    did = body["directive"]["id"]
    # 2) list
    rl = client.get("/api/directives").json()
    assert any(x["id"] == did for x in rl["directives"])
    # 3) get
    rg = client.get(f"/api/directives/{did}").json()
    assert rg["id"] == did
    # 4) done
    rd = client.post(f"/api/directives/{did}/done",
                      json={"evidence": "smoke test passed"}).json()
    assert rd["status"] == "done"
    assert "smoke test" in rd["evidence"]


def test_directives_upsert_400_on_invalid(client, directives_path):
    r = client.post("/api/directives/upsert",
                     json={"directive": {"type": "BOGUS",
                                          "what": "x"}})
    assert r.status_code == 400
    assert "type" in r.json()["error"]


def test_directives_get_404_when_missing(client, directives_path):
    r = client.get("/api/directives/d-no-such-id")
    assert r.status_code == 404


def test_directives_done_404_when_missing(client, directives_path):
    r = client.post("/api/directives/d-no-such-id/done",
                     json={"evidence": "x"})
    assert r.status_code == 404
