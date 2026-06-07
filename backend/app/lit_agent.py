"""Lit Agent — bibliography discovery via arxiv + Semantic Scholar.

Cheaper agent (uses Haiku/Flash by default). Pulls candidate papers
matching the project's claims/keywords, ranks by relevance, files
`cite_paper` decisions for the most promising ones, and maintains a
discoverable list in PaperCitation.

v1 surface: a `search(query)` helper and a `tick()` that runs
periodically when paper mode is active.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from . import paper
from .db import SessionLocal
from .models import (PaperCitation, PaperClaim, PaperMeta, Project)

_ARXIV_API = "http://export.arxiv.org/api/query"
_SEMANTIC_API = "https://api.semanticscholar.org/graph/v1/paper/search"


_ARXIV_ML_CATEGORIES = ("cs.LG", "cs.CL", "cs.AI", "cs.NE", "stat.ML",
                         "cs.CV", "cs.IR")


def _extract_keywords(text: str, k: int = 8) -> list[str]:
    """Pull the most useful keywords from a noisy claim title/summary.
    Heuristic: split into words, drop short / stop / generic words, dedup,
    cap to k. Adequate for arxiv's keyword index."""
    stop = {"a","an","the","of","and","or","for","to","is","are","was","were",
            "be","been","with","that","this","these","those","on","in","at","by",
            "as","than","more","less","most","best","our","we","its","it",
            "from","over","into","can","may","do","does","one","two","three",
            "all","any","some","new","such","model","models","method","methods",
            "approach","approaches","using","via","based","propose","proposes",
            "shows","show","results","result","experiments","experiment",
            "study","studies","paper","papers","work","works","analysis"}
    seen = set()
    out: list[str] = []
    for w in text.split():
        w = "".join(c for c in w.lower() if c.isalnum() or c in "-_")
        if not w or len(w) < 3 or w in stop or w in seen:
            continue
        seen.add(w)
        out.append(w)
        if len(out) >= k:
            break
    return out


def _arxiv_search(query: str, limit: int = 20,
                   ml_only: bool = True) -> list[dict]:
    """Return [{arxiv_id, title, authors, year, abstract, ...}] from arxiv.
    Builds a category-filtered query so we get cs.LG / cs.CL / etc., not
    physics — arxiv ranks by relevance only within whatever subset matches.
    Also robustly handles broad natural-language queries by extracting
    keywords and ANDing them."""
    kws = _extract_keywords(query, k=6)
    if not kws:
        return []
    # AND only the 3 strongest keywords — ANDing 5+ with the category filter
    # is so strict it routinely returns 0 rows, defeating the fallback that
    # exists precisely for when Semantic Scholar is rate-limited.
    kw_clause = "+AND+".join(f"all:{k}" for k in kws[:3])
    if ml_only:
        cat_clause = "+OR+".join(f"cat:{c}" for c in _ARXIV_ML_CATEGORIES)
        sq = f"({kw_clause})+AND+({cat_clause})"
    else:
        sq = kw_clause
    # Build URL manually because arxiv expects literal `+AND+` / `cat:` —
    # urlencode would percent-encode those and break the query.
    url = (_ARXIV_API
           + f"?search_query={sq}"
           + f"&start=0&max_results={limit}"
           + "&sortBy=relevance&sortOrder=descending")
    req = urllib.request.Request(url, headers={"User-Agent": "autoresearcherUI/1"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            xml_text = resp.read().decode("utf-8", errors="ignore")
    except Exception as e:                              # noqa: BLE001
        print(f"[lit] arxiv search failed: {e}", flush=True)
        return []
    ns = {"a": "http://www.w3.org/2005/Atom",
          "arxiv": "http://arxiv.org/schemas/atom"}
    out = []
    try:
        root = ET.fromstring(xml_text)
        for entry in root.findall("a:entry", ns):
            arxiv_id_raw = (entry.findtext("a:id", default="", namespaces=ns)
                            or "")
            arxiv_id = arxiv_id_raw.rsplit("/", 1)[-1].split("v")[0]
            title = (entry.findtext("a:title", default="", namespaces=ns)
                     or "").strip().replace("\n", " ")
            published = (entry.findtext("a:published", default="",
                                        namespaces=ns) or "")
            year = published[:4] if published else ""
            authors = [a.findtext("a:name", default="", namespaces=ns)
                       for a in entry.findall("a:author", ns)]
            abstract = (entry.findtext("a:summary", default="",
                                        namespaces=ns) or "").strip()
            out.append({
                "arxiv_id": arxiv_id,
                "title": title,
                "authors": ", ".join([a for a in authors if a]),
                "year": year,
                "abstract": abstract,
            })
    except Exception as e:                              # noqa: BLE001
        print(f"[lit] arxiv parse failed: {e}", flush=True)
    return out


def _semantic_search(query: str, limit: int = 20) -> list[dict]:
    """Use Semantic Scholar; free tier, no key needed. The free tier is
    aggressively rate-limited (HTTP 429), so we retry a couple of times with
    backoff before giving up — otherwise the scoping sweep returns 1 paper
    instead of a dozen purely because of a transient 429."""
    import time as _t
    params = {
        "query": query, "limit": str(limit),
        "fields": "title,authors,year,abstract,externalIds,citationCount",
    }
    url = _SEMANTIC_API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "autoresearcherUI/1"})
    data = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="ignore"))
            break
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                _t.sleep(2.0 * (attempt + 1))          # 2s, 4s backoff
                continue
            print(f"[lit] semantic scholar HTTP {e.code}", flush=True)
            return []
        except Exception as e:                          # noqa: BLE001
            print(f"[lit] semantic scholar failed: {e}", flush=True)
            return []
    if data is None:
        return []
    out = []
    for p in data.get("data", []):
        ids = p.get("externalIds") or {}
        out.append({
            "arxiv_id": ids.get("ArXiv", ""),
            "semantic_scholar_id": p.get("paperId", ""),
            "doi": ids.get("DOI", ""),
            "title": p.get("title", ""),
            "authors": ", ".join(a.get("name", "")
                                  for a in (p.get("authors") or [])),
            "year": str(p.get("year") or ""),
            "abstract": p.get("abstract", "") or "",
            "citation_count": p.get("citationCount", 0),
        })
    return out


def _bibtex_key(title: str, year: str, first_author: str) -> str:
    """Build a stable bibtex key."""
    fa = (first_author.split(",")[0].split(" ")[0] or "anon").lower()
    fa = "".join(c for c in fa if c.isalpha())[:12]
    title_word = ""
    for w in title.lower().split():
        w_clean = "".join(c for c in w if c.isalpha())
        if len(w_clean) > 3 and w_clean not in (
                "the", "of", "for", "and", "with", "from", "using"):
            title_word = w_clean; break
    return f"{fa}{year}{title_word}"[:32]


def _bibtex_for(p: dict) -> str:
    """Build a minimal bibtex entry for a result row."""
    key = _bibtex_key(p.get("title", ""), p.get("year", ""),
                     p.get("authors", "") or "anon")
    fields = [f"  title = {{{p.get('title','').strip()}}}",
              f"  author = {{{p.get('authors','').strip()}}}",
              f"  year = {{{p.get('year','').strip()}}}"]
    if p.get("arxiv_id"):
        fields.append(f"  eprint = {{{p['arxiv_id']}}}")
        fields.append(f"  archivePrefix = {{arXiv}}")
    if p.get("doi"):
        fields.append(f"  doi = {{{p['doi']}}}")
    fields.append(f"  abstract = {{{p.get('abstract','')[:1000]}}}")
    return ("@article{" + key + ",\n" + ",\n".join(fields) + "\n}\n")


# ── public API ────────────────────────────────────────────────────────────


def search(query: str, limit: int = 20) -> list[dict]:
    """Semantic Scholar has much better natural-language relevance ranking,
    so it leads; arxiv (cs.* category-filtered) is merged in to backfill
    coverage and to survive Semantic-Scholar rate-limits. Dedupe by a
    normalised title so the same paper from both sources collapses to one."""
    rows = _semantic_search(query, limit=limit)
    if len(rows) < limit:
        rows = rows + _arxiv_search(query, limit=limit)
    # dedupe by normalised title, preserving first (semantic-ranked) order
    seen, out = set(), []
    for r in rows:
        t = "".join(c for c in (r.get("title", "") or "").lower()
                    if c.isalnum())[:60]
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(r)
    return out[:max(limit, len(out))]


def _build_relevance(claim_title: str, claim_summary: str,
                      paper: dict) -> str:
    """Concrete one-liner: 'Relates to claim X because of overlapping
    keywords Y. {paper_year} {venue}.' We compare keyword sets and call
    out the overlap so the user can decide fast."""
    kws_claim = set(_extract_keywords(
        (claim_title or "") + " " + (claim_summary or ""), k=20))
    kws_paper = set(_extract_keywords(
        (paper.get("title","") or "") + " " +
        (paper.get("abstract","") or ""), k=30))
    overlap = sorted(kws_claim & kws_paper)[:6]
    parts = []
    if overlap:
        parts.append("Shared keywords: " + ", ".join(f"`{w}`" for w in overlap))
    if paper.get("citation_count"):
        parts.append(f"{paper['citation_count']} citations")
    if paper.get("year"):
        parts.append(str(paper["year"]))
    if not parts:
        return "Surfaced by Lit Agent; relevance not yet assessed."
    return " · ".join(parts)


def upsert_citation(p: dict, source: str, relevance_md: str = "") -> str:
    """Add or update a PaperCitation row. Returns its bibtex key."""
    bibtex = _bibtex_for(p)
    key = bibtex.split("{", 1)[1].split(",", 1)[0]
    db = SessionLocal()
    try:
        existing = db.query(PaperCitation).filter(
            PaperCitation.key == key).first()
        if existing:
            if relevance_md and not (existing.relevance_md or "").strip():
                existing.relevance_md = relevance_md
                db.commit()
            return key
        db.add(PaperCitation(
            key=key, bibtex_md=bibtex, source=source,
            arxiv_id=p.get("arxiv_id", ""),
            semantic_scholar_id=p.get("semantic_scholar_id", ""),
            doi=p.get("doi", ""),
            title=p.get("title", ""), authors=p.get("authors", ""),
            year=p.get("year", ""), abstract_md=p.get("abstract", ""),
            relevance_md=relevance_md or ""))
        db.commit()
    finally:
        db.close()
    return key


def discover_for_purpose(purpose: str, seed_ideas: str = "",
                          max_papers: int = 24,
                          on_progress=None) -> list[dict]:
    """Front-of-project literature sweep, keyed off the research *purpose*
    (and the user's seed ideas) rather than paper-mode claims.

    Used by the scoping gate BEFORE any GPU is spent. Builds several
    queries — the purpose as a whole plus each seed-idea line — searches
    arxiv/Semantic Scholar, dedupes, upserts a PaperCitation row per hit
    (``source='scope'``) so the result is ALREADY cached for paper-mode
    lit_review + the author agent, and returns a compact list for the
    council to synthesize from.

    ``on_progress(found_so_far:int, query:str)`` is called after each query
    so the scoping modal can show live progress.
    """
    queries: list[str] = []
    p = (purpose or "").strip()
    if p:
        queries.append(p[:240])
        kws = _extract_keywords(p, k=6)
        if kws:
            queries.append(" ".join(kws))
    for line in (seed_ideas or "").splitlines():
        line = line.strip().lstrip("-*0123456789. ").strip()
        if len(line) >= 8:
            queries.append(line[:200])
    # de-dup queries while preserving order
    seen_q, uniq_q = set(), []
    for q in queries:
        kq = q.lower()
        if kq not in seen_q:
            seen_q.add(kq); uniq_q.append(q)
    per_q = max(6, max_papers // max(1, len(uniq_q)))
    out: list[dict] = []
    seen_keys: set[str] = set()
    import time as _t
    for qi, q in enumerate(uniq_q):
        if qi:
            _t.sleep(0.5)            # space requests → fewer burst-429s
        try:
            results = search(q, limit=per_q)
        except Exception as e:                              # noqa: BLE001
            print(f"[lit] scope query failed: {e}", flush=True)
            results = []
        for pp in results:
            key = _bibtex_key(pp.get("title", ""), pp.get("year", ""),
                              pp.get("authors", "") or "anon")
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)
            relevance = _build_relevance(purpose, seed_ideas, pp)
            try:
                upsert_citation(
                    pp, source="scope", relevance_md=relevance)
            except Exception as e:                          # noqa: BLE001
                print(f"[lit] scope upsert failed: {e}", flush=True)
            out.append({
                "key": key,
                "title": pp.get("title", ""),
                "authors": pp.get("authors", ""),
                "year": pp.get("year", ""),
                "abstract": (pp.get("abstract", "") or "")[:1200],
                "arxiv_id": pp.get("arxiv_id", ""),
                "relevance": relevance,
            })
            if len(out) >= max_papers:
                break
        if on_progress:
            try: on_progress(len(out), q)
            except Exception: pass
        if len(out) >= max_papers:
            break
    return out


def auto_discover_for_claims(max_per_claim: int = 5) -> int:
    """Run for each active claim: search → file cite_paper decisions for
    the top candidates. Returns the number of decisions filed."""
    filed = 0
    db = SessionLocal()
    try:
        claims = db.query(PaperClaim).filter(
            PaperClaim.status == "active").all()
        already_keys = {c.key for c in db.query(PaperCitation).all()}
    finally:
        db.close()
    for c in claims:
        # Focused query: extract keywords from claim title (the summary
        # adds too much noise for natural-language search APIs).
        kws = _extract_keywords(c.title or "", k=6)
        q = " ".join(kws) if kws else (c.title or "")
        if not q.strip():
            continue
        results = search(q, limit=max_per_claim)
        for p in results:
            key = _bibtex_key(p.get("title", ""), p.get("year", ""),
                              p.get("authors", "") or "anon")
            if key in already_keys:
                continue
            relevance = _build_relevance(c.title or "", c.summary_md or "", p)
            upsert_citation(
                p, source=("arxiv" if p.get("arxiv_id")
                           else "semantic_scholar"),
                relevance_md=relevance)
            already_keys.add(key)
            # Compact author list — first 3 + et al for the decision body.
            authors = (p.get("authors", "") or "").split(", ")
            short_a = ", ".join(authors[:3]) + (
                f", + {len(authors)-3} more" if len(authors) > 3 else "")
            paper.file_decision(
                source="lit", kind="cite_paper",
                title=f"Cite '{p.get('title','')[:80]}'? ({p.get('year','')})",
                body_md=(f"**Why relevant to claim:** {relevance}\n\n"
                         f"**Claim:** {c.title}\n\n"
                         f"**Authors:** {short_a}\n\n"
                         f"**Abstract:** {(p.get('abstract','') or '')[:700]}"),
                default_action="approve",
                priority=10,
                linked_claim_id=c.id,
                linked_citation_key=key)
            filed += 1
    return filed
