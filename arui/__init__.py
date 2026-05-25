"""arui - the autoresearcherUI experiment-tracking SDK.

A tiny, dependency-free, wandb/mlop-compatible logger. The generated train.py
imports this instead of `wandb`. It batches metric points and POSTs them to the
autoresearcherUI backend over plain HTTP using only the standard library, so it
never blocks training and never adds a dependency.

Usage (drop-in for wandb):

    import arui
    run = arui.init(project="bs1learning", name="icl-cartridge-v2",
                    config={"lr": 1e-4, "n_pert": 100})
    for step in range(steps):
        arui.log({"val_fid": 1.23, "train_loss": 0.4}, step=step)
    arui.summary["best_val_fid"] = 0.99
    arui.finish()

Environment variables (injected by the orchestrator when it launches a run):
    ARUI_INGEST_URL   - backend base URL (default http://127.0.0.1:8000)
    ARUI_INGEST_TOKEN - bearer token for the ingest endpoints (optional)
    ARUI_RUN_NAME     - default run name if not passed to init()
"""
from __future__ import annotations

import atexit
import json
import os
import queue
import threading
import time
import urllib.request

__all__ = ["init", "log", "finish", "log_artifact", "summary", "Run"]

_BASE = os.environ.get("ARUI_INGEST_URL", "http://127.0.0.1:8000").rstrip("/")
_TOKEN = os.environ.get("ARUI_INGEST_TOKEN", "")
_FLUSH_EVERY = 0.5          # seconds
_BATCH_MAX = 200            # points

summary: dict = {}          # wandb-compatible summary dict
_active: "Run | None" = None


def _post(path: str, payload: dict) -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{_BASE}{path}", data=data, method="POST",
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {_TOKEN}"} if _TOKEN else {})},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read() or b"{}")


class Run:
    """A single tracked run. Logging is non-blocking: points go on a queue and a
    background thread flushes them in batches. Training is never slowed by I/O,
    and a briefly-unreachable backend cannot stall the run."""

    def __init__(self, project: str, name: str, config: dict, run_id: str):
        self.project = project
        self.name = name
        self.config = config
        self.id = run_id
        self._q: "queue.Queue" = queue.Queue()
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._worker, daemon=True)
        self._t.start()

    def log(self, metrics: dict, step: int | None = None) -> None:
        ts = time.time()
        for k, v in metrics.items():
            try:
                self._q.put_nowait({"key": str(k), "step": step,
                                    "value": float(v), "wall_time": ts})
            except Exception:
                pass

    def _worker(self) -> None:
        buf: list = []
        last = time.time()
        while not self._stop.is_set() or not self._q.empty() or buf:
            try:
                buf.append(self._q.get(timeout=_FLUSH_EVERY))
            except queue.Empty:
                pass
            if buf and (len(buf) >= _BATCH_MAX
                        or time.time() - last >= _FLUSH_EVERY):
                self._send(buf)
                buf, last = [], time.time()

    def _send(self, points: list) -> None:
        try:
            _post("/api/track/log", {"run_id": self.id, "points": points})
        except Exception:
            pass  # production: buffer to a write-ahead file and replay

    def log_artifact(self, name: str, path: str) -> None:
        try:
            _post("/api/track/artifact",
                  {"run_id": self.id, "name": name, "path": path})
        except Exception:
            pass

    def finish(self) -> None:
        self._stop.set()
        self._t.join(timeout=5)
        try:
            _post("/api/track/finish",
                  {"run_id": self.id, "summary": dict(summary)})
        except Exception:
            pass


def init(project: str | None = None, name: str | None = None,
         config: dict | None = None, **_: object) -> Run:
    """Register a run with the backend and return a Run handle."""
    global _active
    project = project or os.environ.get("ARUI_PROJECT", "default")
    name = name or os.environ.get("ARUI_RUN_NAME", f"run-{int(time.time())}")
    config = config or {}
    run_id = name
    try:
        resp = _post("/api/track/run",
                     {"project": project, "name": name, "config": config})
        run_id = resp.get("run_id", name)
    except Exception:
        pass
    _active = Run(project, name, config, run_id)
    atexit.register(lambda: _active and _active.finish())
    return _active


def log(metrics: dict, step: int | None = None) -> None:
    if _active is None:
        raise RuntimeError("arui.init() must be called before arui.log()")
    _active.log(metrics, step)


def log_artifact(name: str, path: str) -> None:
    if _active is not None:
        _active.log_artifact(name, path)


def finish() -> None:
    if _active is not None:
        _active.finish()
