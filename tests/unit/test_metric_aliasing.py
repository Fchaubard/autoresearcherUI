"""Unit tests for automatic metric-key aliasing at ingest.

Bug B (Francois, 2026-06-04): users saw "(not logged)" on real
kept_novel runs because the agent (or anyone porting from wandb) was
logging `loss`/`accuracy`/`learning_rate` instead of the canonical
`train_loss`/`train_acc`/`lr` the dashboard expects.

`metrics.append` now passes each key through `canonical_key`, so common
synonyms get stored under the canonical default-plot names.
"""
from __future__ import annotations

import pytest


def test_canonical_key_maps_loss_to_train_loss(arui_env):
    from backend.app.metrics import canonical_key
    assert canonical_key("loss") == "train_loss"
    assert canonical_key("LOSS") == "train_loss"          # case-insensitive
    assert canonical_key("training_loss") == "train_loss"
    assert canonical_key("train/loss") == "train_loss"


def test_canonical_key_maps_accuracy_to_train_acc(arui_env):
    from backend.app.metrics import canonical_key
    assert canonical_key("accuracy") == "train_acc"
    assert canonical_key("acc") == "train_acc"
    assert canonical_key("training_acc") == "train_acc"


def test_canonical_key_maps_validation_aliases(arui_env):
    from backend.app.metrics import canonical_key
    assert canonical_key("validation_loss") == "val_loss"
    assert canonical_key("eval_loss") == "val_loss"
    assert canonical_key("valid_loss") == "val_loss"
    assert canonical_key("val/loss") == "val_loss"
    assert canonical_key("validation_acc") == "val_acc"
    assert canonical_key("eval_acc") == "val_acc"
    assert canonical_key("val_accuracy") == "val_acc"


def test_canonical_key_maps_throughput_and_lr(arui_env):
    from backend.app.metrics import canonical_key
    assert canonical_key("learning_rate") == "lr"
    assert canonical_key("lr_current") == "lr"
    assert canonical_key("step_time") == "time_per_step"
    assert canonical_key("samples/sec") == "samples_per_sec"
    assert canonical_key("throughput") == "samples_per_sec"
    assert canonical_key("tokens_per_sec") == "samples_per_sec"


def test_canonical_key_passes_through_unknown(arui_env):
    from backend.app.metrics import canonical_key
    # Domain-specific keys are NOT touched.
    assert canonical_key("gsm8k_test_acc") == "gsm8k_test_acc"
    assert canonical_key("my_custom_score") == "my_custom_score"
    # Already-canonical names pass through unchanged.
    assert canonical_key("train_loss") == "train_loss"
    assert canonical_key("val_acc") == "val_acc"
    assert canonical_key("lr") == "lr"


def test_canonical_key_is_idempotent(arui_env):
    from backend.app.metrics import canonical_key
    for k in ("loss", "accuracy", "learning_rate", "validation_loss"):
        c = canonical_key(k)
        assert canonical_key(c) == c, (
            f"aliasing {k!r}→{c!r}→{canonical_key(c)!r} not idempotent")


def test_append_stores_under_canonical_key(arui_env, make_project, make_run):
    """End-to-end: when the agent logs `loss`, the metric store sees
    `train_loss` — which is what the drawer's required-defaults section
    actually looks up."""
    from backend.app import metrics
    make_project()
    rid = "run-alias"
    make_run(id=rid)
    metrics.append(rid, [
        {"key": "loss", "step": 0, "value": 0.5, "wall_time": 1.0},
        {"key": "accuracy", "step": 0, "value": 0.9, "wall_time": 1.0},
        {"key": "learning_rate", "step": 0, "value": 1e-4, "wall_time": 1.0},
    ])
    stored = metrics.keys(rid)
    # Aliased to canonical names — the originals must NOT appear.
    assert "train_loss" in stored
    assert "train_acc" in stored
    assert "lr" in stored
    assert "loss" not in stored
    assert "accuracy" not in stored
    assert "learning_rate" not in stored


def test_aliasing_fixes_missing_default_metric_audit(
        arui_env, make_project, make_run, monkeypatch):
    """The original bug symptom: a run that logs `loss` + `accuracy` should
    NOT emit `missing_default_metric` warnings for `train_loss` /
    `train_acc` — because aliasing maps them to the canonical names."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.app import api
    from backend.app.db import SessionLocal
    from backend.app.models import Event
    monkeypatch.setattr("backend.app.council.is_code_blessed",
                        lambda: True, raising=True)
    monkeypatch.setattr("backend.app.notify.on_run_finished",
                        lambda *_a, **_kw: None, raising=True)
    make_project()
    rid = "run-aliasing-finish"
    make_run(id=rid)

    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app) as client:
        # Send the points through the real /track/log endpoint so the
        # ingest path's aliasing actually runs.
        client.post("/api/track/log", json={
            "run_id": rid,
            "points": [
                {"key": "loss", "step": 0, "value": 0.5, "wall_time": 1.0},
                {"key": "accuracy", "step": 0, "value": 0.9, "wall_time": 1.0},
            ],
        })
        client.post("/api/track/finish",
                    json={"run_id": rid, "summary": {"val_loss": 0.42}})

    db = SessionLocal()
    try:
        evs = (db.query(Event)
               .filter(Event.run_id == rid,
                       Event.type == "missing_default_metric")
               .all())
    finally:
        db.close()
    miss = {e.message.split("'")[1] for e in evs}
    # train_loss + train_acc must be ABSENT from the missing set.
    assert "train_loss" not in miss
    assert "train_acc" not in miss
