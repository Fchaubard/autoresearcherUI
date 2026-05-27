"""PI agent — hourly oversight of the autonomous research agent.

The PI (Principal Investigator) wakes up every hour (configurable), reads
the current state of the project, and writes a short message to the
research agent's tmux session — exactly as if a real PI walked past and
said "hey, the queue's wrong / your runs are diverging / GPU 3 is idle /
please plot X".

What the PI looks at:
  - GPU saturation (any idle / underused?)
  - The most recent ~12 runs — anyone clearly plateaued or diverging?
    A run is "plateaued" if its loss curve hasn't decreased meaningfully
    over its last N steps. "Diverging" if loss is increasing.
  - The top of ideas.md vs what the agent is currently running — is the
    agent following the council's reranking?
  - The last hour of the agent's tmux output (so we don't blindly nag).

What the PI does:
  - Drafts 1-3 short messages and types them into the agent's tmux session
    (the same channel the user uses).
  - Persists a "concerns" summary into chat_message so it shows in the
    Summary feed as a researcher-voice bubble.
  - Emits a 'pi_intervention' event.

Model selection comes from Settings ('pi_agent_model'). Cadence comes from
Settings ('pi_cadence_minutes', default 60). Enable/disable via
'pi_agent_enabled'.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request

from . import council, metrics
from .bus import bus
from .config import DATA_DIR
from .db import SessionLocal
from .models import ChatMessage, Event, Gpu, Run, Setting

_STARTED = False
_LOCK = threading.Lock()
_LAST_RUN = 0.0


DEFAULTS = {
    "pi_agent_enabled": True,
    "pi_agent_model": "gemini-2.5-pro",   # provider chosen from model string
    "pi_cadence_minutes": 60,
}


def _settings() -> dict:
    out = dict(DEFAULTS)
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(Setting.key == "onboarding").first()
        if row and isinstance(row.value, dict):
            for k in DEFAULTS:
                if k in row.value and row.value[k] not in ("", None):
                    out[k] = row.value[k]
    finally:
        db.close()
    return out


def _iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


# ── what model handles the PI? routes by model-name prefix ─────────────
def _provider_for(model: str) -> str | None:
    m = (model or "").lower()
    if m.startswith("gemini"):
        return "gemini" if os.environ.get("GEMINI_API_KEY") else None
    if m.startswith("gpt") or m.startswith("o3") or m.startswith("o4"):
        return "openai" if os.environ.get("OPENAI_API_KEY") else None
    if m.startswith("claude"):
        return "claude" if os.environ.get("ANTHROPIC_API_KEY") else None
    return None


# ── context for the PI ─────────────────────────────────────────────────
def _agent_tail(n: int = 60) -> str:
    """Last few lines of the research agent's tmux output."""
    try:
        out = subprocess.run(
            ["tmux", "capture-pane", "-t", "agent", "-p", "-S", str(-n)],
            capture_output=True, text=True, timeout=8)
        return out.stdout.strip()
    except Exception:
        return ""


def _plateau_signal(run_id: str) -> dict | None:
    """If a run's headline-ish metric has stagnated or risen over its last
    window of points, flag it."""
    try:
        m = metrics.query(run_id, None) or {}
    except Exception:
        return None
    out = None
    for k, pts in m.items():
        if not pts or len(pts) < 12:
            continue
        ys = [p[1] for p in pts[-30:] if isinstance(p, (list, tuple))
              and len(p) >= 2 and p[1] is not None]
        if len(ys) < 12:
            continue
        first, last = ys[0], ys[-1]
        # crude plateau detection — last decile vs first decile of window
        d = (last - first)
        if d > 0 and abs(d) > 1e-6:
            out = {"key": k, "first": first, "last": last,
                   "trend": "diverging"}
            break
        # if max - min is tiny relative to value, plateau
        if abs(max(ys) - min(ys)) < max(1e-8, abs(last) * 0.005):
            out = {"key": k, "first": first, "last": last,
                   "trend": "plateaued"}
            break
    return out


def _build_context() -> dict:
    db = SessionLocal()
    try:
        gpus = [g.dict() for g in db.query(Gpu).order_by(Gpu.index).all()]
        runs = (db.query(Run).order_by(Run.created_at.desc()).limit(20)
                .all())
        run_info = []
        for r in runs:
            sig = _plateau_signal(r.id) if r.status == "running" else None
            run_info.append({
                "id": r.id, "name": r.run_name, "status": r.status,
                "metric": r.headline_metric,
                "baseline_delta": r.baseline_delta,
                "plateau_signal": sig,
            })
        # last hour of council events as a hint
        cutoff = (dt.datetime.now(dt.timezone.utc)
                  - dt.timedelta(hours=2)).isoformat()
        evs = (db.query(Event).filter(Event.created_at >= cutoff)
               .order_by(Event.created_at.desc()).limit(40).all())
        recent_events = [{"type": e.type, "msg": e.message,
                          "at": e.created_at} for e in evs]
    finally:
        db.close()

    # idle GPU summary
    idle = [g for g in gpus if (g.get("util_pct", 0) or 0) < 5
            and (g.get("vram_used_mb", 0) or 0) < 600]
    return {
        "now": _iso(),
        "gpus_total": len(gpus),
        "gpus_idle": len(idle),
        "gpus_idle_indices": [g["index"] for g in idle],
        "gpu_util_avg": (
            round(sum(g.get("util_pct", 0) or 0 for g in gpus)
                  / max(len(gpus), 1), 1)),
        "recent_runs": run_info,
        "recent_events": recent_events[:20],
        "agent_terminal_tail": _agent_tail(80),
    }


SYSTEM = """You are the Principal Investigator for an autonomous ML research
project. An LLM research agent runs experiments on this GPU cluster. Your
job, called once per hour, is to read what's happening and intervene if
something is wrong. You don't run experiments yourself — you nudge the
agent via short, actionable messages typed into its tmux session.

INTERVENE if you see any of:
  - GPUs sitting idle when there are pending ideas. Tell the agent to
    launch the top of ideas.md on every idle GPU NOW.
  - A run that is clearly plateaued or diverging. Tell the agent which
    run id to kill and why — wasted GPU time is the enemy.
  - The agent is stuck or looping (the terminal tail will look repetitive).
    Tell it to read the council's latest review and pick the top pending
    idea from ideas.md.
  - The agent is ignoring the council's reranking (running its own pick
    instead of the top pending row).

DO NOT INTERVENE if everything looks healthy. It is fine to return zero
messages — be sparing, every message interrupts the agent.

Return JSON ONLY, no markdown fence, matching this schema:
{
  "concerns": "<1-3 sentences summarising what you see. Use 'OK.' if all
   is well.>",
  "messages": [
    "<one-line message to type into the agent's tmux session>", ...
  ]
}
- 0-3 messages. Each must be actionable and concrete (mention the run_id,
  the GPU index, the idea id).
- 'concerns' is shown in the Summary feed as a PI bubble, so users see what
  the PI is watching even when no intervention is needed."""


def _call(model: str, system: str, user: str) -> str:
    """Route the call to the right adapter based on model name."""
    prov = _provider_for(model)
    if not prov:
        raise RuntimeError(f"no API key for {model}")
    if prov == "gemini":
        key = os.environ["GEMINI_API_KEY"]
        url = ("https://generativelanguage.googleapis.com/v1beta/"
               f"models/{model}:generateContent?key={key}")
        body = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"responseMimeType": "application/json",
                                 "temperature": 0.5},
        }
        data = council._post_json_retry(url, body, {})
        return data["candidates"][0]["content"]["parts"][0]["text"]
    if prov == "openai":
        key = os.environ["OPENAI_API_KEY"]
        body = {
            "model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "response_format": {"type": "json_object"},
        }
        if "gpt-5" in model or model.startswith("o"):
            body["reasoning_effort"] = "medium"
        data = council._post_json_retry(
            "https://api.openai.com/v1/chat/completions", body,
            {"Authorization": f"Bearer {key}"})
        return data["choices"][0]["message"]["content"]
    if prov == "claude":
        key = os.environ["ANTHROPIC_API_KEY"]
        body = {
            "model": model,
            "max_tokens": 1500,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        data = council._post_json_retry(
            "https://api.anthropic.com/v1/messages", body,
            {"x-api-key": key, "anthropic-version": "2023-06-01"})
        return data["content"][0]["text"]
    raise RuntimeError(f"unknown provider {prov}")


def _send_to_agent(text: str) -> bool:
    """Type a line into the agent's tmux session (same as user chat)."""
    try:
        subprocess.run(["tmux", "send-keys", "-t", "agent", "-l", text],
                       capture_output=True, timeout=8)
        subprocess.run(["tmux", "send-keys", "-t", "agent", "Enter"],
                       capture_output=True, timeout=8)
        return True
    except Exception:
        return False


def cycle(force: bool = False) -> dict | None:
    """Run one PI cycle. Returns the decision dict, or None if disabled /
    failed."""
    cfg = _settings()
    if not cfg.get("pi_agent_enabled", True) and not force:
        return None
    model = (cfg.get("pi_agent_model") or DEFAULTS["pi_agent_model"]).strip()
    if not _provider_for(model):
        print(f"[pi] no API key for {model}; skipping cycle", flush=True)
        return None
    ctx = _build_context()
    user = ("Here is the current state of the research project. Decide if "
            "the agent needs a nudge and return JSON per the schema.\n\n"
            + json.dumps(ctx, indent=2, default=str))
    try:
        text = _call(model, SYSTEM, user)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="ignore")[:300]
        except Exception:
            pass
        print(f"[pi] HTTP {e.code}: {body}", flush=True)
        return None
    except Exception as e:                              # noqa: BLE001
        print(f"[pi] call failed: {e}", flush=True)
        return None

    out = council._safe_parse(text)
    if not out:
        print(f"[pi] non-JSON; first 200: {text[:200]!r}", flush=True)
        return None
    concerns = (out.get("concerns") or "").strip()
    messages = [m for m in (out.get("messages") or []) if m]

    # send each message to the agent
    sent = 0
    for m in messages[:3]:
        if _send_to_agent(m):
            sent += 1
            time.sleep(0.5)

    # persist a chat bubble + event so it shows in the Summary feed
    db = SessionLocal()
    try:
        db.add(ChatMessage(
            id="cm-" + os.urandom(4).hex(),
            role="agent",
            content=("[PI · " + model + "]  " + concerns +
                     (("\n\nNudges:\n  • " + "\n  • ".join(messages))
                      if messages else "")),
            created_at=_iso()))
        db.add(Event(
            id="ev-" + os.urandom(4).hex(),
            type="pi_intervention", severity="info",
            actor="pi:" + model,
            message=(concerns or "PI checked in")[:280],
            created_at=_iso()))
        db.commit()
    finally:
        db.close()
    try:
        bus.publish("events", "runs_changed", {})
    except Exception:
        pass
    print(f"[pi] cycle: concerns={concerns!r} sent={sent}", flush=True)
    return {"concerns": concerns, "messages_sent": sent, "model": model}


# ── scheduler ──────────────────────────────────────────────────────────
def start() -> None:
    """Spawn the background PI scheduler thread. Once-per-process."""
    global _STARTED
    with _LOCK:
        if _STARTED:
            return
        _STARTED = True
    threading.Thread(target=_loop, daemon=True, name="pi").start()
    print("[pi] scheduler started", flush=True)


def _loop() -> None:
    global _LAST_RUN
    # First cycle 90s after startup so onboarding has time to land.
    time.sleep(90)
    while True:
        cfg = _settings()
        cadence_sec = max(60, int(cfg.get("pi_cadence_minutes", 60)) * 60)
        if cfg.get("pi_agent_enabled", True):
            try:
                cycle()
                _LAST_RUN = time.time()
            except Exception as e:                  # noqa: BLE001
                print(f"[pi] loop error: {e}", flush=True)
        # sleep in 10s slices so cadence changes are picked up quickly
        slept = 0
        while slept < cadence_sec:
            time.sleep(10)
            slept += 10
