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


def _arxiv_search(query: str, limit: int = 20) -> list[dict]:
    """Return [{arxiv_id, title, authors, year, abstract, ...}] from arxiv."""
    params = {
        "search_query": query,
        "start": "0",
        "max_results": str(limit),
        "sortBy": "relevance",
        "sortOrder": "descending",
    }
    url = _ARXIV_API + "?" + urllib.parse.urlencode(params)
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
    """Try arxiv first; fall back to Semantic Scholar."""
    rows = _arxiv_search(query, limit=limit)
    if not rows:
        rows = _semantic_search(query, limit=limit)
    return rows


def upsert_citation(p: dict, source: str) -> str:
    """Add or update a PaperCitation row. Returns its bibtex key."""
    bibtex = _bibtex_for(p)
    # parse the key out of the bibtex string
    key = bibtex.split("{", 1)[1].split(",", 1)[0]
    db = SessionLocal()
    try:
        existing = db.query(PaperCitation).filter(
            PaperCitation.key == key).first()
        if existing:
            return key
        db.add(PaperCitation(
            key=key, bibtex_md=bibtex, source=source,
            arxiv_id=p.get("arxiv_id", ""),
            semantic_scholar_id=p.get("semantic_scholar_id", ""),
            doi=p.get("doi", ""),
            title=p.get("title", ""), authors=p.get("authors", ""),
            year=p.get("year", ""), abstract_md=p.get("abstract", ""),
            relevance_md=""))
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
        q = (c.title or "") + " " + (c.summary_md or "")[:200]
        if not q.strip():
            continue
        results = search(q, limit=max_per_claim)
        for p in results:
            key = _bibtex_key(p.get("title", ""), p.get("year", ""),
                              p.get("authors", "") or "anon")
            if key in already_keys:
                continue
            upsert_citation(p, source="arxiv" if p.get("arxiv_id")
                            else "semantic_scholar")
            already_keys.add(key)
            paper.file_decision(
                source="lit", kind="cite_paper",
                title=f"Cite '{p.get('title','')[:80]}'? ({p.get('year','')})",
                body_md=(f"**Relevance to claim:** {c.title}\n\n"
                         f"**Authors:** {p.get('authors','')}\n\n"
                         f"**Abstract:** {p.get('abstract','')[:600]}…"),
                default_action="approve",
                priority=10,
                linked_claim_id=c.id,
                linked_citation_key=key)
            filed += 1
    return filed
