"""Parsing of Claude Code session JSONL into the scrollable rail transcript.

The rail's live terminal mirrors Claude Code's alt-screen TUI, which has no
scrollback. backend/app/transcript.py instead parses the on-disk session JSONL
into clean entries (user / assistant text, thinking, tool calls + results) that
the frontend renders as one long scrollable feed. These tests pin the parsing
contract: which records become entries, how tool calls are summarized, that
text is clipped, and that the `after` cursor tails correctly.
"""
from __future__ import annotations

import json

from backend.app import transcript


def _write_jsonl(path, records):
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _records():
    return [
        # a meta record — must be ignored
        {"type": "ai-title", "uuid": "m1", "title": "x"},
        # real user prompt (string content)
        {"type": "user", "uuid": "u1", "timestamp": "t1",
         "message": {"role": "user", "content": "Launch runs on idle GPUs."}},
        # assistant: thinking + text + a tool_use, all in one record
        {"type": "assistant", "uuid": "a1", "timestamp": "t2",
         "message": {"role": "assistant", "content": [
             {"type": "thinking", "thinking": "Let me check the GPUs first."},
             {"type": "text", "text": "Launching three runs now."},
             {"type": "tool_use", "name": "Bash",
              "input": {"command": "python train.py --gpu 0",
                        "description": "start run"}},
         ]}},
        # tool_result comes back as a user record with a list content
        {"type": "user", "uuid": "u2", "timestamp": "t3",
         "message": {"role": "user", "content": [
             {"type": "tool_result",
              "content": [{"type": "text", "text": "run started ok"}]},
         ]}},
        # a meta assistant record (isMeta) — ignored
        {"type": "assistant", "uuid": "a2", "isMeta": True,
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "meta noise"}]}},
    ]


def test_parses_records_into_entries(tmp_path, monkeypatch):
    f = tmp_path / "sess.jsonl"
    _write_jsonl(f, _records())
    monkeypatch.setattr(transcript, "transcript_file",
                        lambda session: (str(f), "/root/proj"))

    out = transcript.read_transcript("agent")
    kinds = [(e["role"], e["kind"]) for e in out["entries"]]

    # meta records dropped; user prompt + 3 assistant blocks + 1 tool_result
    assert ("user", "user") in kinds
    assert ("assistant", "thinking") in kinds
    assert ("assistant", "text") in kinds
    assert ("assistant", "tool") in kinds
    assert ("user", "tool_result") in kinds
    assert all("meta noise" not in e["text"] for e in out["entries"])

    # Bash tool summarized with command + description, not raw json
    tool = next(e for e in out["entries"] if e["kind"] == "tool")
    assert "Bash" in tool["text"]
    assert "python train.py" in tool["text"]

    # every entry id is unique and stable (uuid:idx)
    ids = [e["id"] for e in out["entries"]]
    assert len(ids) == len(set(ids))
    assert out["cursor"] == ids[-1]


def test_after_cursor_tails(tmp_path, monkeypatch):
    f = tmp_path / "sess.jsonl"
    _write_jsonl(f, _records())
    monkeypatch.setattr(transcript, "transcript_file",
                        lambda session: (str(f), "/root/proj"))

    full = transcript.read_transcript("agent")
    mid = full["entries"][1]["id"]
    tail = transcript.read_transcript("agent", after=mid)
    tail_ids = [e["id"] for e in tail["entries"]]

    # nothing at or before the cursor is returned
    assert mid not in tail_ids
    assert full["entries"][0]["id"] not in tail_ids
    # the remaining (later) entries are present, in order
    assert tail_ids == [e["id"] for e in full["entries"][2:]]


def test_missing_file_is_safe(monkeypatch):
    monkeypatch.setattr(transcript, "transcript_file",
                        lambda session: (None, None))
    out = transcript.read_transcript("author")
    assert out["entries"] == []
    assert out["file"] is None


def test_long_text_is_clipped(tmp_path, monkeypatch):
    big = "x" * 5000
    f = tmp_path / "sess.jsonl"
    _write_jsonl(f, [
        {"type": "assistant", "uuid": "a1", "timestamp": "t",
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": big}]}},
    ])
    monkeypatch.setattr(transcript, "transcript_file",
                        lambda session: (str(f), "/root/proj"))
    out = transcript.read_transcript("agent")
    assert len(out["entries"]) == 1
    assert len(out["entries"][0]["text"]) < len(big)
    assert out["entries"][0]["text"].endswith("…")


def test_encode_cwd():
    assert (transcript._encode_cwd("/root/foo/latex")
            == "-root-foo-latex")
    assert (transcript._encode_cwd("/root/a.b/c")
            == "-root-a-b-c")
