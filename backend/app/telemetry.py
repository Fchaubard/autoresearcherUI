"""Opt-out usage telemetry via raw PostHog HTTP capture.

Deliberately minimal: NO PostHog JS SDK, no autocapture, no session replay, no
cookies, no PII. We DO build person profiles now (so the standard PostHog
dashboards — DAU/WAU/retention — work), but a "person" is only ever a STABLE
RANDOM id: the browser mints an anonymous UUID once (localStorage) and reuses
it; there are no names, emails, IPs-as-identity, or any personal data attached.
Server-originated events carry no browser id and are sent with
``$process_person_profile: false`` so they never create phantom people and
never inflate the user counts.

Opt out with any of:
    ARUI_TELEMETRY_DISABLED=1   (or =true)
    DO_NOT_TRACK=1
    CI=true

Telemetry is fire-and-forget on a daemon thread and swallows every error, so
it can never block or break the app.
"""
from __future__ import annotations

import json
import os
import platform
import threading
import urllib.request
import uuid

# Public PostHog project token. Write-only, safe to ship in a public app.
_POSTHOG_TOKEN = "phc_uWpUipwK9xdKjZTvPEvqKCEjcGdBZaC5374LS8SKsMUy"
_POSTHOG_HOST = "https://us.i.posthog.com/i/v0/e/"
_PROJECT = "autoresearcherui"
_VERSION = "0.1.0"


def telemetry_disabled() -> bool:
    """True if any opt-out env var is set (CI also opts out automatically)."""
    def _on(name: str) -> bool:
        return (os.environ.get(name, "") or "").strip().lower() in ("1", "true")
    return _on("ARUI_TELEMETRY_DISABLED") or _on("DO_NOT_TRACK") or _on("CI")


def _server_distinct_id() -> str:
    """A stable, anonymous per-install id for SERVER-originated events, so they
    aren't a brand-new random id every time. Persisted under DATA_DIR. Best
    effort — falls back to a fixed label if the file can't be written."""
    try:
        from .config import DATA_DIR
        p = DATA_DIR / ".telemetry_id"
        if p.exists():
            v = (p.read_text(errors="ignore") or "").strip()
            if v:
                return v
        v = str(uuid.uuid4())
        try:
            p.write_text(v)
        except Exception:                                   # noqa: BLE001
            pass
        return v
    except Exception:                                       # noqa: BLE001
        return "arui-server"


def build_payload(event: str, properties: dict | None = None,
                  distinct_id: str | None = None) -> dict:
    """Assemble the PostHog capture payload (pure; used by tests).

    ``distinct_id`` is the browser's STABLE anonymous id when present. Person
    profiles are created ONLY for those browser events — server events (no id)
    are sent person-less so the DAU/WAU/retention counts reflect real visitors.
    """
    did = (distinct_id or "").strip()
    is_browser = bool(did)
    return {
        "api_key": _POSTHOG_TOKEN,
        "event": event,
        "distinct_id": did or _server_distinct_id(),
        "properties": {
            **(properties or {}),
            # Person profiles ON for browser visitors (DAU/WAU/retention),
            # OFF for server events so they never inflate the user counts.
            "$process_person_profile": is_browser,
            "project": _PROJECT,
            "version": _VERSION,
            "runtime": "python",
            "python_major": platform.python_version_tuple()[0],
            "platform": platform.system().lower(),
            "arch": platform.machine(),
        },
    }


def _send(event: str, properties: dict | None,
          distinct_id: str | None) -> None:
    try:
        payload = build_payload(event, properties, distinct_id)
        req = urllib.request.Request(
            _POSTHOG_HOST,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST")
        urllib.request.urlopen(req, timeout=4).read()
    except Exception:
        # Telemetry must never break (or even surface in) the app.
        pass


def capture(event: str, properties: dict | None = None,
            distinct_id: str | None = None) -> None:
    """Fire-and-forget an event. Non-blocking, never raises.

    ``distinct_id``: the browser's stable anonymous id (frontend supplies it);
    omit for server events. Only send BORING properties: an event name + coarse
    properties. Never pass paths, prompts, file contents, repo names,
    usernames, emails, hostnames, env vars, API keys, or stack traces.
    """
    if telemetry_disabled():
        return
    try:
        threading.Thread(target=_send,
                         args=(event, properties or {}, distinct_id),
                         daemon=True, name="telemetry").start()
    except Exception:
        pass
