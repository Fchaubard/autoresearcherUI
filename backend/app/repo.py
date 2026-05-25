"""Parsing the experiment repo's contract files (doc 05 §5.3).

The agent owns ideas.md / results.tsv; the orchestrator only reads them. This
module turns ideas.md idea-blocks into structured records.
"""
from __future__ import annotations

import json
import re

# the six status emojis program.md defines -> normalized enum
STATUS_EMOJI = {
    "⚪": "not_implemented", "🔵": "implemented", "🟡": "running",
    "🔴": "failed", "🟢": "success", "🟣": "unclear",
}
STATUS_NAMES = ("not_implemented", "implemented", "running",
                "failed", "success", "unclear")


def parse_ideas_md(text: str) -> list[dict]:
    """Parse ideas.md into idea dicts. Blocks are delimited by lines that are
    just '#'; each block has '- key: `value`' lines (per program.md's template).
    Lenient: a malformed block is skipped, not fatal."""
    blocks: list[list[str]] = []
    cur: list[str] = []
    for line in text.splitlines():
        if line.strip() == "#":
            if cur:
                blocks.append(cur)
            cur = []
        else:
            cur.append(line)
    if cur:
        blocks.append(cur)

    ideas: list[dict] = []
    for blk in blocks:
        f: dict[str, str] = {}
        for line in blk:
            m = re.match(r"\s*-\s*([A-Za-z][\w .]*?)\s*:\s*(.*)", line)
            if not m:
                continue
            val = m.group(2).strip()
            if len(val) > 1 and val[0] == "`" and val[-1] == "`":
                val = val[1:-1]
            f[m.group(1).strip().lower()] = val
        if not f.get("idea_id"):
            continue
        ideas.append({
            "idea_id": f["idea_id"],
            "description": f.get("description", ""),
            "why": f.get("why", ""),
            "ev": _num(f.get("ev improvement") or f.get("ev") or "0"),
            "status": _status(f.get("status", "")),
            "hpps": _json(f.get("hpps", "{}")),
        })
    return ideas


def _num(s: str) -> float:
    m = re.search(r"-?\d+\.?\d*", s or "")
    return float(m.group()) if m else 0.0


def _status(s: str) -> str:
    for emoji, name in STATUS_EMOJI.items():
        if emoji in (s or ""):
            return name
    low = (s or "").lower()
    for name in STATUS_NAMES:
        if name in low or name.replace("_", " ") in low:
            return name
    return "not_implemented"


def _json(s: str) -> dict:
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}
