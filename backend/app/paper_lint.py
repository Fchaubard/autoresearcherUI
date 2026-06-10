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


def format_violations(violations: list[dict], limit: int = 40) -> str:
    """Human-readable summary for an error message / event."""
    if not violations:
        return "clean — no em-dashes or AI-slop found"
    lines = [f"{len(violations)} prose violation(s):"]
    for v in violations[:limit]:
        loc = f"{v.get('source','')}:{v.get('line','?')}"
        lines.append(f"  [{v['kind']}] {loc}  {v['rule']}  ::  {v['snippet']}")
    if len(violations) > limit:
        lines.append(f"  … and {len(violations) - limit} more")
    return "\n".join(lines)
