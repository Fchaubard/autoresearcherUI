"""Unit tests for backend.app.lit_agent."""
from __future__ import annotations


def test_extract_keywords_dedup_and_stop(arui_env):
    from backend.app.lit_agent import _extract_keywords
    kws = _extract_keywords(
        "The effect of the diffusion ensemble on the test set is large")
    # stops "the", "of", "is", "on"
    assert "diffusion" in kws
    assert "ensemble" in kws
    assert "the" not in kws
    assert "of" not in kws


def test_extract_keywords_short_and_generic_dropped(arui_env):
    from backend.app.lit_agent import _extract_keywords
    kws = _extract_keywords("our new model achieves better results")
    # "our", "new", "model", "results" are all stopped
    assert "our" not in kws
    assert "model" not in kws
    assert "results" not in kws
    assert "achieves" in kws or "better" not in kws


def test_extract_keywords_caps_to_k(arui_env):
    from backend.app.lit_agent import _extract_keywords
    long_str = " ".join(f"word{i}" for i in range(50))
    kws = _extract_keywords(long_str, k=5)
    assert len(kws) == 5


def test_extract_keywords_deterministic(arui_env):
    from backend.app.lit_agent import _extract_keywords
    text = "discrete diffusion model evaluation"
    assert _extract_keywords(text) == _extract_keywords(text)


def test_build_relevance_with_overlap(arui_env):
    from backend.app.lit_agent import _build_relevance
    out = _build_relevance(
        claim_title="discrete diffusion improves likelihood",
        claim_summary="we beat baseline",
        paper={"title": "discrete diffusion via score entropy",
               "abstract": "we propose a discrete diffusion likelihood "
                            "estimator",
               "year": "2024", "citation_count": 42})
    assert "discrete" in out or "diffusion" in out
    assert "2024" in out
    assert "42" in out


def test_build_relevance_no_overlap(arui_env):
    from backend.app.lit_agent import _build_relevance
    out = _build_relevance(
        claim_title="quantum supremacy", claim_summary="",
        paper={"title": "raster scan in graphics",
               "abstract": "old crt rendering"})
    # No overlap → falls back to default sentence
    assert "relevance" in out.lower() or "lit agent" in out.lower()


def test_arxiv_search_returns_empty_on_no_keywords(arui_env):
    from backend.app import lit_agent
    # Only stop words → no kws → empty results
    out = lit_agent._arxiv_search("the of and to")
    assert out == []


def test_arxiv_search_parses_xml(arui_env, monkeypatch):
    """Mock urlopen → arxiv XML → parsed entry list."""
    import io
    from backend.app import lit_agent
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2401.12345v1</id>
    <title>A novel diffusion ensemble</title>
    <summary>We propose ensembles for diffusion models.</summary>
    <published>2024-01-12T00:00:00Z</published>
    <author><name>Jane Doe</name></author>
    <author><name>John Roe</name></author>
  </entry>
</feed>"""

    class FakeResp:
        def __init__(self, data): self._data = data
        def read(self): return self._data.encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_open(req, timeout=20):
        return FakeResp(xml)

    monkeypatch.setattr(lit_agent.urllib.request, "urlopen", fake_open)
    out = lit_agent._arxiv_search("diffusion ensemble", limit=5)
    assert len(out) == 1
    assert out[0]["arxiv_id"] == "2401.12345"
    assert "diffusion" in out[0]["title"].lower()
    assert out[0]["year"] == "2024"
    assert "Jane Doe" in out[0]["authors"]


def test_arxiv_search_includes_cat_filter(arui_env, monkeypatch):
    """The query URL must include ML category filters when ml_only=True."""
    from backend.app import lit_agent
    captured = {"url": ""}

    class FakeResp:
        def read(self): return b"<feed xmlns='http://www.w3.org/2005/Atom'/>"
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_open(req, timeout=20):
        captured["url"] = req.full_url
        return FakeResp()

    monkeypatch.setattr(lit_agent.urllib.request, "urlopen", fake_open)
    lit_agent._arxiv_search("diffusion ensemble", ml_only=True)
    url = captured["url"]
    # category filter should be in there
    assert "cat:cs.LG" in url
    assert "cat:cs.CL" in url


def test_arxiv_search_handles_network_error(arui_env, monkeypatch):
    from backend.app import lit_agent

    def boom(*a, **kw):
        raise RuntimeError("network down")
    monkeypatch.setattr(lit_agent.urllib.request, "urlopen", boom)
    assert lit_agent._arxiv_search("diffusion model") == []


def test_upsert_citation_new(arui_env, db_session):
    from backend.app import lit_agent
    from backend.app.models import PaperCitation
    key = lit_agent.upsert_citation(
        {"title": "Some paper", "year": "2024", "authors": "Smith, Lee",
         "arxiv_id": "2401.00001", "abstract": "abs"},
        source="arxiv", relevance_md="related")
    row = db_session.query(PaperCitation).filter(
        PaperCitation.key == key).first()
    assert row is not None
    assert row.source == "arxiv"
    assert row.arxiv_id == "2401.00001"
    assert row.relevance_md == "related"


def test_upsert_citation_idempotent_returns_same_key(arui_env, db_session):
    from backend.app import lit_agent
    from backend.app.models import PaperCitation
    p = {"title": "Stable Paper", "year": "2024", "authors": "Smith",
         "arxiv_id": "2401.00002", "abstract": ""}
    k1 = lit_agent.upsert_citation(p, source="arxiv")
    k2 = lit_agent.upsert_citation(p, source="arxiv",
                                     relevance_md="new note")
    assert k1 == k2
    rows = db_session.query(PaperCitation).filter(
        PaperCitation.key == k1).all()
    assert len(rows) == 1
    # second call fills relevance if missing
    assert "new note" in rows[0].relevance_md


def test_search_falls_back_to_arxiv(arui_env, monkeypatch):
    from backend.app import lit_agent
    monkeypatch.setattr(lit_agent, "_semantic_search",
                         lambda q, limit=20: [])
    fake_results = [{"title": "x", "arxiv_id": "abc"}]
    monkeypatch.setattr(lit_agent, "_arxiv_search",
                         lambda q, limit=20: fake_results)
    out = lit_agent.search("anything")
    assert out == fake_results


def test_search_returns_semantic_first(arui_env, monkeypatch):
    from backend.app import lit_agent
    monkeypatch.setattr(lit_agent, "_semantic_search",
                         lambda q, limit=20: [{"title": "ss-row"}])
    called = {"v": False}

    def boom(*a, **kw):
        called["v"] = True
        return []
    monkeypatch.setattr(lit_agent, "_arxiv_search", boom)
    out = lit_agent.search("anything")
    assert out == [{"title": "ss-row"}]
    assert called["v"] is False
