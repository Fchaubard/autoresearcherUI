#!/usr/bin/env python3
"""Read-only Codex usage audit, scoped to one exact workspace.

Uses per-response token_usage_record events, never cumulative token_count events.
No prompts, tool arguments, credentials or response text enter the report.
Dollar figures are estimates at 2026-10-29 standard GPT-6 Astra rates, assuming
the default 30-minute cache-write lifetime. Other models remain unpriced.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime
import json
from pathlib import Path


TOKEN_FIELDS = (
    "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
    "output_tokens", "reasoning_output_tokens", "total_tokens",
)


def astra_cost(usage: dict) -> dict:
    """Partition input charges; reasoning is already included in output."""
    read = usage.get("cached_input_tokens", 0)
    write = usage.get("cache_write_input_tokens", 0)
    uncached = usage["input_tokens"] - read - write
    if min(read, write, uncached, usage["output_tokens"]) < 0:
        raise ValueError("Invalid token accounting; refusing a misleading estimate")
    long = usage["input_tokens"] > 272_000
    costs = {
        "cached_input": read * (2 if long else 1) / 1e6,
        "cache_write_30m": write * (25 if long else 12.5) / 1e6,
        "uncached_input": uncached * (20 if long else 10) / 1e6,
        "output_including_reasoning": usage["output_tokens"] * (75 if long else 50) / 1e6,
    }
    return dict(costs, total=sum(costs.values()))


def audit(paths, cwd: str, start: str | None = None, end: str | None = None) -> dict:
    start_at = datetime.fromisoformat(start.replace("Z", "+00:00")) if start else None
    end_at = datetime.fromisoformat(end.replace("Z", "+00:00")) if end else None
    groups = defaultdict(lambda: {"responses": 0, "tokens": Counter(), "cost": Counter(),
                                   "max_request_input": 0, "long_requests": 0})
    seen = set()
    sessions = []
    duplicates = malformed = 0
    for path in sorted(map(Path, paths)):
        model = effort = None
        selected = False
        first = last = None
        count = 0
        with path.open() as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    malformed += 1
                    continue  # A live log may end in an incomplete record.
                payload = event.get("payload", {})
                kind = event.get("type")
                if kind == "session_meta":
                    selected = payload.get("cwd") == cwd
                    if not selected:
                        break
                if not selected:
                    continue
                if kind == "turn_context":
                    model, effort = payload.get("model"), payload.get("effort")
                if kind != "token_usage_record":
                    continue
                at = datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00"))
                if (start_at and at < start_at) or (end_at and at >= end_at):
                    continue
                response_id = payload.get("response_id")
                if response_id and response_id in seen:
                    duplicates += 1
                    continue
                if response_id:
                    seen.add(response_id)
                usage = payload["usage"]
                group = groups[(model, effort)]
                group["responses"] += 1
                group["tokens"].update({key: usage.get(key, 0) for key in TOKEN_FIELDS})
                group["max_request_input"] = max(group["max_request_input"], usage["input_tokens"])
                group["long_requests"] += usage["input_tokens"] > 272_000
                if model == "gpt-6-astra":
                    group["cost"].update(astra_cost(usage))
                first = min(first, at) if first else at
                last = max(last, at) if last else at
                count += 1
        if count:
            sessions.append({"file": path.name, "responses": count,
                             "first": first.isoformat(), "last": last.isoformat()})
    results = []
    for (model, effort), group in sorted(groups.items(), key=lambda item: str(item[0])):
        tokens = dict(group["tokens"])
        results.append({"model": model, "effort": effort, "responses": group["responses"],
                        "tokens": tokens, "max_request_input": group["max_request_input"],
                        "long_requests": group["long_requests"],
                        "cache_hit_fraction": tokens["cached_input_tokens"] / tokens["input_tokens"]
                        if tokens["input_tokens"] else 0,
                        "estimated_usd": dict(group["cost"]) if group["cost"] else None})
    return {"workspace": cwd, "start_inclusive": start, "end_exclusive": end,
            "pricing_date": "2026-10-29", "pricing_source":
            "https://developers.openai.com/api/docs/models/gpt-6-astra",
            "assumptions": ["Standard service tier; 30-minute cache writes",
                            "Reasoning tokens are included in output, not charged twice",
                            "No invoice, discounts, regional uplifts or other API roles included",
                            "Models follow the most recent recorded turn_context",
                            "Non-Astra models are counted but unpriced"],
            "duplicate_responses_skipped": duplicates, "malformed_lines_skipped": malformed,
            "sessions": sessions, "models": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions", type=Path, default=Path.home() / ".codex/sessions")
    parser.add_argument("--cwd", required=True, help="Exact session workspace to include")
    parser.add_argument("--start", help="Inclusive ISO8601 timestamp with timezone")
    parser.add_argument("--end", help="Exclusive ISO8601 timestamp with timezone")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = audit(args.sessions.rglob("*.jsonl"), args.cwd, args.start, args.end)
    encoded = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded)
    else:
        print(encoded, end="")


if __name__ == "__main__":
    main()
