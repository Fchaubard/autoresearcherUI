"""CPU-only mode: a missing nvidia-smi / 0 GPUs must be a valid CPU-only node,
never a fatal error. Covers GPU polling, the scoping preflight, and the
compute-context prompt guidance."""
import subprocess

import pytest


# ── GPU polling ─────────────────────────────────────────────────────────────
def test_poll_gpus_missing_nvidia_smi_clears_and_returns(arui_env, monkeypatch):
    from backend.app import monitor
    from backend.app.db import SessionLocal
    from backend.app.models import Gpu
    # seed a stale GPU row from a "previous run"
    db = SessionLocal()
    db.add(Gpu(index=0, model="A100", util_pct=10, vram_used_mb=1000,
               total_vram_mb=40000, temp_c=40, sampled_at="x"))
    db.commit(); db.close()

    def _no_smi(*a, **k):
        raise FileNotFoundError("nvidia-smi")
    monkeypatch.setattr(subprocess, "run", _no_smi)

    monitor._poll_gpus()                     # must NOT raise
    db = SessionLocal()
    try:
        assert db.query(Gpu).count() == 0    # stale rows cleared
    finally:
        db.close()


def test_gpu_count_zero_when_no_nvidia_smi(arui_env, monkeypatch):
    from backend.app import monitor
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    assert monitor.gpu_count() == 0


def test_poll_gpus_zero_rows_clears(arui_env, monkeypatch):
    from backend.app import monitor
    from backend.app.db import SessionLocal
    from backend.app.models import Gpu
    db = SessionLocal()
    db.add(Gpu(index=0, model="A100", util_pct=1, vram_used_mb=1,
               total_vram_mb=1, temp_c=1, sampled_at="x"))
    db.commit(); db.close()

    class R:
        returncode = 0
        stdout = ""            # nvidia-smi present but reports no GPUs
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: R())
    monitor._poll_gpus()
    db = SessionLocal()
    try:
        assert db.query(Gpu).count() == 0
    finally:
        db.close()


# ── scoping preflight ───────────────────────────────────────────────────────
def test_preflight_cpu_only_reports_success(arui_env, monkeypatch):
    from backend.app import scoping

    def _fake_run(argv, **k):
        class R:
            returncode = 127        # nvidia-smi not found / errored
            stdout = ""
            stderr = "not found"
        return R()
    monkeypatch.setattr(scoping.subprocess, "run", _fake_run)
    # block real network in the arxiv check
    monkeypatch.setattr(scoping, "_iso", lambda: "t")
    pf = scoping._run_preflight()
    gpu = [c for c in pf["checks"] if c["name"] == "gpu"][0]
    assert gpu["ok"] is True
    assert "CPU-only" in gpu["detail"]


# ── prompt guidance ─────────────────────────────────────────────────────────
def test_compute_context_note_cpu_only(arui_env, monkeypatch):
    from backend.app import realrun, monitor
    monkeypatch.setattr(monitor, "gpu_count", lambda: 0)
    note = realrun._compute_context_note()
    assert "CPU-ONLY" in note
    assert "stop" in note.lower()


def test_compute_context_note_gpu(arui_env, monkeypatch):
    from backend.app import realrun, monitor
    monkeypatch.setattr(monitor, "gpu_count", lambda: 8)
    note = realrun._compute_context_note()
    assert "8 GPU" in note


def test_setup_prompt_embeds_compute_context(arui_env, monkeypatch):
    from backend.app import realrun, monitor
    monkeypatch.setattr(monitor, "gpu_count", lambda: 0)
    p = realrun._setup_prompt({"purpose": "predict S&P returns with sklearn",
                               "metric": "val_mse"})
    assert "COMPUTE CONTEXT" in p
    assert "CPU-ONLY" in p


def test_pi_idle_escalation_noop_on_cpu_only(arui_env):
    """With 0 GPUs (CPU-only) the idle-GPU stall escalation must do nothing -
    no timer, no email - since there are no GPUs to sit idle."""
    from backend.app import pi
    from backend.app.db import SessionLocal
    from backend.app.models import Setting
    pi._idle_gpu_escalation({"gpus_total": 0, "gpus_idle": 0})
    db = SessionLocal()
    try:
        assert db.query(Setting).filter(
            Setting.key == pi._IDLE_GPU_STATE_KEY).first() is None
    finally:
        db.close()


def test_pi_system_prompt_has_cpu_only_clause(arui_env):
    from backend.app import pi
    assert "CPU-ONLY" in pi.SYSTEM and "gpus_total: 0" in pi.SYSTEM


def test_sklearn_snp_prompt_on_cpu_only_node(arui_env, monkeypatch):
    """The user's example: 'predict S&P returns with sklearn only ... val = MSE'
    on a CPU-only MacBook. The built agent brief must carry the purpose, the
    MSE metric, and CPU-only guidance that tells the agent to keep working
    (scaffold + CPU baselines) rather than treating 'no GPU' as 'stop'."""
    from backend.app import realrun, monitor
    monkeypatch.setattr(monitor, "gpu_count", lambda: 0)
    cfg = {
        "purpose": ("Predict S&P returns with the sklearn library only - simple "
                    "old-school models - and report the best one. Input: ticks "
                    "in any format from the S&P trailing 4 weeks; predict the "
                    "next 4 weeks of returns."),
        "metric": "val_mse",
        "eval": "MSE on a held-out 4-week window.",
        "seed_ideas": "Ridge, Lasso, RandomForest, GradientBoosting baselines.",
    }
    p = realrun._setup_prompt(cfg)
    assert "sklearn" in p and "val_mse" in p
    assert "CPU-ONLY" in p
    # CPU-only guidance must redirect to real CPU work, not a stop
    assert "CPU smoke tests" in p or "CPU-sized" in p
