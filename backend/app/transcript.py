"""Render a Claude Code *session transcript* into a clean, scrollable feed.

Why this exists
---------------
The rail "Research agent" / "Author agent" terminals stream the LIVE tmux
pane via ``/api/agent/raw``. But Claude Code runs as a full-screen
alt-screen TUI (``alternate_on=1, history_size=0``), so that pane has **no
scrollback** — you can only ever see the current screen, and the user
(rightly) complained the author terminal feels "stuck, can't scroll".

Claude Code, however, writes every turn of the conversation to a JSONL
transcript under ``~/.claude/projects/<encoded-cwd>/<session>.jsonl``. That
file is the real, complete history. This module parses the newest transcript
for a given tmux session and turns it into a list of small, readable entries
(user prompts, assistant text, thinking, tool calls + results) that the
frontend renders as one long, scrollable conversation — the same experience
for BOTH the research and author agents (one shared code path).

Public API
----------
``read_transcript(session, after=None, limit=400)`` -> dict
    {"entries": [...], "cursor": "<entry id>", "file": "<basename>",
     "cwd": "<launch cwd>"}

Each entry: {"id", "role", "kind", "text", "ts"} where
    role  in {"user", "assistant", "system"}
    kind  in {"user", "text", "thinking", "tool", "tool_result", "system"}
"""

from __future__ import annotations

import glob
import json
import os
import subprocess

CLAUDE_PROJECTS = os.path.expanduser("~/.claude/projects")

# Cap how much text any single entry carries. Tool output (Bash, Read) can be
# enormous; the transcript is for skimming what the agent is doing, not for
# re-reading megabytes of pdflatex logs.
_MAX_TEXT = 1600
_MAX_TOOL_RESULT = 700


# ── locating the transcript file ─────────────────────────────────────────
def _tmux_cwd(session: str) -> str | None:
    try:
        out = subprocess.run(
            ["tmux", "display-message", "-p", "-t", session,
             "#{pane_current_path}"],
            capture_output=True, text=True, timeout=4)
        p = (out.stdout or "").strip()
        return p or None
    except Exception:                                       # noqa: BLE001
        return None


def _encode_cwd(cwd: str) -> str:
    # Claude Code encodes the launch cwd into the project-dir name by
    # replacing every '/' and '.' with '-'. A leading '/' becomes a leading
    # '-'. e.g. /root/foo/latex -> -root-foo-latex
    return cwd.replace("/", "-").replace(".", "-")


def _newest_jsonl(d: str) -> str | None:
    files = glob.glob(os.path.join(d, "*.jsonl"))
    if not files:
        return None
    return max(files, key=os.path.getmtime)


def _session_hint(session: str) -> str:
    # The author agent is launched in the paper's ``latex`` dir, so its
    # project-dir name ends in '-latex'. The research agent runs in the
    # project root (no '-latex' suffix). Used only as a fallback when the
    # tmux cwd lookup fails.
    return "author" if session == "author" else "agent"


def transcript_file(session: str) -> tuple[str | None, str | None]:
    """Return (jsonl_path, launch_cwd) for the newest transcript of `session`."""
    cwd = _tmux_cwd(session)
    if cwd:
        d = os.path.join(CLAUDE_PROJECTS, _encode_cwd(cwd))
        if os.path.isdir(d):
            f = _newest_jsonl(d)
            if f:
                return f, cwd
    # Fallback: scan all project dirs, prefer ones matching the session's
    # role (author -> '-latex' suffix), pick the newest jsonl.
    try:
        dirs = [p for p in glob.glob(os.path.join(CLAUDE_PROJECTS, "*"))
                if os.path.isdir(p)]
    except Exception:                                       # noqa: BLE001
        dirs = []
    if not dirs:
        return None, cwd
    want_latex = (_session_hint(session) == "author")
    preferred = [d for d in dirs if d.endswith("-latex") == want_latex]
    pool = preferred or dirs
    best, best_mt = None, -1.0
    for d in pool:
        f = _newest_jsonl(d)
        if not f:
            continue
        mt = os.path.getmtime(f)
        if mt > best_mt:
            best, best_mt = f, mt
    return best, cwd


# ── parsing records into entries ─────────────────────────────────────────
def _clip(s: str, n: int) -> str:
    s = s or ""
    if len(s) <= n:
        return s
    return s[:n].rstrip() + " …"


def _tool_summary(block: dict) -> str:
    name = block.get("name") or "tool"
    inp = block.get("input") or {}
    try:
        if name == "Bash":
            cmd = inp.get("command", "")
            desc = inp.get("description")
            line = cmd if not desc else f"{desc}\n$ {cmd}"
            return f"Bash · {_clip(line, 400)}"
        if name in ("Read", "Write", "Edit", "NotebookEdit"):
            return f"{name} · {inp.get('file_path', '')}"
        if name in ("Grep", "Glob"):
            return f"{name} · {inp.get('pattern', '')}"
        if name == "TodoWrite":
            todos = inp.get("todos") or []
            return f"TodoWrite · {len(todos)} items"
        if name == "Task":
            return f"Task · {inp.get('description', '')}"
        # generic: short json
        return f"{name} · {_clip(json.dumps(inp, ensure_ascii=False), 300)}"
    except Exception:                                       # noqa: BLE001
        return name


def _tool_result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "text":
                    parts.append(b.get("text", ""))
                elif b.get("type") == "image":
                    parts.append("[image]")
        return "\n".join(parts)
    return ""


def _entries_from_record(o: dict) -> list[dict]:
    typ = o.get("type")
    if typ not in ("user", "assistant"):
        return []
    if o.get("isMeta"):
        return []
    msg = o.get("message") or {}
    role = msg.get("role") or typ
    uuid = o.get("uuid") or ""
    ts = o.get("timestamp") or ""
    content = msg.get("content")
    out: list[dict] = []
    idx = 0

    def push(kind: str, text: str):
        nonlocal idx
        text = (text or "").strip()
        if not text:
            return
        out.append({
            "id": f"{uuid}:{idx}",
            "role": role,
            "kind": kind,
            "text": text,
            "ts": ts,
        })
        idx += 1

    # A plain string content = a real user prompt (or a slash-command echo).
    if isinstance(content, str):
        push("user" if role == "user" else "text", _clip(content, _MAX_TEXT))
        return out

    if isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text":
                push("text", _clip(b.get("text", ""), _MAX_TEXT))
            elif bt == "thinking":
                push("thinking", _clip(b.get("thinking", ""), _MAX_TEXT))
            elif bt == "tool_use":
                push("tool", _tool_summary(b))
            elif bt == "tool_result":
                push("tool_result", _clip(_tool_result_text(b), _MAX_TOOL_RESULT))
    return out


def read_transcript(session: str, after: str | None = None,
                    limit: int = 400) -> dict:
    """Parse the newest Claude Code transcript for `session`.

    `after` is an entry id (``<uuid>:<idx>``); only entries that come AFTER it
    are returned (for cheap live tailing). Returns at most `limit` entries
    (the most recent ones).
    """
    path, cwd = transcript_file(session)
    if not path or not os.path.isfile(path):
        return {"entries": [], "cursor": after, "file": None, "cwd": cwd}

    entries: list[dict] = []
    try:
        with open(path, "r", errors="replace") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    o = json.loads(ln)
                except Exception:                           # noqa: BLE001
                    continue
                entries.extend(_entries_from_record(o))
    except Exception:                                       # noqa: BLE001
        return {"entries": [], "cursor": after, "file": os.path.basename(path),
                "cwd": cwd}

    # Tail after the cursor, if given and still present.
    if after:
        cut = None
        for i, e in enumerate(entries):
            if e["id"] == after:
                cut = i
                break
        if cut is not None:
            entries = entries[cut + 1:]

    cursor = entries[-1]["id"] if entries else after
    if len(entries) > limit:
        entries = entries[-limit:]
    return {"entries": entries, "cursor": cursor,
            "file": os.path.basename(path), "cwd": cwd}
