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
    kw_clause = "+AND+".join(f"all:{k}" for k in kws[:5])
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
    """Use Semantic Scholar; free tier, no key needed."""
    params = {
        "query": query, "limit": str(limit),
        "fields": "title,authors,year,abstract,externalIds,citationCount",
    }
    url = _SEMANTIC_API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "autoresearcherUI/1"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
    except Exception as e:                              # noqa: BLE001
        print(f"[lit] semantic scholar failed: {e}", flush=True)
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
    """Semantic Scholar has much better natural-language relevance ranking
    than arxiv's keyword-index search, so we try it first now. arxiv is
    the fallback (cs.* category-filtered)."""
    rows = _semantic_search(query, limit=limit)
    if not rows:
        rows = _arxiv_search(query, limit=limit)
    return rows


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
