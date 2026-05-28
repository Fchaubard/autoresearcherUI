"""Unit tests for backend.app.pi."""
from __future__ import annotations


def test_provider_for_routes_by_prefix(arui_env, monkeypatch):
    from backend.app.pi import _provider_for
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    assert _provider_for("gemini-2.5-pro") == "gemini"
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    assert _provider_for("gpt-5") == "openai"
    assert _provider_for("o4-mini") == "openai"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "c")
    assert _provider_for("claude-opus-4-6") == "claude"


def test_provider_for_none_without_key(arui_env, monkeypatch):
    from backend.app.pi import _provider_for
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert _provider_for("gemini-2.5-pro") is None
    assert _provider_for("gpt-5") is None
    assert _provider_for("claude-opus") is None
    assert _provider_for("mystery") is None


def test_plateau_signal_too_few_points(arui_env, monkeypatch):
    from backend.app import pi
    # No metrics at all → None
    monkeypatch.setattr(pi.metrics, "query", lambda rid, keys: {})
    assert pi._plateau_signal("r1") is None


def test_plateau_signal_diverging(arui_env, monkeypatch):
    from backend.app import pi
    # 20 points, last bigger than first → diverging
    pts = [(i, float(i)) for i in range(20)]
    monkeypatch.setattr(pi.metrics, "query", lambda rid, keys: {"loss": pts})
    sig = pi._plateau_signal("r1")
    assert sig is not None
    assert sig["trend"] == "diverging"


def test_plateau_signal_plateaued(arui_env, monkeypatch):
    from backend.app import pi
    pts = [(i, 0.5) for i in range(20)]
    monkeypatch.setattr(pi.metrics, "query", lambda rid, keys: {"loss": pts})
    sig = pi._plateau_signal("r1")
    assert sig is not None
    assert sig["trend"] == "plateaued"


def test_build_context_shape(arui_env, db_session, make_project, make_run,
                                fake_subprocess, monkeypatch):
    from backend.app import pi
    from backend.app.models import Event, Gpu
    make_project()
    db_session.add(Gpu(index=0, util_pct=2.0, vram_used_mb=100))
    db_session.add(Gpu(index=1, util_pct=80.0, vram_used_mb=20000))
    make_run(id="r1", status="running", started_at="2026-01-01T00:00:00+00:00")
    make_run(id="r2", status="kept", headline_metric=0.5)
    db_session.add(Event(id="e1", type="info", message="test"))
    db_session.commit()
    monkeypatch.setattr(pi.metrics, "query", lambda rid, keys: {})
    ctx = pi._build_context()
    assert ctx["gpus_total"] == 2
    assert ctx["gpus_idle"] == 1
    assert isinstance(ctx["recent_runs"], list)
    assert any(r["id"] == "r1" for r in ctx["recent_runs"])
    assert isinstance(ctx["recent_events"], list)
    assert "agent_terminal_tail" in ctx


def test_build_paper_context_shape(arui_env, db_session, make_project,
                                       make_run, fake_subprocess,
                                       monkeypatch):
    from backend.app import pi, paper
    from backend.app.models import (PaperClaim, PaperDecision, PaperMeta,
                                     Gpu)
    make_project(name="P", metric_direction="minimize")
    paper.set_project_mode("paper")
    db_session.add(PaperMeta(id="pm", venue="V", deadline_iso=""))
    db_session.add(PaperClaim(id="c1", title="x", status="active",
                                evidence_strength="strong"))
    db_session.add(PaperDecision(id="d1", source="agent", kind="cite_paper",
                                   title="t", status="pending"))
    db_session.add(Gpu(index=0, util_pct=1.0, vram_used_mb=100))
    make_run(id="pr1", context="paper", status="kept",
             headline_metric=0.1, ended_at="2026-01-01T00:00:00+00:00",
             paper_claim_id="c1", config={"dataset": "cifar"})
    db_session.commit()
    ctx = pi._build_paper_context()
    assert ctx["mode"] == "paper"
    assert ctx["venue"] == "V"
    assert ctx["n_claims"] == 1
    assert ctx["pending_decisions"] == 1
    assert ctx["paper_runs_by_status"].get("kept") == 1
    assert any(r["id"] == "pr1" for r in ctx["recent_finished_runs"])
    assert "build_status" in ctx


def test_cycle_skips_when_disabled(arui_env, setting_setter, monkeypatch):
    from backend.app import pi
    setting_setter("onboarding", {"pi_agent_enabled": False})
    # Even without an API key
    out = pi.cycle()
    assert out is None


def test_cycle_skips_when_no_key(arui_env, setting_setter, monkeypatch):
    from backend.app import pi
    setting_setter("onboarding", {"pi_agent_enabled": True,
                                    "pi_agent_model": "gemini-2.5-pro"})
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    out = pi.cycle()
    assert out is None


def test_cycle_dispatches_to_paper_when_in_paper_mode(arui_env, monkeypatch):
    """When mode == 'paper', cycle() delegates to cycle_paper()."""
    from backend.app import pi, paper
    paper.set_project_mode("paper")
    called = {"v": False}

    def fake_paper(force=False):
        called["v"] = True
        return {"mode": "paper", "concerns": "OK.", "messages_sent": 0,
                "model": "x"}
    monkeypatch.setattr(pi, "cycle_paper", fake_paper)
    out = pi.cycle()
    assert called["v"]
    assert out["mode"] == "paper"


def test_cycle_paper_skips_when_no_key(arui_env, setting_setter,
                                          monkeypatch):
    from backend.app import pi, paper
    paper.set_project_mode("paper")
    setting_setter("onboarding", {"pi_agent_enabled": True,
                                    "pi_agent_model": "gemini-2.5-pro"})
    # Council module-load may have read keys.env; force-clear before running.
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert pi.cycle_paper() is None


def test_cycle_paper_happy_path(arui_env, setting_setter, db_session,
                                  make_project, monkeypatch, fake_subprocess):
    """When LLM returns valid JSON, cycle_paper persists chat + event
    and reports messages_sent."""
    import json
    from backend.app import pi, paper
    from backend.app.models import ChatMessage, Event, PaperMeta
    paper.set_project_mode("paper")
    make_project()
    db_session.add(PaperMeta(id="pm1", venue="V"))
    db_session.commit()
    setting_setter("onboarding", {"pi_agent_enabled": True,
                                    "pi_agent_model": "gemini-2.5-pro"})
    monkeypatch.setenv("GEMINI_API_KEY", "stub")
    payload = {"concerns": "All looks good.",
                "messages": ["queue ablation Y", "kill pr-abc"]}
    monkeypatch.setattr(pi, "_call",
                         lambda model, sys, user: json.dumps(payload))
    out = pi.cycle_paper()
    assert out is not None
    assert out["concerns"] == "All looks good."
    # 2 messages → 2 tmux send-keys cycles (each is 2 calls)
    assert out["messages_sent"] >= 1
    # persisted chat + event
    assert db_session.query(ChatMessage).count() >= 1
    assert db_session.query(Event).count() >= 1
