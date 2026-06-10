"""Prose linters for paper mode — two operator HARD rules.

  1. NO EM-DASHES anywhere. Francois hates them: the unicode em-dash (—), the
     LaTeX em-dash (---), a spaced double-hyphen used as one ( -- ), and an
     en-dash used between words (–). Numeric ranges ("3--5", "0.5–0.6") are
     fine and are NOT flagged.
  2. NO AI-slop antithesis ("it's not X, it's Y" / "not just X but Y") and a
     handful of classic LLM tells. "no one writes technical papers like that."

`lint_prose(text)` returns a list of violations (empty == clean). The paper
bundle gate calls `lint_paper_dir(...)` and BLOCKS bundling on any violation.
Kept deliberately conservative so it never flags legitimate scientific prose
like "ASR is not reduced, but remains high".
"""
from __future__ import annotations

import re
from pathlib import Path

# ── rule 1: em-dashes (banned everywhere) ──────────────────────────────────
_EMDASH = [
    (re.compile("—"),                                  "em-dash (—)"),
    (re.compile(r"---"),                                    "LaTeX em-dash (---)"),
    (re.compile(r"(?<=\s)--(?=\s)"),                        "spaced em-dash ( -- )"),
    (re.compile("(?<=[A-Za-z])\\s*–\\s*(?=[A-Za-z])"), "en-dash between words (–)"),
]

# ── rule 2: AI-slop antithesis + classic tells (conservative) ──────────────
_SLOP = [
    (re.compile(r"\bnot\s+(just|only|merely|simply)\b[^.?!]{1,80}?\bbut\b", re.I),
     "'not just/only X but Y' antithesis"),
    (re.compile(r"\bit['’]?s\s+not\s+(about\s+)?[^.?!,]{1,40},\s*it['’]?s\b", re.I),
     "'it's not X, it's Y' antithesis"),
    (re.compile(r"\bisn['’]?t\s+(just|only|merely)\b[^.?!]{1,80}?\bit['’]?s\b", re.I),
     "'isn't just X, it's Y' antithesis"),
    (re.compile(r"\b(delve|delving|tapestry|testament to|in the realm of|"
                r"navigate the landscape|it['’]?s worth noting|"
                r"it['’]?s important to note|paradigm shift)\b", re.I),
     "AI-slop tell"),
]


def lint_prose(text: str, *, source: str = "") -> list[dict]:
    """Return a list of {source,line,kind,rule,snippet} violations; [] if clean."""
    out: list[dict] = []
    for i, line in enumerate((text or "").splitlines(), 1):
        for rx, label in _EMDASH:
            if rx.search(line):
                out.append({"source": source, "line": i, "kind": "emdash",
                            "rule": label, "snippet": line.strip()[:140]})
        for rx, label in _SLOP:
            if rx.search(line):
                out.append({"source": source, "line": i, "kind": "slop",
                            "rule": label, "snippet": line.strip()[:140]})
    return out


def lint_paper_dir(folder, exts=(".tex", ".md")) -> list[dict]:
    """Lint every prose source file under `folder` (recursively). Used as a
    bundle GATE: a non-empty result blocks bundling."""
    out: list[dict] = []
    try:
        base = Path(folder)
        for p in sorted(base.rglob("*")):
            if p.is_file() and p.suffix.lower() in exts:
                try:
                    out.extend(lint_prose(p.read_text(errors="ignore"),
                                          source=str(p.relative_to(base))))
                except Exception:                          # noqa: BLE001
                    pass
    except Exception:                                      # noqa: BLE001
        pass
    return out


# ── bib lint: every \cite resolves; no placeholder / incomplete entries ────
_CITE_RX = re.compile(r"\\cite[a-zA-Z]*\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}")
_ENTRY_RX = re.compile(r"@(\w+)\s*\{\s*([^,\s]+)\s*,")
_SKIP_TYPES = {"comment", "string", "preamble", "set"}
_PLACEHOLDER_RX = re.compile(r"\b(TODO|TBD|FIXME|XXX|\?\?\?|placeholder|unknown|"
                             r"author\s*name|paper\s*title)\b", re.I)
_REQUIRED = ("title", "author", "year")


def _field(body: str, name: str):
    m = re.search(r"\b" + name + r"\s*=\s*[{\"]([^}\"]*)", body, re.I)
    return m.group(1).strip() if m else None


def lint_bib(folder) -> list[dict]:
    """Check that every \\cite resolves to a complete, non-placeholder .bib
    entry. Returns violations (empty == clean). Mirrors Widom's "make all
    citations complete and consistent; do not just paste random BibTeX". A
    non-empty result BLOCKS bundling."""
    folder = Path(folder)
    tex, bib = "", ""
    try:
        for p in folder.rglob("*.tex"):
            tex += "\n" + p.read_text(errors="ignore")
        for p in folder.rglob("*.bib"):
            bib += "\n" + p.read_text(errors="ignore")
    except Exception:                                      # noqa: BLE001
        pass
    # parse bib entries -> {key: body up to next @}
    entries: dict[str, str] = {}
    ms = list(_ENTRY_RX.finditer(bib))
    for i, m in enumerate(ms):
        if m.group(1).lower() in _SKIP_TYPES:
            continue
        start = m.end()
        end = ms[i + 1].start() if i + 1 < len(ms) else len(bib)
        entries[m.group(2)] = bib[start:end]
    cited: set[str] = set()
    for m in _CITE_RX.finditer(tex):
        for k in m.group(1).split(","):
            k = k.strip()
            if k:
                cited.add(k)
    out: list[dict] = []
    for k in sorted(cited):
        if k not in entries:
            out.append({"kind": "bib", "key": k,
                        "rule": "cited key has no .bib entry (will render [?])"})
    for key, body in entries.items():
        for f in _REQUIRED:
            val = _field(body, f)
            if not val:
                out.append({"kind": "bib", "key": key,
                            "rule": f"entry missing required field '{f}'"})
            elif _PLACEHOLDER_RX.search(val):
                out.append({"kind": "bib", "key": key,
                            "rule": f"placeholder '{f}': {val[:60]!r}"})
    return out


# ── asset lint: figures are TikZ-from-CSV (no matplotlib), no leftover TODO ─
_ASSET_BANNED = [
    (re.compile(r"\b(matplotlib|pyplot|savefig)\b", re.I),
     "raster plotting (must be TikZ/pgfplots from CSV)"),
    (re.compile(r"\\includegraphics(\[[^\]]*\])?\{[^}]*\.(png|jpe?g|gif)",
                re.I), "raster image include (use TikZ)"),
]


def lint_assets(folder) -> list[dict]:
    """At bundle time, figures/ + tables/ must be TikZ-from-CSV (no matplotlib
    or raster includes) and contain no leftover TODO placeholder. Empty ==
    clean."""
    folder = Path(folder)
    out: list[dict] = []
    for sub in ("figures", "tables"):
        d = folder / sub
        if not d.exists():
            continue
        for p in sorted(d.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in (".tex", ".tikz", ".py"):
                continue
            txt = p.read_text(errors="ignore")
            src = f"{sub}/{p.name}"
            for rx, label in _ASSET_BANNED:
                if rx.search(txt):
                    out.append({"kind": "assets", "source": src, "rule": label})
            if re.search(r"\bTODO\b", txt):
                out.append({"kind": "assets", "source": src,
                            "rule": "unfilled TODO placeholder"})
    return out


def format_violations(violations: list[dict], limit: int = 40) -> str:
    """Human-readable summary for an error message / event. Handles both prose
    (source/line/snippet) and bib (key) violations."""
    if not violations:
        return "clean: no em-dash, AI-slop, or citation issues found"
    lines = [f"{len(violations)} violation(s):"]
    for v in violations[:limit]:
        loc = (f"{v.get('source','')}:{v.get('line','?')}"
               if v.get("source") or v.get("line")
               else (f"[{v.get('key','')}]" if v.get("key") else ""))
        tail = f"  ::  {v['snippet']}" if v.get("snippet") else ""
        lines.append(f"  [{v.get('kind','?')}] {loc}  {v.get('rule','')}{tail}")
    if len(violations) > limit:
        lines.append(f"  ... and {len(violations) - limit} more")
    return "\n".join(lines)
