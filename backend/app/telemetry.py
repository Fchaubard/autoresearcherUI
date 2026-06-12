"""Anonymous, opt-out usage telemetry via raw PostHog HTTP capture.

Deliberately minimal: NO PostHog JS SDK, no autocapture, no session replay, no
cookies, no identify(), no stable user ID, no local id file, no person
profiles, no PII. Every event gets a fresh random distinct_id and sets
``$process_person_profile: false`` so PostHog never builds a person profile.
It only ever sends coarse, boring event counts (which command/feature ran,
success/failure, OS/runtime) so we can see what's used.

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


def build_payload(event: str, properties: dict | None = None) -> dict:
    """Assemble the PostHog capture payload (pure; used by tests)."""
    return {
        "api_key": _POSTHOG_TOKEN,
        "event": event,
        # No stable user ID: a fresh anonymous id for every single event.
        "distinct_id": str(uuid.uuid4()),
        "properties": {
            **(properties or {}),
            # CRITICAL: never create user/person profiles.
            "$process_person_profile": False,
            "project": _PROJECT,
            "version": _VERSION,
            "runtime": "python",
            "python_major": platform.python_version_tuple()[0],
            "platform": platform.system().lower(),
            "arch": platform.machine(),
        },
    }


def _send(event: str, properties: dict | None) -> None:
    try:
        payload = build_payload(event, properties)
        req = urllib.request.Request(
            _POSTHOG_HOST,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST")
        urllib.request.urlopen(req, timeout=4).read()
    except Exception:
        # Telemetry must never break (or even surface in) the app.
        pass


def capture(event: str, properties: dict | None = None) -> None:
    """Fire-and-forget an anonymous event. Non-blocking, never raises.

    Only send BORING things: an event name + coarse properties. Never pass
    paths, prompts, file contents, repo names, usernames, emails, hostnames,
    env vars, API keys, or stack traces. For failures send a coarse
    ``error_type`` (e.g. "config_missing"), not a raw exception.
    """
    if telemetry_disabled():
        return
    try:
        threading.Thread(target=_send, args=(event, properties or {}),
                         daemon=True, name="telemetry").start()
    except Exception:
        pass
