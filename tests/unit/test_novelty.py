"""Unit tests for the novelty hash + duplicate-killer registry
(RESEARCH_IMPROVEMENT_PLAN.md #3).

Surface area:
  - backend.app.novelty.canonicalise (recursive normalisation)
  - backend.app.novelty.novelty_hash (deterministic 16-char digest)
  - backend.app.novelty.is_seed_replicate (escape hatch detection)
  - backend.app.novelty.is_probe_or_smoke (smoke whitelist)
  - backend.app.novelty.register (atomic check-and-insert)
  - backend.app.novelty.populate_registry_from_db (startup migration)
  - /api/track/run integration: HTTP 409 on duplicate, bypass for
    explicit replicates and probe/smoke runs.
"""
from __future__ import annotations

import pytest


# ─────────────────────────── unit-level: pure helpers ─────────────────


def test_novelty_hash_deterministic(arui_env):
    """Same logical config -> same hash, independent of key order."""
    from backend.app import novelty
    a = {"lr": 0.001, "model": "transformer", "ensemble_n": 5}
    b = {"ensemble_n": 5, "model": "transformer", "lr": 0.001}
    assert novelty.novelty_hash(a) == novelty.novelty_hash(b)
    # And rerunning the same call gives the same digest.
    assert novelty.novelty_hash(a) == novelty.novelty_hash(a)


def test_novelty_hash_drops_log_only_keys(arui_env):
    """run_name, timestamps, members list etc. must NOT contribute to
    the hash — otherwise the duplicate killer never fires (each launch
    stamps a fresh run_name)."""
    from backend.app import novelty
    base = {"lr": 0.001, "model": "tf", "dataset": "gsm8k"}
    polluted = {**base,
                "run_name": "ensemble-5-v3",
                "what": "try 5-way ensemble",
                "why": "council suggested it",
                "members": ["a", "b", "c"],
                "created_at": "2026-06-04T10:00:00Z",
                "timestamp": 12345,
                "log_dir": "/tmp/x",
                "run_id": "abc"}
    assert novelty.novelty_hash(base) == novelty.novelty_hash(polluted)


def test_novelty_hash_distinguishes_modelling_keys(arui_env):
    """Different lr / model / dataset MUST yield different hashes —
    otherwise the duplicate killer is over-aggressive."""
    from backend.app import novelty
    a = {"lr": 0.001, "model": "tf"}
    b = {"lr": 0.0001, "model": "tf"}     # different lr
    c = {"lr": 0.001, "model": "mamba"}   # different model
    assert novelty.novelty_hash(a) != novelty.novelty_hash(b)
    assert novelty.novelty_hash(a) != novelty.novelty_hash(c)


def test_canonicalise_on_dict_list_nested(arui_env):
    """canonicalise() must recurse correctly into nested dicts and
    lists, sort dict keys, and drop LOG_ONLY_KEYS at every level."""
    from backend.app import novelty
    cfg = {
        "outer": {
            "lr": 1e-3,
            "run_name": "should-be-dropped-deeply-too",
            "sweep": [
                {"seed": 0, "tag": "first"},   # 'tag' is log-only
                {"seed": 1, "tag": "second"},
            ],
        },
        "timestamp": "drop me",
    }
    canon = novelty.canonicalise(cfg)
    # 'timestamp' is gone at the top level.
    assert "timestamp" not in canon
    # 'outer' survives; 'run_name' beneath it is dropped.
    assert "outer" in canon
    assert "run_name" not in canon["outer"]
    # The list of dicts has every 'tag' field stripped.
    sweep = canon["outer"]["sweep"]
    assert sweep == [{"seed": 0}, {"seed": 1}]
    # Dict keys are sorted within each level (lr, sweep).
    assert list(canon["outer"].keys()) == ["lr", "sweep"]


def test_canonicalise_normalises_sets(arui_env):
    """Sets are unordered — different insertion orders must canonicalise
    to the same list so the hash matches."""
    from backend.app import novelty
    a = {"members": {"x", "y", "z"}}
    b = {"members": {"z", "y", "x"}}
    assert novelty.canonicalise(a) == novelty.canonicalise(b)
    assert novelty.novelty_hash(a) == novelty.novelty_hash(b)


def test_novelty_hash_none_and_empty_equal(arui_env):
    """An agent that sends no config at all should be a duplicate of
    another agent that sends an empty config. Both = 'configured
    nothing'."""
    from backend.app import novelty
    assert novelty.novelty_hash(None) == novelty.novelty_hash({})


def test_is_seed_replicate_detection(arui_env):
    """All three opt-in signals must be honoured by is_seed_replicate."""
    from backend.app import novelty
    # 1. Idea class
    assert novelty.is_seed_replicate({"idea_class": "REPRODUCE"})
    assert novelty.is_seed_replicate({"idea_class": "seed_replicate"})
    # 2. Inline flag
    assert novelty.is_seed_replicate({"seed_replicate": True})
    # 3. run_id prefix
    assert novelty.is_seed_replicate({}, run_id="seed_001")
    # And the negative: a plain config is NOT a replicate.
    assert not novelty.is_seed_replicate({"lr": 1e-3}, run_id="my-run")


def test_is_probe_or_smoke_detection(arui_env):
    """The _probe / _smoke whitelist is also used by track_finish for
    the success_smoke status branch — keep them in sync."""
    from backend.app import novelty
    assert novelty.is_probe_or_smoke("_probe_001")
    assert novelty.is_probe_or_smoke("_smoke_xyz")
    assert not novelty.is_probe_or_smoke("real-run")
    assert not novelty.is_probe_or_smoke("")


def test_register_blocks_duplicates(arui_env):
    """A second call with the same canonical config gets accepted=False
    and points at the original run_id."""
    from backend.app import novelty
    cfg = {"lr": 0.001, "model": "tf"}
    ok, existing, h = novelty.register(cfg, "run-A")
    assert ok and existing is None and len(h) == 16
    ok2, existing2, h2 = novelty.register(cfg, "run-B")
    assert ok2 is False
    assert existing2 == "run-A"
    assert h2 == h


def test_register_allows_seed_replicate(arui_env):
    """Explicit seed-replicate bypasses dedup; the original keeps
    ownership of the registry slot (so future ordinary launches still
    point at the right canonical run_id)."""
    from backend.app import novelty
    cfg = {"lr": 0.001, "model": "tf"}
    novelty.register(cfg, "run-A")
    # An explicit replicate of the same config is accepted.
    cfg_rep = {**cfg, "seed_replicate": True}
    ok, existing, _ = novelty.register(cfg_rep, "run-B")
    assert ok is True
    # And the registry still credits run-A as the canonical claimant
    # (a third plain launch points back at A, not B).
    ok3, existing3, _ = novelty.register(cfg, "run-C")
    assert ok3 is False
    assert existing3 == "run-A"


def test_register_allows_probe_smoke(arui_env):
    """Smoke probes always pass through, even if their config matches a
    previously-registered hash. They're not real experiments."""
    from backend.app import novelty
    cfg = {"lr": 0.001}
    novelty.register(cfg, "real-1")
    ok, _, _ = novelty.register(cfg, "_probe_lr_001")
    assert ok is True
    ok2, _, _ = novelty.register(cfg, "_smoke_lr_002")
    assert ok2 is True


# ─────────────────────────── HTTP-level: /api/track/run ───────────────


@pytest.fixture
def client(arui_env, fake_subprocess):
    """A FastAPI TestClient bound to the api router (mirrors
    test_research_pause.py to keep tests fast and dependency-free)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.app.api import router
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as c:
        yield c


def test_track_run_rejects_duplicate(client):
    """POST /api/track/run with a previously-seen config returns HTTP
    409 + {"error":"duplicate","existing_run_id":...}."""
    r = client.post("/api/track/run",
                    json={"name": "first", "config": {"lr": 0.001}})
    assert r.status_code == 200
    assert r.json()["run_id"] == "first"
    novel_hash = r.json()["novelty_hash"]

    r2 = client.post("/api/track/run",
                     json={"name": "second", "config": {"lr": 0.001}})
    assert r2.status_code == 409
    body = r2.json()
    assert body["error"] == "duplicate"
    assert body["existing_run_id"] == "first"
    assert body["novelty_hash"] == novel_hash


def test_track_run_drops_log_only_keys_when_dedup(client):
    """Two configs that differ only in run_name / what / why etc. are
    still rejected as duplicates."""
    client.post("/api/track/run",
                json={"name": "first",
                      "config": {"lr": 0.001, "what": "try lr=0.001"}})
    r = client.post("/api/track/run",
                    json={"name": "second",
                          "config": {"lr": 0.001,
                                      "what": "still try lr=0.001"}})
    assert r.status_code == 409
    assert r.json()["existing_run_id"] == "first"


def test_track_run_allows_explicit_seed_replicate(client):
    """idea_class=REPRODUCE / seed_replicate=true / run_id startswith
    seed_ all let the same config through."""
    client.post("/api/track/run",
                json={"name": "first", "config": {"lr": 0.001}})
    # Method 1: idea_class
    r = client.post("/api/track/run",
                    json={"name": "rep-1",
                          "config": {"lr": 0.001,
                                      "idea_class": "REPRODUCE"}})
    assert r.status_code == 200
    # Method 2: inline flag
    r = client.post("/api/track/run",
                    json={"name": "rep-2",
                          "config": {"lr": 0.001,
                                      "seed_replicate": True}})
    assert r.status_code == 200
    # Method 3: run_id prefix
    r = client.post("/api/track/run",
                    json={"name": "seed_3", "config": {"lr": 0.001}})
    assert r.status_code == 200


def test_track_run_probe_bypasses_dedup(client):
    """_probe and _smoke runs never trip the duplicate killer — they
    pre-date bless and can repeat the same config legitimately."""
    cfg = {"lr": 0.001}
    r = client.post("/api/track/run",
                    json={"name": "_probe_a", "config": cfg})
    assert r.status_code == 200
    r = client.post("/api/track/run",
                    json={"name": "_probe_b", "config": cfg})
    assert r.status_code == 200
    r = client.post("/api/track/run",
                    json={"name": "_smoke_c", "config": cfg})
    assert r.status_code == 200


def test_track_run_distinct_configs_both_accepted(client):
    """The duplicate killer must not be over-aggressive — distinct
    configs (different lr) BOTH register successfully."""
    r = client.post("/api/track/run",
                    json={"name": "a", "config": {"lr": 0.001}})
    assert r.status_code == 200
    r = client.post("/api/track/run",
                    json={"name": "b", "config": {"lr": 0.01}})
    assert r.status_code == 200


# ─────────────────────────── startup migration ────────────────────────


def test_populate_registry_from_db_seeds_existing_runs(
        arui_env, make_project, make_run):
    """populate_registry_from_db scans every kept-ish run and rebuilds
    the hash registry. Without this the duplicate killer would forget
    every prior config on a backend restart."""
    from backend.app import novelty
    make_project()
    # Two kept runs with distinct configs + one crashed run that should
    # NOT seed the registry (crashed configs are explicitly rejected,
    # repopulating them would block re-attempts).
    make_run(id="run-novel-A", status="kept_novel",
             config={"lr": 0.001, "model": "tf"})
    make_run(id="run-novel-B", status="kept",
             config={"lr": 0.002, "model": "tf"})
    make_run(id="run-crashed", status="crashed",
             config={"lr": 0.5, "model": "tf"})

    # Fresh in-process registry — caller is responsible for clearing
    # before seeding so two backends starting in parallel don't clash.
    novelty._clear_for_tests()
    loaded = novelty.populate_registry_from_db()
    assert loaded == 2

    # The two kept hashes are claimed; the crashed one is not.
    assert novelty.novelty_hash({"lr": 0.001, "model": "tf"}) \
        in novelty.run_registry
    assert novelty.novelty_hash({"lr": 0.002, "model": "tf"}) \
        in novelty.run_registry
    assert novelty.novelty_hash({"lr": 0.5, "model": "tf"}) \
        not in novelty.run_registry


def test_populate_registry_then_track_run_blocks_duplicate(
        arui_env, make_project, make_run):
    """End-to-end: a kept run from a prior process is enough to block
    a fresh /api/track/run launching the same config."""
    from backend.app import novelty
    make_project()
    make_run(id="prior-run", status="kept_novel",
             config={"lr": 0.001, "model": "tf"})
    novelty._clear_for_tests()
    novelty.populate_registry_from_db()

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.app.api import router
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as c:
        r = c.post("/api/track/run",
                   json={"name": "new-run",
                         "config": {"lr": 0.001, "model": "tf"}})
    assert r.status_code == 409
    assert r.json()["existing_run_id"] == "prior-run"
