"""Watchdog onboarding helper — asks the council whether the ship
defaults make sense for this project's research agenda and persists
the verdict to ``watchdog.config``.

Called once after onboarding completes (or via /api/watchdog/review),
NOT at every council strategic review. The watchdog config doesn't
change often; once the agent has signed off it's stable.

We use the same LLM stack the council uses (``council._call_reviewer``)
so the API key + provider plumbing is shared.
"""
from __future__ import annotations

import json
import os
from typing import Optional

from ..db import SessionLocal
from ..models import Event, Project, Setting
from . import config as wd_config


SYSTEM = """You are an ML-infrastructure engineer reviewing a list of
default "watchdog" monitoring scripts that will supervise the training
runs in a research project. Each script has tunable parameters with
sensible defaults that work for typical projects (~10 min eval cycles,
loss-based metrics). For projects with unusual characteristics — very
long eval phases, RL with reward not loss, non-standard metric keys,
heavy IO that pauses logging — the defaults may be wrong.

Your job: read the project's research purpose and validation metric,
then return a JSON dict whose KEYS are script names and whose VALUES
are partial overrides ({"enabled": false} OR {"params": {...}} OR
both). Only emit overrides you're confident the project needs; an
empty dict is fine if the defaults work as-is.

DO NOT redefine anything you're not changing. If you think the
defaults are right, return an empty dict {}. If you only want to
change one param of one script, return only that.

You must respond with JSON ONLY — no markdown fences, no prose.
"""


PROMPT_TEMPLATE = """Project purpose:
{purpose}

Validation metric: {metric} (direction: {direction})
Kill criteria the operator set: {kill}

Default watchdog scripts (read each script's describe text and decide
if its DEFAULT_PARAMS make sense for THIS project):

{scripts_block}

Respond with JSON like:
{{
  "no_metric_flow": {{"params": {{"timeout_sec": 7200}}}},
  "diverging": {{"enabled": false}}
}}

or {{}} if the defaults are all fine.
"""


def _scripts_block() -> str:
    lines = []
    for s in wd_config.list_scripts():
        lines.append(
            f"  • {s['name']}\n"
            f"      describe: {s['describe']}\n"
            f"      default_params: "
            f"{json.dumps(s['default_params'])}\n"
            f"      kills_run: {s['kills_run']}")
    return "\n".join(lines)


def review_with_council(*, force: bool = False) -> dict:
    """If the agent hasn't reviewed the watchdog config yet (or
    ``force=True``), ask the council and persist the verdict.

    Returns ``{"status": "skipped"|"applied", "overrides": {...}}``.
    Never raises into the caller.
    """
    db = SessionLocal()
    try:
        proj = db.query(Project).first()
        if not proj or not (proj.purpose or "").strip():
            return {"status": "skipped",
                    "reason": "no onboarded project yet"}
        # Was this already done?
        marker = (db.query(Setting)
                  .filter(Setting.key == "watchdog.reviewed_at").first())
        if marker and not force:
            return {"status": "skipped",
                    "reason": "already reviewed",
                    "reviewed_at": (marker.value or {}).get("at")}
        purpose = (proj.purpose or "").strip()
        metric = (proj.validation_metric or "?").strip()
        direction = (proj.metric_direction or "minimize").strip()
        kill_row = (db.query(Setting)
                    .filter(Setting.key == "kill_criteria").first())
        kill = ((kill_row.value or {}).get("criteria") or "(none)") \
            if kill_row else "(none)"
    finally:
        db.close()

    prompt = PROMPT_TEMPLATE.format(
        purpose=purpose[:4000],
        metric=metric,
        direction=direction,
        kill=kill[:600],
        scripts_block=_scripts_block(),
    )
    # Pick whichever reviewer is available (OpenAI / Gemini / Claude).
    # We piggy-back on council._call_reviewer because it already handles
    # key plumbing, retries, JSON-safe parsing.
    try:
        from .. import council
        reviewers = council._available_reviewers(council._settings())
    except Exception as e:                                  # noqa: BLE001
        print(f"[watchdog.onboarding] no reviewers available: {e}",
              flush=True)
        return {"status": "skipped", "reason": "no reviewers"}
    if not reviewers:
        return {"status": "skipped",
                "reason": "no council API keys configured"}

    out = None
    last_err = ""
    for r in reviewers[:2]:        # try up to two in fallback order
        try:
            cfg = council._settings()
            raw = council._call_reviewer(r, SYSTEM, prompt, cfg)
            if isinstance(raw, dict):
                out = raw
                break
        except Exception as e:                              # noqa: BLE001
            last_err = str(e)[:200]
            continue
    if not isinstance(out, dict):
        return {"status": "skipped",
                "reason": f"reviewer returned non-dict: {last_err}"}

    # The reviewer's response should be a dict of overrides. Validate +
    # sanitize before persisting. We never accept top-level entries that
    # aren't valid script names; unknown params under a known script are
    # silently dropped.
    valid_scripts = {s["name"] for s in wd_config.list_scripts()}
    clean: dict = {}
    for name, cust in (out or {}).items():
        if name not in valid_scripts or not isinstance(cust, dict):
            continue
        entry = {}
        if "enabled" in cust:
            entry["enabled"] = bool(cust["enabled"])
        if isinstance(cust.get("params"), dict):
            entry["params"] = {k: v for k, v in cust["params"].items()
                                if isinstance(k, str)}
        if entry:
            clean[name] = entry

    if clean:
        wd_config.set_config(clean, source="agent_authored")
    # Stamp the review even when the agent returned {} (defaults OK).
    from datetime import datetime, timezone
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc).isoformat()
        marker = (db.query(Setting)
                  .filter(Setting.key == "watchdog.reviewed_at").first())
        if marker is None:
            db.add(Setting(key="watchdog.reviewed_at",
                            value={"at": now, "overrides": clean}))
        else:
            marker.value = {"at": now, "overrides": clean}
        db.add(Event(
            id="ev-" + os.urandom(4).hex(),
            type="watchdog_reviewed",
            severity="info",
            actor="council:watchdog",
            message=(f"Council reviewed watchdog defaults; "
                     f"overrides={list(clean.keys()) or '(none)'}")[:280],
            created_at=now,
        ))
        db.commit()
    finally:
        db.close()
    return {"status": "applied", "overrides": clean}
