"""In-process pub/sub for Server-Sent Events (doc 11 D1).

One-way realtime (metrics, events, gpus, chat) is delivered over SSE. This bus
fans a published payload out to every subscribed EventSource. Everything runs
on the single asyncio event loop, so a plain asyncio.Queue per subscriber is
all that is needed - no broker, no extra process.
"""
from __future__ import annotations

import asyncio
import json
import math
from collections import defaultdict
from typing import AsyncIterator


def _json_safe(obj):
    """Recursively replace non-finite floats (NaN / +Inf / -Inf) with None.

    ``json.dumps`` defaults to ``allow_nan=True``, which emits bare ``NaN`` /
    ``Infinity`` tokens. Those are valid Python-flavoured JSON but are rejected
    by the browser's ``JSON.parse`` (and the strict JSON spec), so an SSE frame
    carrying a NaN metric value throws ``SyntaxError: Unexpected token 'N'`` in
    the dashboard's EventSource handler — repeatedly, since every metric tick
    re-sends it. Sanitising to ``null`` keeps the stream valid; the chart code
    already treats missing points as gaps. Mirrors the NaN→None handling in
    ``metrics.series`` so the SSE and REST paths agree.
    """
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def _dumps(payload) -> str:
    """JSON-encode an SSE payload, guaranteeing spec-valid output.

    ``allow_nan=False`` is a belt-and-braces guard: if some non-float, non-dict,
    non-list container still smuggles in a NaN, we raise here (caught by the
    caller) rather than emit a frame the browser cannot parse.
    """
    return json.dumps(_json_safe(payload), allow_nan=False)


class Bus:
    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._seq = 0

    def publish(self, topic: str, type_: str, payload: dict) -> None:
        self._seq += 1
        msg = {"id": self._seq, "type": type_, "payload": payload}
        for q in list(self._subs.get(topic, ())):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass

    async def subscribe(self, topic: str) -> AsyncIterator[str]:
        """Yield SSE-formatted frames for a topic until the client disconnects.

        Keep-alives at 5 s instead of 20 s — cloudflared quick-tunnels and
        corporate proxies tend to buffer the first chunk for ~30 s if the
        body stays small, which makes early events invisible. A 5 s heartbeat
        keeps the stream above the proxy's buffering threshold so events
        propagate within ~1 s of being published.

        The frontend also runs a 6 s polling fallback (see app.js
        ``refreshDashboardLive``), so even if SSE fails entirely the
        dashboard stays fresh — this just makes SSE itself robust.
        """
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subs[topic].add(q)
        try:
            # a comment frame opens the stream immediately
            yield ": connected\n\n"
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=5)
                    yield (f"id: {msg['id']}\n"
                           f"event: {msg['type']}\n"
                           f"data: {_dumps(msg['payload'])}\n\n")
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"   # prevent idle proxy timeouts
        finally:
            self._subs[topic].discard(q)


bus = Bus()
