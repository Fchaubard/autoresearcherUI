"""Background monitor: telemetry, run reconciliation, log capture, idea sync.

One daemon thread, every few seconds:
  1. polls nvidia-smi -> Gpu table; polls CPU/RAM/disk -> cached system stats;
  2. pipes each run's tmux session to a persistent log file (so logs survive
     the session dying);
  3. reconciles run status — a dead 'running' run becomes crashed;
  4. syncs the agent's ideas.md into the idea queue and copies each idea's
     description onto its run;
  5. nudges the agent when GPUs sit idle.

Everything is best-effort: a missing nvidia-smi or tmux just skips that step.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import shlex
import subprocess
import threading
import time

from . import kill_criteria, metrics
from .bus import bus
from .config import DATA_DIR, WORKSPACE_DIR
from .db import SessionLocal
from .models import Event, Gpu, Idea, Project, Run, Setting

# Default policy if the user didn't enter anything.
_DEFAULT_KILL_CRITERIA = "1 hour"

_STARTED = False
_LOCK = threading.Lock()
_POLL_SEC = 6
_RUN_GRACE_SEC = 12 * 60
_PENDING_WORDS = ("pending", "todo", "queued", "planned", "next", "on deck")
_DONE_IN_STATUS = ("done", "complete", "bad", "kept", "crash", "skip",
                   "discard", "fail")
_IDEA_HEADERS = ("tier", "idea", "queue", "todo", "plan", "next", "candidate",
                 "ablation", "experiment", "backlog", "to try", "to-do",
                 "sweep")
_NUDGE_COOLDOWN = 18 * 60
_IDLE_SUSTAIN = 4 * 60
_INFRA = {"arui", "cf", "agent"}
_RUN_LOGS = DATA_DIR / "run_logs"

_last_nudge = 0.0
_idle_since = 0.0
_piped: set[str] = set()
_started_at = time.time()
_system: dict = {}


def _iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _epoch(iso_str: str | None):
    try:
        d = dt.datetime.fromisoformat(iso_str) if iso_str else None
    except Exception:
        return None
    if d is None:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d.timestamp()


def _safe_name(name: str) -> bool:
    return bool(re.match(r"^[A-Za-z0-9_.\-=]+$", name or ""))


def start() -> None:
    """Start the once-per-process monitor thread."""
    global _STARTED
    with _LOCK:
        if _STARTED:
            return
        _STARTED = True
    threading.Thread(target=_loop, daemon=True, name="monitor").start()
    print("[monitor] started — telemetry + reconcile + log capture", flush=True)


def _loop() -> None:
    while True:
        sessions = _tmux_sessions()
        # Proactively wire pipe-pane (raw stream + persistent log) for any
        # newly-discovered tmux session. Idempotent — _piped tracks the
        # ones we've already piped, so this is O(new) per tick. Without
        # this, the Sessions tab's xterm would only get bytes after the
        # user POSTs /api/sessions/<name>/attach — too laggy for a tab
        # they JUST clicked. Now the file is being filled the whole
        # time and the xterm streams from byte 0.
        try:
            _ensure_log_pipes(sessions)
        except Exception as e:                       # noqa: BLE001
            print(f"[monitor] _ensure_log_pipes error: {e}", flush=True)
        # Self-heal the infra agent terminals (author/agent). sweep_enable_all
        # skips these on purpose, so when their pipe-pane drops nothing brings
        # the live xterm back — the "terminal frozen" bug. ensure_piped is a
        # no-op when the pipe is healthy; it only re-enables a dropped one.
        try:
            from . import pane_stream as _ps
            for _s in ("author", "agent"):
                if _ps.ensure_piped(_s):
                    print(f"[monitor] re-enabled dropped pipe-pane for {_s}",
                          flush=True)
        except Exception as e:                       # noqa: BLE001
            print(f"[monitor] pipe self-heal error: {e}", flush=True)
        for step in (_poll_gpus, _poll_system):
            try:
                step()
            except Exception as e:                   # noqa: BLE001
                print(f"[monitor] {step.__name__} error: {e}", flush=True)
        changed = False
        try:
            changed = _reconcile_runs(sessions)
        except Exception as e:                       # noqa: BLE001
            print(f"[monitor] reconcile error: {e}", flush=True)
        try:
            if _apply_kill_criteria(sessions):
                changed = True
        except Exception as e:                       # noqa: BLE001
            print(f"[monitor] kill_criteria error: {e}", flush=True)
        for step in (_sync_idea_queue, _enrich_runs, _nudge_idle_gpus):
            try:
                step()
            except Exception as e:                   # noqa: BLE001
                print(f"[monitor] {step.__name__} error: {e}", flush=True)
        # Watchdog tick (PR 4 of state-control rewrite, 2026-06-05).
        # Runs every script against every RUNNING run; fires Events and
        # pages the agent via tmux send-keys when something breaks.
        # Per-run de-dup is handled inside runner.
        try:
            from . import watchdog as wd
            wd.tick()
        except Exception as e:                       # noqa: BLE001
            print(f"[monitor] watchdog tick error: {e}", flush=True)
        # Supervisor tick (PI lifecycle watchdog). Keeps the research
        # unblocked at the PHASE level — re-triggers an orphaned/stalled
        # council review so the agent never waits forever on a verdict
        # that will never come, and keeps lifecycle status fresh for the
        # feed + emails. Local + fast; no LLM. Best-effort.
        try:
            from . import supervisor
            supervisor.tick()
        except Exception as e:                       # noqa: BLE001
            print(f"[monitor] supervisor tick error: {e}", flush=True)
        if changed:
            bus.publish("events", "runs_changed", {})
        time.sleep(_POLL_SEC)


def message_agent(text: str) -> None:
    """Type a line into the live agent's tmux session."""
    subprocess.run(["tmux", "send-keys", "-t", "agent", "-l", text],
                   capture_output=True, timeout=10)
    subprocess.run(["tmux", "send-keys", "-t", "agent", "Enter"],
                   capture_output=True, timeout=10)


# ──────────────────────────── GPU telemetry ────────────────────────────────

def _poll_gpus() -> None:
    out = subprocess.run(
        ["nvidia-smi",
         "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,"
         "temperature.gpu",
         "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=15)
    if out.returncode != 0:
        return
    rows = []
    for line in out.stdout.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 6:
            continue
        try:
            rows.append({
                "index": int(parts[0]), "model": parts[1],
                "util": float(parts[2]), "used": float(parts[3]),
                "total": float(parts[4]), "temp": float(parts[5]),
            })
        except ValueError:
            continue
    if not rows:
        return
    db = SessionLocal()
    try:
        now = _iso()
        for r in rows:
            g = db.query(Gpu).filter(Gpu.index == r["index"]).first()
            if not g:
                g = Gpu(index=r["index"])
                db.add(g)
            g.model = r["model"]
            g.util_pct = r["util"]
            g.vram_used_mb = r["used"]
            g.total_vram_mb = int(r["total"])
            g.temp_c = r["temp"]
            g.sampled_at = now
        db.commit()
    finally:
        db.close()
    bus.publish("gpus", "gpus", {})


# ─────────────────────────── system telemetry ──────────────────────────────

def _poll_system() -> None:
    """Refresh the cached host stats so /api/system never shells out itself."""
    info: dict = {"updated_at": _iso(),
                  "uptime_sec": int(time.time() - _started_at)}
    try:
        info["loadavg"] = [round(x, 2) for x in os.getloadavg()]
    except Exception:
        info["loadavg"] = []
    try:
        import psutil
        info["cpu_percent"] = psutil.cpu_percent(interval=None)
        info["cpu_count"] = psutil.cpu_count()
        vm = psutil.virtual_memory()
        info["ram"] = {"used_gb": round(vm.used / 1e9, 1),
                       "total_gb": round(vm.total / 1e9, 1),
                       "percent": vm.percent}
        du = psutil.disk_usage(str(DATA_DIR))
        info["disk"] = {"used_gb": round(du.used / 1e9, 1),
                        "total_gb": round(du.total / 1e9, 1),
                        "free_gb": round(du.free / 1e9, 1),
                        "percent": du.percent}
    except Exception as e:                           # noqa: BLE001
        info["stats_note"] = f"psutil unavailable ({e})"
        try:
            out = subprocess.run(["df", "-B1", str(DATA_DIR)],
                                 capture_output=True, text=True, timeout=10)
            f = out.stdout.splitlines()[-1].split()
            total, used, free = int(f[1]), int(f[2]), int(f[3])
            info["disk"] = {"used_gb": round(used / 1e9, 1),
                            "total_gb": round(total / 1e9, 1),
                            "free_gb": round(free / 1e9, 1),
                            "percent": round(used / max(total, 1) * 100, 1)}
        except Exception:
            pass
    _system.clear()
    _system.update(info)


def system_stats() -> dict:
    """Cached host stats + live GPU rows. Cheap — never shells out."""
    db = SessionLocal()
    try:
        gpus = [g.dict() for g in db.query(Gpu).order_by(Gpu.index).all()]
    finally:
        db.close()
    out = dict(_system)
    out["gpus"] = gpus
    return out


# ───────────────────────────── log capture ─────────────────────────────────

def _ensure_log_pipes(sessions: set[str]) -> None:
    """Stream each run's tmux pane to BOTH the per-session raw byte file
    (Sessions tab xterm streams from here via /api/agent/raw) AND a
    persistent ``run_logs/<id>.log`` file (so finished runs are still
    inspectable via /api/runs/<id>/logs).

    We delegate to ``pane_stream.enable(session, mirror_to=...)`` which
    issues a single ``tmux pipe-pane`` mapped through ``tee`` so the
    byte stream lands in both files. That way, the moment the
    orchestrator / research agent / paper runner / + new button spawns
    a tmux session, the user can open the Sessions tab and see live
    bytes with NO /attach round-trip delay.
    """
    from . import pane_stream
    _RUN_LOGS.mkdir(parents=True, exist_ok=True)
    for s in sessions:
        if s in _INFRA or s in _piped or not _safe_name(s):
            continue
        logf = _RUN_LOGS / f"{s}.log"
        try:
            pane_stream.enable(s, mirror_to=str(logf))
            _piped.add(s)
        except Exception:                            # noqa: BLE001
            pass


def run_log(run_id: str, tail: int = 800) -> dict:
    """A run's captured logs. For a live run, the full tmux scrollback; for a
    finished run, the persisted pipe-pane file. Tailed so a huge log never
    floods the browser."""
    if not _safe_name(run_id):
        return {"text": "", "alive": False, "lines": 0}
    alive = subprocess.run(["tmux", "has-session", "-t", run_id],
                           capture_output=True).returncode == 0
    text = ""
    if alive:                                        # full scrollback
        try:
            out = subprocess.run(
                ["tmux", "capture-pane", "-t", run_id, "-p", "-S", "-"],
                capture_output=True, text=True, timeout=12)
            text = out.stdout
        except Exception:                            # noqa: BLE001
            text = ""
    if not text.strip():                             # finished run -> the file
        path = _RUN_LOGS / f"{run_id}.log"
        if path.exists():
            try:
                text = path.read_text(errors="ignore")
            except OSError:
                text = ""
    all_lines = text.splitlines()
    shown = all_lines[-tail:] if tail and len(all_lines) > tail else all_lines
    return {"text": "\n".join(shown), "alive": alive,
            "lines": len(all_lines), "shown": len(shown)}


# ─────────────────────────── run reconciliation ────────────────────────────

def _tmux_sessions() -> set[str]:
    try:
        out = subprocess.run(["tmux", "list-sessions", "-F",
                              "#{session_name}"],
                             capture_output=True, text=True, timeout=10)
        return {s.strip() for s in out.stdout.splitlines() if s.strip()}
    except Exception:                                # noqa: BLE001
        return set()


def _reconcile_runs(sessions: set[str]) -> bool:
    """Mark any 'running' run that has clearly died as crashed."""
    db = SessionLocal()
    changed = False
    try:
        running = db.query(Run).filter(Run.status == "running").all()
        if not running:
            return False
        now = time.time()
        for run in running:
            if run.id in sessions or (run.tmux_session
                                      and run.tmux_session in sessions):
                continue
            last = metrics.last_activity(run.id)
            if last and (now - last) < _RUN_GRACE_SEC:
                continue
            started = _epoch(run.started_at)
            if started and (now - started) < _RUN_GRACE_SEC:
                continue
            run.status = "crashed"
            run.ended_at = _iso()
            db.add(Event(
                id="ev-" + os.urandom(4).hex(), type="run_finished",
                severity="warning", actor="system",
                message=f"{run.id} interrupted — no live session or metric "
                        f"activity",
                run_id=run.id, created_at=_iso()))
            changed = True
            # ask the council to review this crashed run too — failures often
            # carry the most signal about what to try next.
            try:
                from . import council
                council.review_async(run.id)
            except Exception as e:                      # noqa: BLE001
                print(f"[monitor] council review_async failed: {e}", flush=True)
        if changed:
            db.commit()
    finally:
        db.close()
    return changed


# ─────────────────────────── kill criteria ───────────────────────────────


def _kill_run_session(run_id: str, tmux_session: str) -> None:
    """Kill the run's tmux session(s) — best-effort, both the run id and the
    tmux_session column (which may differ for legacy rows)."""
    for s in {run_id, tmux_session}:
        if not s or not _safe_name(s) or s in _INFRA:
            continue
        try:
            subprocess.run(["tmux", "kill-session", "-t", s],
                           capture_output=True, timeout=10)
        except Exception:                              # noqa: BLE001
            pass


def _onboarding_kill_text() -> str:
    """Return the user's kill-criteria string from the onboarding row, or
    the default if nothing was set."""
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(Setting.key == "onboarding").first()
        cfg = dict(row.value) if row and isinstance(row.value, dict) else {}
    finally:
        db.close()
    text = (cfg.get("kill_criteria") or "").strip()
    return text or _DEFAULT_KILL_CRITERIA


def _apply_kill_criteria(sessions: set[str]) -> bool:
    """Parse the user's kill criteria and apply it to every running run.

    Returns True if any run was killed (so the caller can publish a
    runs_changed event).
    """
    text = _onboarding_kill_text()
    rules = kill_criteria.parse(text)
    if not rules:
        return False
    db = SessionLocal()
    changed = False
    try:
        running = db.query(Run).filter(Run.status == "running").all()
        if not running:
            return False
        now = time.time()
        for run in running:
            # Only consider runs that actually have a live tmux session —
            # if it's already dead, _reconcile_runs handles it.
            alive = (run.id in sessions
                     or (run.tmux_session and run.tmux_session in sessions))
            if not alive:
                continue
            try:
                m = metrics.query(run.id)
            except Exception:                          # noqa: BLE001
                m = {}
            try:
                fire, reason = kill_criteria.check_run(run, rules, m, now=now)
            except Exception as e:                     # noqa: BLE001
                print(f"[monitor] kill_criteria check error: {e}", flush=True)
                continue
            if not fire:
                continue
            _kill_run_session(run.id, run.tmux_session or "")
            run.status = "crashed"
            run.ended_at = _iso()
            db.add(Event(
                id="ev-" + os.urandom(4).hex(), type="run_killed",
                severity="warning", actor="system",
                message=f"{run.id} killed by user kill-criteria — {reason}",
                run_id=run.id, created_at=_iso()))
            changed = True
            try:
                from . import council
                council.review_async(run.id)
            except Exception as e:                     # noqa: BLE001
                print(f"[monitor] council review_async failed: {e}",
                      flush=True)
        if changed:
            db.commit()
    finally:
        db.close()
    return changed


# ───────────────────────────── idea parsing ────────────────────────────────

def _parse_ideas(cfg: dict) -> dict[str, tuple[str, bool]]:
    """Parse the agent's ideas.md -> {idea_id: (description, is_pending)}.
    Handles markdown tables (with a status column) and bullet lists under
    idea-ish headers. Deterministic, first occurrence wins."""
    name = (cfg.get("repo_name") or "").strip()
    if not name:
        return {}
    path = WORKSPACE_DIR / name / "ideas.md"
    if not path.exists():
        return {}
    out: dict[str, tuple[str, bool]] = {}
    header = ""
    table_is_ideas = False
    prev_pipe = False
    for ln in path.read_text(errors="ignore").splitlines():
        s = ln.strip()
        if not s:
            prev_pipe = False
            continue
        if s.startswith("#"):
            header = s.lstrip("# ").lower()
            prev_pipe = False
            continue
        if s.startswith("|"):                          # markdown table
            cells = [c.strip() for c in s.strip("|").split("|")]
            if not cells or all(set(c) <= set("-: ") for c in cells):
                prev_pipe = True                       # separator row
                continue
            if not prev_pipe:                          # header row of a table
                table_is_ideas = (cells and cells[0].lower()
                                  in ("status", "state"))
                prev_pipe = True
                continue
            prev_pipe = True
            if not table_is_ideas or len(cells) < 2:
                continue
            st = cells[0].lower()
            idea_id = cells[1].strip("`* ")
            what = (cells[-1] if len(cells) > 2 else "").strip()
            pending = any(w in st for w in _PENDING_WORDS)
            if idea_id and len(idea_id) > 1 and idea_id not in out:
                out[idea_id] = (what or idea_id, pending)
            continue
        prev_pipe = False
        if s[0] in "-*•":                              # bullet-list idea
            if not any(w in header for w in _IDEA_HEADERS):
                continue
            done = any(m in s for m in ("[x]", "[X]", "✅", "~~"))
            body = s.lstrip("-*•[]xX ✓✅").strip()
            if len(body) < 4:
                continue
            m = re.match(r"`([^`]+)`", body)
            if m:
                idea_id = m.group(1).strip()
                what = body[m.end():].strip(" -—:`").strip()
            else:
                head = re.split(r"[—:]| - |\. ", body, 1)[0].strip()
                idea_id = re.sub(r"[^A-Za-z0-9]+", "_",
                                 head).strip("_")[:40].lower()
                what = body
            if idea_id and len(idea_id) > 1 and idea_id not in out:
                out[idea_id] = ((what or idea_id)[:240], not done)
    return out


def _onboarding_cfg(db) -> dict:
    row = db.query(Setting).filter(Setting.key == "onboarding").first()
    return dict(row.value) if row and isinstance(row.value, dict) else {}


def _sync_idea_queue() -> None:
    """Keep 'deck-*' Idea rows in step with ideas.md's pending entries."""
    db = SessionLocal()
    try:
        proj = db.query(Project).first()
        if not proj:
            return
        cfg = _onboarding_cfg(db)
        drow = db.query(Setting).filter(
            Setting.key == "dismissed_ideas").first()
        dismissed = set(drow.value) if drow and isinstance(drow.value, list) \
            else set()
        pending = {k: v[0] for k, v in _parse_ideas(cfg).items() if v[1]}
        run_ids = {r.id for r in db.query(Run).all()}
        deck = {i.idea_id: i for i in
                db.query(Idea).filter(Idea.id.like("deck-%")).all()}
        for idea_id, idea in deck.items():
            if (idea_id not in pending or idea_id in run_ids
                    or idea_id in dismissed):
                db.delete(idea)
        for idea_id, what in pending.items():
            if idea_id in run_ids or idea_id in deck or idea_id in dismissed:
                continue
            db.add(Idea(id="deck-" + idea_id, project_id=proj.id,
                        idea_id=idea_id, description=(what or idea_id)[:240],
                        status="not_implemented", source="agent",
                        created_at=_iso()))
        db.commit()
    finally:
        db.close()


def _enrich_runs() -> None:
    """Give every run's Idea a real description — from the run's own config
    (what / why — the reliable per-run channel) or a matching ideas.md entry —
    so the drawer shows research context, never an SDK stub."""
    db = SessionLocal()
    try:
        ideas = _parse_ideas(_onboarding_cfg(db))
        for run in db.query(Run).all():
            idea = db.query(Idea).filter(Idea.id == run.idea_id).first()
            if not idea:
                continue
            desc = (idea.description or "").strip()
            if desc and desc not in ("(logged via the arui SDK)",
                                     run.run_name, run.id):
                continue                              # already meaningful
            new = ""
            cfg = run.config if isinstance(run.config, dict) else {}
            for k in ("what", "description", "hypothesis", "idea", "desc"):
                if cfg.get(k):
                    new = str(cfg[k]).strip()
                    break
            if cfg.get("why"):
                new = (new + "  —  why: "
                       + str(cfg["why"]).strip()).strip(" —")
            if not new:
                entry = ideas.get(run.id) or ideas.get(run.run_name or "")
                if entry:
                    new = entry[0]
            if new != desc:
                idea.description = new
        db.commit()
    finally:
        db.close()


# ───────────────────────────── gpu nudge ───────────────────────────────────

def _nudge_idle_gpus() -> None:
    """If GPUs have sat idle for a while, tell the agent to fill them."""
    global _last_nudge, _idle_since
    db = SessionLocal()
    try:
        gpus = db.query(Gpu).all()
    finally:
        db.close()
    if not gpus:
        return
    idle = [g for g in gpus if (g.util_pct or 0) < 5
            and (g.vram_used_mb or 0) < 600]
    now = time.time()
    if len(idle) < 2:
        _idle_since = 0.0
        return
    if _idle_since == 0.0:
        _idle_since = now
        return
    if now - _idle_since < _IDLE_SUSTAIN or now - _last_nudge < _NUDGE_COOLDOWN:
        return
    _last_nudge = now
    idx = ", ".join(str(g.index) for g in idle)
    try:
        message_agent(
            f"{len(idle)} GPUs are sitting idle ({idx}). Launch experiments "
            f"on them right now — every idle GPU must be running one of your "
            f"experiments. Pull the next ideas from ideas.md and start each "
            f"in its own tmux session. Do not leave GPUs idle.")
        print(f"[monitor] nudged agent — {len(idle)} GPUs idle", flush=True)
    except Exception as e:                           # noqa: BLE001
        print(f"[monitor] nudge send error: {e}", flush=True)
