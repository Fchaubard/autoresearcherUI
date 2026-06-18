"""Render a Claude Code session transcript as scrollable TERMINAL text.

Why this exists
---------------
Claude Code 2.1.x renders its interactive UI as a FULLSCREEN (alt-screen) TUI:
it positions the cursor absolutely and repaints the whole screen every frame,
so the tmux pane (and therefore the rail xterm that mirrors it) only ever holds
ONE screen — there is no scrollback, you cannot scroll up to the start of the
conversation, and a live repaint wipes any selection. (Verified: forcing
CI=1 / TERM=dumb does not change this.) The research terminal only looks
scrollable because its log was produced by an OLDER, inline-rendering claude.

Claude Code does, however, write the full conversation to a JSONL transcript on
disk. This module turns that transcript into a stream of plain terminal text
(with a few ANSI colors) that we feed into the SAME xterm widget the research
terminal uses. The result: one long, scrollable, selectable, copyable terminal
that goes all the way back to the first message — identical UX for the research
and author agents.

Public API
----------
``render_text(session, after=None, limit=4000)`` -> dict
    {"text": "<ansi terminal text>", "cursor": "<entry id>", "file": <name>}
"""

from __future__ import annotations

import glob
import json
import os
import subprocess

CLAUDE_PROJECTS = os.path.expanduser("~/.claude/projects")

_MAX_TEXT = 4000           # clip a single assistant/user block
_MAX_TOOL_RESULT = 1200    # clip tool output

# ANSI (xterm renders these). \r\n because xterm wants CRLF.
_RESET = "\x1b[0m"
_USER = "\x1b[1;36m"       # bold cyan
_ASSIST = "\x1b[0m"        # default fg
_THINK = "\x1b[2;37m"      # dim grey
_TOOL = "\x1b[1;32m"       # bold green
_RESULT = "\x1b[2;37m"     # dim grey


# ── locating the transcript file ─────────────────────────────────────────
def _tmux_cwd(session: str) -> str | None:
    try:
        out = subprocess.run(
            ["tmux", "display-message", "-p", "-t", session,
             "#{pane_current_path}"],
            capture_output=True, text=True, timeout=4)
        return (out.stdout or "").strip() or None
    except Exception:                                       # noqa: BLE001
        return None


def _encode_cwd(cwd: str) -> str:
    return cwd.replace("/", "-").replace(".", "-")


def _newest_jsonl(d: str) -> str | None:
    files = glob.glob(os.path.join(d, "*.jsonl"))
    return max(files, key=os.path.getmtime) if files else None


def transcript_file(session: str) -> tuple[str | None, str | None]:
    """(jsonl_path, launch_cwd) for the newest transcript of `session`."""
    cwd = _tmux_cwd(session)
    if cwd:
        d = os.path.join(CLAUDE_PROJECTS, _encode_cwd(cwd))
        if os.path.isdir(d):
            f = _newest_jsonl(d)
            if f:
                return f, cwd
    # The research ('agent') session is paused in paper mode, so its tmux cwd is
    # gone. Derive its project dir from the AUTHOR's cwd (same project root, just
    # without the trailing '/latex').
    if session != "author":
        acwd = _tmux_cwd("author")
        if acwd:
            root = acwd[:-len("/latex")] if acwd.endswith("/latex") else acwd
            d = os.path.join(CLAUDE_PROJECTS, _encode_cwd(root))
            if os.path.isdir(d):
                f = _newest_jsonl(d)
                if f:
                    return f, root
    # Last-resort fallback: scan project dirs (restricted to this repo so a
    # stray throwaway `claude` session elsewhere can't be picked up). author ->
    # '-latex' suffix, research -> no suffix.
    try:
        dirs = [p for p in glob.glob(os.path.join(CLAUDE_PROJECTS, "*"))
                if os.path.isdir(p) and "autoresearcher" in os.path.basename(p).lower()]
    except Exception:                                       # noqa: BLE001
        dirs = []
    if not dirs:
        return None, cwd
    want_latex = (session == "author")
    pool = [d for d in dirs if d.endswith("-latex") == want_latex] or dirs
    best, best_mt = None, -1.0
    for d in pool:
        f = _newest_jsonl(d)
        if f and os.path.getmtime(f) > best_mt:
            best, best_mt = f, os.path.getmtime(f)
    return best, cwd


# ── parsing + rendering ──────────────────────────────────────────────────
def _clip(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[:n].rstrip() + " …"


def _crlf(s: str) -> str:
    # xterm needs CRLF; normalize any lone \n.
    return s.replace("\r\n", "\n").replace("\n", "\r\n")


def _tool_summary(b: dict) -> str:
    name = b.get("name") or "tool"
    inp = b.get("input") or {}
    try:
        if name == "Bash":
            cmd = inp.get("command", "")
            return "Bash " + _clip(cmd, 600)
        if name in ("Read", "Write", "Edit", "NotebookEdit"):
            return f"{name} {inp.get('file_path', '')}"
        if name in ("Grep", "Glob"):
            return f"{name} {inp.get('pattern', '')}"
        if name == "TodoWrite":
            return f"TodoWrite ({len(inp.get('todos') or [])} items)"
        if name == "Task":
            return f"Task {inp.get('description', '')}"
        return f"{name} " + _clip(json.dumps(inp, ensure_ascii=False), 300)
    except Exception:                                       # noqa: BLE001
        return name


def _tool_result_text(b: dict) -> str:
    c = b.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        out = []
        for x in c:
            if isinstance(x, dict):
                if x.get("type") == "text":
                    out.append(x.get("text", ""))
                elif x.get("type") == "image":
                    out.append("[image]")
        return "\n".join(out)
    return ""


def _entries(o: dict) -> list[tuple[str, str, str]]:
    """-> list of (entry_id, kind, rendered_text_block)."""
    typ = o.get("type")
    if typ not in ("user", "assistant") or o.get("isMeta"):
        return []
    msg = o.get("message") or {}
    role = msg.get("role") or typ
    uuid = o.get("uuid") or ""
    content = msg.get("content")
    out: list[tuple[str, str, str]] = []
    idx = 0

    def emit(kind: str, block: str):
        nonlocal idx
        out.append((f"{uuid}:{idx}", kind, block))
        idx += 1

    if isinstance(content, str):
        txt = _clip(content, _MAX_TEXT).strip()
        if txt:
            emit("user", f"\r\n{_USER}❯ {_crlf(txt)}{_RESET}\r\n")
        return out
    if isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text":
                txt = _clip(b.get("text", ""), _MAX_TEXT).strip()
                if txt:
                    emit("text", f"\r\n{_ASSIST}{_crlf(txt)}{_RESET}\r\n")
            elif bt == "thinking":
                txt = _clip(b.get("thinking", ""), _MAX_TEXT).strip()
                if txt:
                    emit("thinking", f"\r\n{_THINK}{_crlf(txt)}{_RESET}\r\n")
            elif bt == "tool_use":
                emit("tool", f"\r\n{_TOOL}⏺ {_crlf(_tool_summary(b))}{_RESET}\r\n")
            elif bt == "tool_result":
                txt = _clip(_tool_result_text(b), _MAX_TOOL_RESULT).strip()
                if txt:
                    body = "\r\n".join("  " + ln for ln in _crlf(txt).split("\r\n"))
                    emit("tool_result", f"{_RESULT}{body}{_RESET}\r\n")
    return out


def render_text(session: str, after: str | None = None,
                limit: int = 4000) -> dict:
    """Render the newest transcript for `session` as terminal text.

    `after` (an entry id ``uuid:idx``) returns only the text produced AFTER it,
    for cheap live appends. Otherwise the whole conversation (capped to the last
    `limit` blocks) is returned.
    """
    path, cwd = transcript_file(session)
    if not path or not os.path.isfile(path):
        return {"text": "", "cursor": after, "file": None, "cwd": cwd}

    entries: list[tuple[str, str, str]] = []
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
                entries.extend(_entries(o))
    except Exception:                                       # noqa: BLE001
        return {"text": "", "cursor": after, "file": os.path.basename(path),
                "cwd": cwd}

    if after:
        cut = None
        for i, (eid, _, _) in enumerate(entries):
            if eid == after:
                cut = i
                break
        if cut is not None:
            entries = entries[cut + 1:]
    elif len(entries) > limit:
        entries = entries[-limit:]

    cursor = entries[-1][0] if entries else after
    text = "".join(block for (_, _, block) in entries)
    return {"text": text, "cursor": cursor,
            "file": os.path.basename(path), "cwd": cwd}
