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
import sys
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
_orig_stdout = None         # set while a run is capturing console output
_orig_stderr = None


class _Tee:
    """Wrap a stream so every write is mirrored to a sink — used to capture
    the training script's console output for the dashboard's run logs."""

    def __init__(self, orig, sink):
        self._orig = orig
        self._sink = sink

    def write(self, s):
        try:
            self._orig.write(s)
        except Exception:
            pass
        try:
            self._sink(s)
        except Exception:
            pass
        return len(s) if s else 0

    def flush(self):
        try:
            self._orig.flush()
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(self._orig, name)


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
        self._logbuf: list = []
        self._loglock = threading.Lock()
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

    def add_log(self, s: str) -> None:
        """Buffer a chunk of console output to ship to the backend."""
        if not s:
            return
        with self._loglock:
            self._logbuf.append(s)
            if len(self._logbuf) > 6000:          # cap a runaway log
                self._logbuf = self._logbuf[-3000:]

    def _flush_logs(self) -> None:
        with self._loglock:
            if not self._logbuf:
                return
            text = "".join(self._logbuf)
            self._logbuf = []
        try:
            _post("/api/track/logs", {"run_id": self.id, "text": text})
        except Exception:
            pass

    def _worker(self) -> None:
        buf: list = []
        last = last_log = time.time()
        while (not self._stop.is_set() or not self._q.empty() or buf
               or self._logbuf):
            try:
                buf.append(self._q.get(timeout=_FLUSH_EVERY))
            except queue.Empty:
                pass
            if buf and (len(buf) >= _BATCH_MAX
                        or time.time() - last >= _FLUSH_EVERY):
                self._send(buf)
                buf, last = [], time.time()
            if time.time() - last_log >= 1.5:
                self._flush_logs()
                last_log = time.time()

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
        global _orig_stdout, _orig_stderr
        if _orig_stdout is not None:                # stop capturing console
            sys.stdout = _orig_stdout
        if _orig_stderr is not None:
            sys.stderr = _orig_stderr
        _orig_stdout = _orig_stderr = None
        self._flush_logs()
        self._stop.set()
        self._t.join(timeout=6)
        self._flush_logs()
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
    # capture the training script's console output for the dashboard's logs
    global _orig_stdout, _orig_stderr
    if _orig_stdout is None:
        _orig_stdout, _orig_stderr = sys.stdout, sys.stderr
        sys.stdout = _Tee(_orig_stdout, _active.add_log)
        sys.stderr = _Tee(_orig_stderr, _active.add_log)
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
