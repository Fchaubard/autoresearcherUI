"""The orchestrator — the autonomous research loop (doc 05).

It bootstraps a project, runs the baseline, then schedules the remaining ideas
highest-EV first, keeping up to n_slots runs in flight ("never idle a GPU").
Each run is a real subprocess of the experiment repo's train.py, which logs
metrics back through the arui SDK. Results are parsed, ideas + journal updated,
and events streamed to the dashboard.

The agent (FakeAgent or RealAgent) is injected, so this exact loop is what the
e2e integration test exercises — hardware-free, with the FakeAgent.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import random
import re
import sys

from .agent import FakeAgent
from .bus import bus
from .config import DATA_DIR, PORT, ROOT
from .db import SessionLocal
from .models import Event, Gpu, Idea, JournalEntry, Project, Run

_rng = random.Random()
RUN_TIMEOUT = 600  # seconds — generous; the example project runs in <1s


def _iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _read(path: str, name: str) -> str:
    try:
        with open(os.path.join(path, name)) as f:
            return f.read()
    except OSError:
        return ""


class Orchestrator:
    def __init__(self, project_dir: str, agent=None, n_slots: int = 3,
                 metric_key: str = "val_mse", direction: str = "minimize",
                 name: str | None = None):
        self.project_dir = os.path.abspath(project_dir)
        self.agent = agent or FakeAgent(self.project_dir)
        self.n_slots = n_slots
        self.metric_key = metric_key
        self.direction = direction          # minimize | maximize
        self.name = name or os.path.basename(self.project_dir.rstrip("/"))
        self.project_id = "proj-" + self.name
        self.baseline: float | None = None
        self.running = False

    # ── event / journal helpers ────────────────────────────────────────────
    def _event(self, db, type_, sev, actor, msg, run_id="", idea_id=""):
        e = Event(id=f"ev-{_rng.randrange(16**8):08x}", type=type_,
                  severity=sev, actor=actor, message=msg, run_id=run_id,
                  idea_id=idea_id, created_at=_iso())
        db.add(e)
        db.commit()
        bus.publish("events", "event", e.dict())

    def _journal(self, db, title, body):
        db.add(JournalEntry(id=f"jr-{_rng.randrange(16**8):08x}",
                            date=dt.date.today().isoformat(), title=title,
                            body=body, created_at=_iso()))
        db.commit()

    def _improvement(self, headline: float) -> float:
        """Signed improvement vs. baseline, positive = better, direction-aware."""
        if self.baseline is None:
            return 0.0
        return (self.baseline - headline) if self.direction == "minimize" \
            else (headline - self.baseline)

    # ── bootstrap ──────────────────────────────────────────────────────────
    def bootstrap(self) -> list[dict]:
        ideas = self.agent.bootstrap()
        db = SessionLocal()
        proj = db.query(Project).filter(Project.id == self.project_id).first()
        if not proj:
            proj = Project(
                id=self.project_id, name=self.name,
                repo_path=self.project_dir,
                purpose=_read(self.project_dir, "program.md")[:600],
                validation_metric=self.metric_key,
                metric_direction=self.direction,
                status="bootstrapping", gpu_count=self.n_slots,
                created_at=_iso())
            db.add(proj)
        for g in range(self.n_slots):
            if not db.query(Gpu).filter(Gpu.index == g).first():
                db.add(Gpu(index=g, model="cpu-slot", total_vram_mb=0))
        for idea in ideas:
            iid = idea["idea_id"]
            if db.query(Idea).filter(Idea.idea_id == iid,
                                     Idea.project_id == self.project_id).first():
                continue
            db.add(Idea(id=f"idea-{iid}", project_id=self.project_id,
                        idea_id=iid, description=idea["description"],
                        why=idea["why"], ev=idea["ev"],
                        status="not_implemented", hpps=idea["hpps"],
                        source="seed", created_at=_iso()))
        db.commit()
        self._event(db, "idea_added", "info", "agent",
                    f"Bootstrapped {len(ideas)} ideas for {self.name}.")
        self._journal(db, "Bootstrap",
                      f"Created the {self.name} project and parsed "
                      f"{len(ideas)} idea(s) from ideas.md. Running the "
                      f"baseline first; every later run is compared to it.")
        db.close()
        return ideas

    # ── execute one experiment ─────────────────────────────────────────────
    async def _execute(self, idea_row_id: str, gpu_index: int) -> None:
        db = SessionLocal()
        idea = db.query(Idea).filter(Idea.id == idea_row_id).first()
        run_id = idea.idea_id
        is_baseline = run_id == "baseline"
        hpps = dict(idea.hpps or {})
        desc = idea.description          # capture before the session closes
        idea.status = "running"
        idea.started_at = _iso()
        db.add(Run(id=run_id, project_id=self.project_id, idea_id=idea.id,
                   run_name=run_id, status="running", is_baseline=is_baseline,
                   gpu_index=gpu_index, tmux_session=f"train-gpu{gpu_index}",
                   config=hpps, started_at=_iso(), created_at=_iso()))
        db.commit()
        self._event(db, "run_started", "info", "system",
                    f"Launched {run_id} on GPU {gpu_index}.",
                    run_id=run_id, idea_id=idea.id)
        bus.publish("events", "runs_changed", {})
        db.close()

        cfg = self.agent.implement({"idea_id": run_id, "hpps": hpps,
                                    "description": desc})
        headline, ok, log_tail = await self._launch(run_id, gpu_index, cfg)

        db = SessionLocal()
        idea = db.query(Idea).filter(Idea.id == idea_row_id).first()
        run = db.query(Run).filter(Run.id == run_id).first()
        run.ended_at = idea.ended_at = _iso()

        if not ok or headline is None:
            run.status = "crashed"
            idea.status = "failed"
            idea.results_vs_baseline = "run did not produce a metric"
            idea.analysis = self.agent.analyze(
                {"idea_id": run_id}, {"crashed": True})
            db.commit()
            self._event(db, "run_finished", "warning", "agent",
                        f"{run_id} crashed — no metric produced.",
                        run_id=run_id, idea_id=idea.id)
        else:
            run.headline_metric = headline
            if is_baseline:
                self.baseline = headline
                run.is_baseline = True
                run.baseline_delta = 0.0
                run.status = "kept"
                idea.status = "success"
                idea.results_vs_baseline = f"baseline {self.metric_key} = {headline:.4f}"
                idea.analysis = f"Baseline established at {headline:.4f}."
            else:
                imp = self._improvement(headline)
                run.baseline_delta = imp
                thresh = max(1e-4, abs(self.baseline or 1) * 0.02)
                if imp > thresh:
                    idea.status, run.status = "success", "kept"
                elif imp < -thresh:
                    idea.status, run.status = "failed", "discarded"
                else:
                    idea.status, run.status = "unclear", "discarded"
                idea.results_vs_baseline = (
                    f"{headline:.4f} {self.metric_key} vs "
                    f"{self.baseline:.4f} baseline "
                    f"({'+' if imp >= 0 else ''}{imp:.4f})")
                idea.analysis = self.agent.analyze(
                    {"idea_id": run_id},
                    {"improvement": imp, "metric": self.metric_key})
            db.commit()
            sev = "info"
            self._event(db, "run_finished", sev, "agent",
                        f"{run_id} finished: {run.status} — "
                        f"{idea.results_vs_baseline}",
                        run_id=run_id, idea_id=idea.id)
            if not is_baseline and idea.status == "success":
                self._event(db, "breakthrough", "info", "agent",
                            f"{run_id} beat the baseline.",
                            run_id=run_id, idea_id=idea.id)
        bus.publish("events", "runs_changed", {})
        db.close()

    async def _launch(self, run_id: str, gpu_index: int, cfg: dict):
        """Run the experiment repo's train.py as a subprocess. Returns
        (headline_metric|None, ok, log_tail)."""
        cfg_path = os.path.join(DATA_DIR, f"cfg_{run_id}.json")
        with open(cfg_path, "w") as f:
            json.dump(cfg, f)
        env = dict(os.environ)
        env["ARUI_INGEST_URL"] = f"http://127.0.0.1:{PORT}"
        env["ARUI_RUN_NAME"] = run_id
        env["ARUI_PROJECT"] = self.name
        env["ARUI_CONFIG"] = cfg_path
        env["ARUI_GPU"] = str(gpu_index)
        # ensure the experiment repo can `import arui` even if it is not
        # pip-installed (a real cloned experiment repo, or the test fixture)
        env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "train.py", cwd=self.project_dir, env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT)
            out, _ = await asyncio.wait_for(proc.communicate(),
                                            timeout=RUN_TIMEOUT)
            text = (out or b"").decode("utf-8", "ignore")
            m = re.search(rf"{re.escape(self.metric_key)}\s*[:=]\s*"
                          r"([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)", text)
            headline = float(m.group(1)) if m else None
            ok = proc.returncode == 0 and headline is not None
            return headline, ok, text[-800:]
        except (asyncio.TimeoutError, OSError) as e:
            return None, False, f"launch error: {e}"

    # ── the loop ───────────────────────────────────────────────────────────
    async def run(self) -> None:
        self.running = True
        self.bootstrap()
        db = SessionLocal()
        proj = db.query(Project).filter(Project.id == self.project_id).first()
        proj.status = "running"
        ideas = db.query(Idea).filter(
            Idea.project_id == self.project_id).all()
        baseline_id = next((i.id for i in ideas if i.idea_id == "baseline"),
                           None)
        rest = sorted([i for i in ideas if i.idea_id != "baseline"],
                      key=lambda i: -i.ev)
        rest_ids = [i.id for i in rest]
        db.commit()
        db.close()

        # 1) baseline alone — every later run is compared to it
        if baseline_id:
            await self._execute(baseline_id, 0)

        # 2) remaining ideas, highest-EV first, up to n_slots concurrently
        gpu_pool = list(range(self.n_slots))
        lock = asyncio.Lock()

        async def worker(idea_id: str):
            async with lock:
                gpu = gpu_pool.pop()
            try:
                await self._execute(idea_id, gpu)
            finally:
                async with lock:
                    gpu_pool.append(gpu)

        sem = asyncio.Semaphore(self.n_slots)

        async def guarded(idea_id: str):
            async with sem:
                await worker(idea_id)

        await asyncio.gather(*(guarded(i) for i in rest_ids))

        # 3) finalize
        db = SessionLocal()
        proj = db.query(Project).filter(Project.id == self.project_id).first()
        proj.status = "done"
        runs = db.query(Run).filter(Run.project_id == self.project_id).all()
        kept = [r for r in runs if r.status == "kept" and not r.is_baseline]
        best = None
        if kept:
            best = (min if self.direction == "minimize" else max)(
                kept, key=lambda r: r.headline_metric)
            proj.baseline_run_id = "baseline"
        proj.status = "done"
        db.commit()
        win = (f"Best run: {best.run_name} "
               f"({self.metric_key} {best.headline_metric:.4f}, "
               f"{'+' if best.baseline_delta >= 0 else ''}"
               f"{best.baseline_delta:.4f} vs baseline)."
               if best else "No idea beat the baseline.")
        self._journal(db, "Run complete",
                      f"Completed {len(runs)} experiment(s): "
                      f"{len(kept)} improved on the baseline. {win}")
        self._event(db, "run_finished", "info", "system",
                    f"Research loop complete — {win}")
        bus.publish("events", "runs_changed", {})
        db.close()
        self.running = False


# module-level handle so the API can start/inspect one orchestrator
_active: Orchestrator | None = None


def start(project_dir: str, **kw) -> Orchestrator:
    global _active
    _active = Orchestrator(project_dir, **kw)
    asyncio.create_task(_active.run())
    return _active


def active() -> Orchestrator | None:
    return _active
