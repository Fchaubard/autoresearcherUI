"""Regression: SSE frames must be spec-valid JSON even with NaN/Inf metrics.

A metric point carrying a non-finite ``value`` used to be serialised by the
bus with the default ``json.dumps`` (``allow_nan=True``), which emits a bare
``NaN`` token. The browser's ``EventSource``/``JSON.parse`` rejects that with
``SyntaxError: Unexpected token 'N'`` on every metric tick, breaking the live
dashboard. The bus now sanitises non-finite floats to ``null``.
"""
import asyncio
import json
import math

import pytest

from backend.app import bus as bus_mod
from backend.app.bus import Bus, _json_safe, _dumps


def test_json_safe_replaces_non_finite():
    out = _json_safe(
        {
            "run_id": "r1",
            "points": [
                {"step": 1, "value": float("nan"), "wall_time": 0.5},
                {"step": 2, "value": float("inf"), "wall_time": 1.0},
                {"step": 3, "value": float("-inf"), "wall_time": 1.5},
                {"step": 4, "value": 0.42, "wall_time": 2.0},
            ],
        }
    )
    vals = [p["value"] for p in out["points"]]
    assert vals == [None, None, None, 0.42]
    # wall_time is finite and must survive untouched
    assert out["points"][0]["wall_time"] == 0.5


def test_dumps_is_strict_valid_json():
    payload = {"value": float("nan"), "nested": [float("inf"), 1.0]}
    frame = _dumps(payload)
    # The bare NaN/Infinity tokens must never appear in the wire format.
    assert "NaN" not in frame
    assert "Infinity" not in frame
    # And it must round-trip through a strict parser (parse_constant rejects
    # the Python-flavoured tokens, mirroring the browser's JSON.parse).
    parsed = json.loads(frame, parse_constant=lambda c: (_ for _ in ()).throw(
        ValueError(f"non-finite token: {c}")))
    assert parsed["value"] is None
    assert parsed["nested"][0] is None
    assert parsed["nested"][1] == 1.0


def test_subscribe_emits_valid_frame_for_nan_point():
    async def run():
        b = Bus()
        agen = b.subscribe("metrics")
        # first frame is the ": connected" comment opener
        opener = await agen.__anext__()
        assert opener.startswith(":")
        b.publish(
            "metrics",
            "metric",
            {"run_id": "r1", "points": [{"step": 1, "value": float("nan"),
                                         "wall_time": 0.5}]},
        )
        frame = await asyncio.wait_for(agen.__anext__(), timeout=2)
        # Extract the data: line and assert it parses strictly.
        data_line = next(ln[len("data: "):] for ln in frame.splitlines()
                         if ln.startswith("data: "))
        assert "NaN" not in data_line
        parsed = json.loads(data_line)
        assert parsed["points"][0]["value"] is None
        await agen.aclose()

    asyncio.run(run())


def test_module_level_bus_singleton_exists():
    # Guard against accidental removal of the shared instance other modules
    # import (e.g. api.bus.publish).
    assert isinstance(bus_mod.bus, Bus)
