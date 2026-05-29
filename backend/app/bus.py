"""In-process pub/sub for Server-Sent Events (doc 11 D1).

One-way realtime (metrics, events, gpus, chat) is delivered over SSE. This bus
fans a published payload out to every subscribed EventSource. Everything runs
on the single asyncio event loop, so a plain asyncio.Queue per subscriber is
all that is needed - no broker, no extra process.
"""
from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import AsyncIterator


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
                           f"data: {json.dumps(msg['payload'])}\n\n")
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"   # prevent idle proxy timeouts
        finally:
            self._subs[topic].discard(q)


bus = Bus()
