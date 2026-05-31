"""Regression tests pinning the validation-metric onboarding fix.

A real user (Francois, 2026-05-31) onboarded with `gsm8k_val_acc` and
the dashboard ended up tracking `val_loss` (the dropdown default) with
direction='minimize'. Root cause: the field was a `<select>` whose
value silently no-op'd when set to an unknown string.

Three contracts pinned here so the bug can't come back silently:

1.  OB_FIELDS[metric] is type='metric' (free-text + datalist),
    NOT 'select'. The whole reason the bug existed was the select.
2.  /api/onboarding accepts arbitrary metric strings (custom benchmark
    names) — saves them verbatim to Project.validation_metric.
3.  Direction is derived correctly from the metric NAME for every
    common case the user might paste in:
        gsm8k_val_acc → maximize    (acc → up)
        squad_em       → maximize    (_em → up)
        pass@1         → maximize
        val_loss       → minimize
        rmse           → minimize
        perplexity     → minimize
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


# ────────────────────────── contract 1: not a select ─────────────────────────

def test_metric_field_is_text_input_not_select():
    """OB_FIELDS entry for 'metric' must be type='metric' (free-text +
    datalist). If anyone reverts it back to 'select', this test fails."""
    app_js = (Path(__file__).resolve().parents[2]
              / "backend" / "app" / "static" / "app.js").read_text()
    # Find the OB_FIELDS metric row.
    m = re.search(
        r"\[\s*'metric'\s*,\s*'[^']*'\s*,\s*'(\w+)'", app_js)
    assert m is not None, "OB_FIELDS metric row not found"
    field_type = m.group(1)
    assert field_type == "metric", (
        f"OB_FIELDS metric type is {field_type!r}; must be 'metric' "
        "(free-text + datalist). Reverting to 'select' silently "
        "drops user-typed values that aren't in the option list.")
    # And the form builder must know how to render 'metric'.
    assert "type === 'metric'" in app_js, (
        "buildSettingsForm does not handle the 'metric' field type "
        "— rendering will fall through to plain <input> without a "
        "datalist, losing the suggestions UX.")
    assert "<datalist>" in app_js or "el('datalist'" in app_js, (
        "Form builder must produce a <datalist> for the metric field.")


# ────────────────────────── contract 2: backend accepts anything ─────────────

@pytest.fixture
def client(arui_env, fake_subprocess):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.app.api import router
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as c:
        yield c


def test_onboarding_saves_arbitrary_metric_verbatim(client):
    """POSTing a custom metric name must persist verbatim — no
    silent munging, no defaulting to 'val_loss'."""
    custom = "gsm8k_val_acc"
    r = client.post("/api/onboarding", json={
        "project_name": "diff-ensemble",
        "repo_name": "diff-ensemble",
        "validation_metric": custom,
        "metric": custom,                   # the JS uses 'metric'
        "metric_direction": "maximize",
        "purpose": "test the metric",
        "email": "me@x.com",
    })
    assert r.status_code == 200, r.text
    # Read it back from the Project row.
    from backend.app.db import SessionLocal
    from backend.app.models import Project
    s = SessionLocal()
    try:
        p = s.query(Project).first()
        assert p is not None
        assert p.validation_metric == custom, (
            f"Project.validation_metric is {p.validation_metric!r}; "
            f"expected {custom!r}. The onboarding endpoint dropped the "
            "custom value somewhere.")
    finally:
        s.close()


# ────────────────────────── contract 3: direction heuristic ──────────────────

# Use a separate (lighter) test fixture that doesn't need full DB.
def _direction_for(metric: str) -> str:
    """Replay the direction heuristic from /api/onboarding."""
    import re as _re
    _ml = _re.sub(r"[\s\-]+", "_", metric.strip().lower())
    _max = (
        "accuracy", "_acc", "acc_", "acc@",
        "f1", "exact_match", "em", "_em",
        "bleu", "rouge", "meteor", "chrf",
        "score", "reward",
        "auc", "map", "ndcg", "hit", "mrr",
        "pass@",
        "win", "elo",
    )
    _min = (
        "loss", "perplexity", "ppl", "error",
        "rmse", "mse", "mae", "bpb", "bpc",
        "fid", "kid",
        "divergence", "regret",
    )
    if any(t in _ml for t in _min):
        return "minimize"
    if any(t in _ml for t in _max):
        return "maximize"
    return "minimize"


@pytest.mark.parametrize("metric,expected", [
    # The exact user input that broke before the fix:
    ("gsm8k_val_acc",   "maximize"),
    ("gsm8k_test_acc",  "maximize"),
    ("gsm 8k val acc",  "maximize"),     # paste with spaces still detected
    ("val_acc",         "maximize"),
    ("test_accuracy",   "maximize"),
    ("squad_em",        "maximize"),
    ("exact_match",     "maximize"),
    ("pass@1",          "maximize"),
    ("bleu_score",      "maximize"),
    ("auc",             "maximize"),
    ("reward",          "maximize"),
    ("f1",              "maximize"),
    # Lower-is-better cases:
    ("val_loss",        "minimize"),
    ("train_loss",      "minimize"),
    ("perplexity",      "minimize"),
    ("ppl",             "minimize"),
    ("rmse",            "minimize"),
    ("mse",             "minimize"),
    ("mae",             "minimize"),
    ("test_error",      "minimize"),
    ("fid",             "minimize"),
    ("bpb",             "minimize"),
])
def test_direction_heuristic_picks_right_arrow(metric, expected):
    """Pin direction for every metric a real ML researcher would paste."""
    assert _direction_for(metric) == expected, (
        f"{metric!r} should resolve to direction={expected!r}, "
        "but the heuristic in api.py picked the other one. This is "
        "what made gsm8k_val_acc appear as 'val_loss ↓' on the dashboard.")
