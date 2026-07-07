"""Unit tests for the scoping gate (Phase 0).

Covers backend.app.scoping, the scope_* helpers in backend.app.council, and
lit_agent.discover_for_purpose. Every LLM / network call is monkeypatched, so
these run hardware-free and key-free like the rest of the unit suite.
"""
from __future__ import annotations

import time

import pytest


# ── canned synthesis used across the flow tests ──────────────────────────────
def _synth():
    return {
        "problem_restated": "Defend an open-weight LLM against backdoors.",
        "sota_summary": "Defenses are weak [shuai2024survey].",
        "user_ideas_assessment": [
            {"idea": "fine-prune the model", "verdict": "risky",
             "closest_prior_work": ["shuai2024survey", "ghost2099fake"],
             "novel_delta": "none", "cheap_kill_test": "plant + prune, measure ASR"},
        ],
        "new_ideas": [
            {"idea": "spectral weight ablation", "why": "anomalies live in weights",
             "idea_class": "ORTHOGONAL", "closest_prior_work": ["ziqian2025watch"],
             "novel_delta": "reference-free", "cheap_kill_test": "PCA on the delta"},
        ],
        "open_questions": ["what utility drop is acceptable?"],
        "recommended_direction": "Reproduce [ziqian2025watch] then extend it.",
    }


def _papers():
    return [
        {"key": "shuai2024survey", "title": "A Survey of Backdoors", "year": "2024",
         "authors": "Shuai", "abstract": "survey", "relevance": "shared kw"},
        {"key": "ziqian2025watch", "title": "Watch the Weights", "year": "2025",
         "authors": "Ziqian", "abstract": "weights", "relevance": "shared kw"},
    ]


# ════════════════════════════ gate flag ════════════════════════════════════
def test_gate_enabled_default_on(arui_env, monkeypatch):
    from backend.app import scoping
    monkeypatch.delenv("ARUI_SCOPING_GATE", raising=False)
    assert scoping.gate_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "False", "no", "off", ""])
def test_gate_disabled_values(arui_env, monkeypatch, val):
    from backend.app import scoping
    monkeypatch.setenv("ARUI_SCOPING_GATE", val)
    assert scoping.gate_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "on"])
def test_gate_enabled_values(arui_env, monkeypatch, val):
    from backend.app import scoping
    monkeypatch.setenv("ARUI_SCOPING_GATE", val)
    assert scoping.gate_enabled() is True


# ════════════════════════════ state ════════════════════════════════════════
def test_state_default_is_idle(arui_env):
    from backend.app import scoping
    assert scoping.state_get() == {"status": "idle"}


def test_state_set_get_roundtrip_and_patch(arui_env):
    from backend.app import scoping
    scoping.state_set({"status": "searching", "papers": []})
    got = scoping.state_get()
    assert got["status"] == "searching"
    assert "updated_at" in got            # state_set stamps it
    scoping._patch(status="awaiting_user", synthesis={"x": 1})
    got = scoping.state_get()
    assert got["status"] == "awaiting_user"
    assert got["synthesis"] == {"x": 1}
    assert got["papers"] == []            # _patch merges, doesn't clobber


# ════════════════════════ council scope helpers ════════════════════════════
def test_scoping_order_prefers_configured_model(arui_env, monkeypatch):
    from backend.app import council
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    assert council._scoping_order({"scoping_model": "gemini"})[0] == "gemini"
    assert council._scoping_order({"scoping_model": "claude"})[0] == "claude"
    assert council._scoping_order({"scoping_model": "openai"})[0] == "openai"
    # every available reviewer is present, just reordered
    assert set(council._scoping_order({"scoping_model": "claude"})) == {
        "gemini", "openai", "claude"}


def test_scoping_order_default_and_unknown_fall_back_to_gemini(arui_env, monkeypatch):
    from backend.app import council
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    assert council._scoping_order({})[0] == "gemini"
    assert council._scoping_order({"scoping_model": "bogus"})[0] == "gemini"


def test_scoping_order_only_includes_available(arui_env, monkeypatch):
    from backend.app import council
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert council._scoping_order({"scoping_model": "claude"}) == ["gemini"]


def test_validate_scope_keys_drops_hallucinated_citations(arui_env):
    from backend.app import council
    synth = _synth()
    cleaned = council._validate_scope_keys(synth, {"shuai2024survey", "ziqian2025watch"})
    # the fake "ghost2099fake" key is removed; the real one stays
    assert cleaned["user_ideas_assessment"][0]["closest_prior_work"] == ["shuai2024survey"]
    assert cleaned["new_ideas"][0]["closest_prior_work"] == ["ziqian2025watch"]


def test_scope_papers_block_renders_keys_and_truncates(arui_env):
    from backend.app import council
    block = council._scope_papers_block(_papers())
    assert "[shuai2024survey]" in block and "[ziqian2025watch]" in block
    tiny = council._scope_papers_block(_papers(), max_chars=10)
    assert len(tiny) <= 200       # truncation kicks in well before both papers


# ════════════════════════════ lit agent ════════════════════════════════════
def test_discover_for_purpose_dedupes_and_caches(arui_env, monkeypatch):
    from backend.app import lit_agent
    from backend.app.db import SessionLocal
    from backend.app.models import PaperCitation
    monkeypatch.setattr(time, "sleep", lambda *a, **k: None)
    canned = [
        {"title": "Backdoor Attacks on LLMs", "year": "2024", "authors": "A B",
         "abstract": "x", "arxiv_id": "2401.0001"},
        {"title": "Weight Monitoring Defenses", "year": "2025", "authors": "C D",
         "abstract": "y", "arxiv_id": "2502.0002"},
    ]
    calls = []
    monkeypatch.setattr(lit_agent, "search",
                        lambda q, limit=20: calls.append(q) or list(canned))
    out = lit_agent.discover_for_purpose(
        "trigger-agnostic backdoor defense for open-weight LLMs",
        "fine-pruning\nweight monitoring", max_papers=24)
    # multiple queries derived from purpose + seed lines
    assert len(calls) >= 2
    # the same two papers across queries collapse to two unique entries
    assert len(out) == 2
    assert all(p.get("key") for p in out)
    assert all("relevance" in p for p in out)
    # cached as PaperCitation rows for paper-mode reuse
    db = SessionLocal()
    try:
        assert db.query(PaperCitation).count() == 2
        assert {c.source for c in db.query(PaperCitation).all()} == {"scope"}
    finally:
        db.close()


def test_search_merges_semantic_and_arxiv_dedup(arui_env, monkeypatch):
    from backend.app import lit_agent
    monkeypatch.setattr(lit_agent, "_semantic_search",
                        lambda q, limit=20: [{"title": "Shared Paper", "year": "2024",
                                              "authors": "X", "abstract": "a"}])
    monkeypatch.setattr(lit_agent, "_arxiv_search",
                        lambda q, limit=20, ml_only=True: [
                            {"title": "Shared Paper", "year": "2024", "authors": "X",
                             "abstract": "a", "arxiv_id": "1"},
                            {"title": "Arxiv Only Paper", "year": "2023",
                             "authors": "Y", "abstract": "b", "arxiv_id": "2"}])
    rows = lit_agent.search("anything", limit=20)
    titles = sorted(r["title"] for r in rows)
    assert titles == ["Arxiv Only Paper", "Shared Paper"]   # dedup by title


# ════════════════════════ scoping pure helpers ═════════════════════════════
def test_approved_ideas_respects_keep_indices(arui_env):
    from backend.app import scoping
    st = {"synthesis": _synth()}
    # keep only the new idea, drop the user idea
    approved = scoping._approved_ideas(st, keep_user=[], keep_new=[0])
    assert len(approved) == 1
    a = approved[0]
    assert a["what"] == "spectral weight ablation"
    assert a["idea_class"] == "ORTHOGONAL"
    assert a["acceptance"] == "PCA on the delta"     # kill test -> acceptance
    # default (None) keeps everything
    assert len(scoping._approved_ideas(st, None, None)) == 2


def test_render_literature_review_and_scope_brief(arui_env):
    from backend.app import scoping
    st = {"synthesis": _synth(), "papers": _papers()}
    md = scoping._render_literature_review(st)
    assert "## State of the art" in md and "[ziqian2025watch]" in md
    brief = scoping._scope_brief(st, "my final direction")
    assert "my final direction" in brief
    assert "directives.jsonl" in brief


# ════════════════════════════ flow (mocked LLM) ════════════════════════════
def test_chat_appends_user_and_agent_messages(arui_env, monkeypatch):
    from backend.app import scoping, council
    scoping.state_set({"status": "awaiting_user", "synthesis": _synth(),
                       "papers": _papers(), "messages": []})
    monkeypatch.setattr(council, "scope_chat",
                        lambda *a, **k: "Here is my critical pushback.")
    out = scoping.chat("is this novel?")
    roles = [m["role"] for m in out["messages"]]
    assert roles == ["user", "agent"]
    assert out["messages"][-1]["text"] == "Here is my critical pushback."


def test_finalize_updates_synthesis_from_conversation(arui_env, monkeypatch):
    from backend.app import scoping, council
    scoping.state_set({"status": "awaiting_user", "synthesis": _synth(),
                       "papers": _papers(), "messages": [
                           {"role": "user", "text": "drop idea 1"}]})
    new = dict(_synth()); new["recommended_direction"] = "REVISED PLAN"
    monkeypatch.setattr(council, "scope_finalize", lambda *a, **k: new)
    out = scoping.finalize()
    assert out["synthesis"]["recommended_direction"] == "REVISED PLAN"


def test_confirm_preview_is_a_dry_run(arui_env, monkeypatch):
    from backend.app import scoping, realrun
    started = []
    monkeypatch.setattr(realrun, "start_real", lambda cfg, **k: started.append(cfg))
    scoping.state_set({"status": "awaiting_user", "preview": True,
                       "synthesis": _synth(), "papers": _papers()})
    out = scoping.confirm(final_direction="x")
    assert out["status"] == "confirmed"
    assert out["seeded_directives"] == []        # nothing seeded in preview
    assert started == []                         # the agent is NOT spawned


def test_confirm_seeds_directives_and_launches(arui_env, monkeypatch, setting_setter):
    from backend.app import scoping, realrun, directives
    setting_setter("onboarding", {"repo_name": "proj", "purpose": "p"})
    started = []
    monkeypatch.setattr(realrun, "start_real", lambda cfg, **k: started.append(cfg))
    scoping.state_set({"status": "awaiting_user", "preview": False,
                       "synthesis": _synth(), "papers": _papers()})
    out = scoping.confirm(final_direction="the agreed plan")
    assert out["status"] == "confirmed"
    # both ideas became SCIENCE directives in the queue
    science = [d for d in directives.read_all() if d.get("type") == "SCIENCE"]
    assert len(science) == 2
    assert all(d.get("author") == "scope" for d in science)
    assert any("spectral weight ablation" in d["what"] for d in science)
    # the agent was launched, grounded with a scope_brief
    assert len(started) == 1
    assert "scope_brief" in started[0]
    assert "the agreed plan" in started[0]["scope_brief"]
    # the workspace got the cached review + lessons.md related-work
    ws = arui_env / "workspace" / "proj"
    assert (ws / "literature_review.md").exists()
    assert "Related work / SOTA" in (ws / "lessons.md").read_text()


def test_skip_preview_does_not_launch(arui_env, monkeypatch):
    from backend.app import scoping, realrun
    started = []
    monkeypatch.setattr(realrun, "start_real", lambda cfg, **k: started.append(cfg))
    scoping.state_set({"status": "awaiting_user", "preview": True})
    out = scoping.skip(reason="expert")
    assert out["status"] == "skipped"
    assert started == []


def test_skip_nonpreview_launches(arui_env, monkeypatch, setting_setter):
    from backend.app import scoping, realrun
    setting_setter("onboarding", {"repo_name": "proj"})
    started = []
    monkeypatch.setattr(realrun, "start_real", lambda cfg, **k: started.append(cfg))
    scoping.state_set({"status": "awaiting_user", "preview": False})
    out = scoping.skip()
    assert out["status"] == "skipped"
    assert len(started) == 1


# ════════════════════════════ API endpoints ════════════════════════════════
@pytest.fixture
def client(arui_env, fake_subprocess):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.app.api import router
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as c:
        yield c


def test_api_scope_status_idle(client):
    r = client.get("/api/scope/status")
    assert r.status_code == 200
    assert r.json()["status"] == "idle"


def test_api_scope_start_preview_returns_searching(client, monkeypatch):
    # don't let the background worker do real work
    from backend.app import lit_agent, council
    monkeypatch.setattr(lit_agent, "discover_for_purpose",
                        lambda *a, **k: [])
    monkeypatch.setattr(council, "scope_review", lambda *a, **k: _synth())
    r = client.post("/api/scope/start_preview",
                    json={"purpose": "p", "metric": "m", "seed_ideas": ""})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "searching" and body["preview"] is True


def test_api_scope_chat_endpoint(client, monkeypatch):
    from backend.app import scoping, council
    scoping.state_set({"status": "awaiting_user", "synthesis": _synth(),
                       "papers": [], "messages": []})
    monkeypatch.setattr(council, "scope_chat", lambda *a, **k: "reply!")
    r = client.post("/api/scope/chat", json={"text": "hello"})
    assert r.status_code == 200
    assert r.json()["messages"][-1]["text"] == "reply!"


def test_api_scope_confirm_preview_endpoint(client):
    from backend.app import scoping
    scoping.state_set({"status": "awaiting_user", "preview": True,
                       "synthesis": _synth(), "papers": []})
    r = client.post("/api/scope/confirm", json={"final_direction": "go"})
    assert r.status_code == 200
    assert r.json()["status"] == "confirmed"


# ════════════════ onboarding -> gate wiring (the key seam) ══════════════════
def test_onboarding_triggers_scoping_when_gate_on(client, monkeypatch):
    from backend.app import scoping, realrun
    monkeypatch.setenv("ARUI_SCOPING_GATE", "1")
    scope_calls, agent_calls = [], []
    monkeypatch.setattr(scoping, "start",
                        lambda cfg, **k: scope_calls.append(cfg) or {"status": "scoping"})
    monkeypatch.setattr(realrun, "start_real", lambda cfg, **k: agent_calls.append(cfg))
    from backend.app import token_check
    monkeypatch.setattr(token_check, "check_all",
                        lambda cfg: {"claude": {"ok": True},
                                     "advisor": {"provider": "claude"}})
    r = client.post("/api/onboarding", json={
        "repo_name": "p", "metric": "m", "claude_token": "sk-ant-x", "purpose": "y"})
    assert r.json()["status"] == "scoping"
    assert len(scope_calls) == 1          # scoping started…
    assert agent_calls == []              # …and the agent is NOT spawned yet


def test_onboarding_starts_agent_directly_when_gate_off(client, monkeypatch):
    from backend.app import scoping, realrun
    monkeypatch.setenv("ARUI_SCOPING_GATE", "0")
    scope_calls, agent_calls = [], []
    monkeypatch.setattr(scoping, "start", lambda cfg, **k: scope_calls.append(cfg))
    monkeypatch.setattr(realrun, "start_real", lambda cfg, **k: agent_calls.append(cfg))
    from backend.app import token_check
    monkeypatch.setattr(token_check, "check_all",
                        lambda cfg: {"claude": {"ok": True},
                                     "advisor": {"provider": "claude"}})
    r = client.post("/api/onboarding", json={
        "repo_name": "p", "metric": "m", "claude_token": "sk-ant-x", "purpose": "y"})
    assert r.json()["status"] == "started"
    assert len(agent_calls) == 1          # agent launched directly…
    assert scope_calls == []              # …scoping bypassed
