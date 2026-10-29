import importlib.util
import json
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location(
    "audit_codex_usage", Path(__file__).resolve().parents[2] / "scripts/audit_codex_usage.py")
audit_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit_module)


def test_input_partition_and_reasoning_are_not_double_charged():
    usage = {"input_tokens": 10000, "cached_input_tokens": 7000,
             "cache_write_input_tokens": 2000, "output_tokens": 400,
             "reasoning_output_tokens": 300}
    assert audit_module.astra_cost(usage)["total"] == pytest.approx(.062)


@pytest.mark.parametrize("tokens,expected", [(272000, 2.72), (272001, 5.44002)])
def test_long_context_boundary(tokens, expected):
    assert audit_module.astra_cost({"input_tokens": tokens, "output_tokens": 0})["total"] == pytest.approx(expected)


def test_invalid_partition_is_not_silently_priced():
    with pytest.raises(ValueError):
        audit_module.astra_cost({"input_tokens": 2, "cached_input_tokens": 3, "output_tokens": 0})


def test_workspace_scope_deduplication_and_cumulative_events(tmp_path):
    usage = {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110}
    events = [
        {"type": "session_meta", "payload": {"cwd": "/research"}},
        {"type": "turn_context", "payload": {"model": "gpt-6-astra", "effort": "xhigh"}},
        {"type": "token_usage_record", "payload": {"response_id": "one", "usage": usage}},
        {"type": "token_usage_record", "payload": {"response_id": "one", "usage": usage}},
        {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": usage}}},
    ]
    path = tmp_path / "research.jsonl"
    path.write_text("\n".join(json.dumps(dict(e, timestamp="2026-10-29T12:00:00Z")) for e in events))
    other = tmp_path / "repair.jsonl"
    other.write_text(path.read_text().replace('"/research"', '"/repair"').replace('"one"', '"two"'))
    result = audit_module.audit([path, other], "/research")
    assert result["models"][0]["responses"] == 1
    assert result["models"][0]["estimated_usd"]["total"] == pytest.approx(.0015)
    assert result["duplicate_responses_skipped"] == 1
    assert not audit_module.audit([path], "/research", end="2026-10-29T12:00:00Z")["models"]
