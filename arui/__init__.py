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

__all__ = ["init", "log", "log_defaults", "finish", "log_artifact",
           "summary", "Run", "REQUIRED_DEFAULT_KEYS"]

# The keys that every training run MUST log so the dashboard's drawer
# "All plots" section is populated. The agent's setup prompt is required
# to call ``arui.log_defaults(...)`` (or log all of these by hand) at
# every step or eval. The backend audits that all of these were seen at
# run-finish — missing keys surface as an Event-severity-warning so the
# user sees clearly that a default metric wasn't logged.
REQUIRED_DEFAULT_KEYS = (
    "val_loss", "val_acc", "lr", "train_loss", "train_acc",
    "time_per_step", "samples_per_sec",
)

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
         config: dict | None = None, baseline: bool = False,
         **_: object) -> Run:
    """Register a run with the backend and return a Run handle.

    Pass ``baseline=True`` to mark this run as THE no-mitigation baseline
    anchor (e.g. the undefended/poisoned model, the un-tuned control). The
    dashboard's "improvement vs baseline" reads this run as the starting
    point, so mark the run that demonstrates the problem EXISTS — not a
    run that already solved it, and not a clean/ideal floor. Without an
    explicit mark, the dashboard falls back to the worst real run, which
    can be misleading."""
    global _active
    project = project or os.environ.get("ARUI_PROJECT", "default")
    name = name or os.environ.get("ARUI_RUN_NAME", f"run-{int(time.time())}")
    config = dict(config or {})
    if baseline:
        config["is_baseline"] = True
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


# Agent phase reporting (PR 1 of state-control rewrite, 2026-06-05).
#
# The autoresearcher agent calls ``arui.phase("planning")`` at every
# lifecycle transition. The backend stores the value in the
# ``orchestrator.phase`` Setting and emits a phase_changed Event. The
# dashboard pill reads this directly instead of inferring state from
# tmux scrollback (which was the source of every stale-status bug).
#
# Allowed phases — keep this list short and meaningful. If you find
# yourself wanting "kindof in planning but also reviewing", DON'T add
# a new phase; emit the dominant one and use the ``detail`` dict for
# the nuance.
PHASES = (
    "bootstrap",            # very first boot — scaffolding code, no runs yet
    "planning",             # popping next idea, drafting run configs
    "launching_runs",       # tmux send-keys'ing python train.py invocations
    "watching_runs",        # at least one run is live; agent is waiting
    "council_review",       # batch finished; LLM reviewers running
    "idle_waiting_direction",  # research_paused or operator-input needed
    "concluding",           # agent declared purpose answered, preparing summary
    "complete",             # council approved conclusion; paper-mode ready
    "error",                # agent itself crashed / unrecoverable
)


def phase(phase: str, detail: dict | None = None) -> None:
    """Report the agent's current lifecycle phase.

    Call this at every transition. The backend stores the value in the
    ``orchestrator.phase`` Setting, emits a ``phase_changed`` Event, and
    the dashboard pill reads it directly. This is the source of truth
    for what the agent is doing — far more reliable than scraping the
    tmux pane for keywords.

    No-op (silently logs) if the call fails — the agent's main loop
    must never crash because the dashboard is unreachable.

    Args:
        phase: One of ``PHASES``. Unknown phases are still POSTed (in
               case the backend understands new ones); a warning is
               printed to stderr.
        detail: Optional small dict of context (e.g. ``{"idea_id": ...,
                "n_runs": 3, "blocked_on": "council"}``). Keep it under
                ~1 KB — this is for human consumption in the modal.
    """
    if phase not in PHASES:
        print(f"[arui] warning: unknown phase {phase!r}; allowed: "
              + ",".join(PHASES), file=sys.stderr)
    body = {"phase": phase, "detail": detail or {}}
    try:
        _post("/api/phase", body)
    except Exception as e:                                  # noqa: BLE001
        # Phase reporting is best-effort. The agent's main loop must
        # not crash because the dashboard is unreachable.
        print(f"[arui] phase report failed: {e}", file=sys.stderr)


# Per-run stopwatch used by ``log_defaults`` to compute ``time_per_step``
# and ``samples_per_sec`` without the user having to thread a stopwatch
# through their training loop. Keyed by ``id(_active)`` so a fresh run
# resets it.
_step_clock: dict[int, float] = {}


def _optimizer_lr(optimizer) -> float | None:
    """Best-effort: extract a single learning-rate scalar from any
    PyTorch-style optimizer, a plain float, or ``None``. Returns the
    first param_group's lr (the common case — schedulers update it in
    place). Never raises."""
    if optimizer is None:
        return None
    if isinstance(optimizer, (int, float)):
        return float(optimizer)
    try:
        groups = getattr(optimizer, "param_groups", None)
        if groups:
            lr = groups[0].get("lr")
            if lr is not None:
                return float(lr)
    except Exception:
        pass
    # Last-ditch: an attribute called .lr on the optimizer itself.
    try:
        lr = getattr(optimizer, "lr", None)
        if lr is not None:
            return float(lr)
    except Exception:
        pass
    return None


def log_defaults(
    model=None,
    optimizer=None,
    step: int | None = None,
    batch_size: int | None = None,
    train_loss: float | None = None,
    val_loss: float | None = None,
    val_acc: float | None = None,
    train_acc: float | None = None,
    extra: dict | None = None,
) -> dict:
    """Log the dashboard's required default metrics in one call.

    Every training run MUST surface these keys (see
    ``REQUIRED_DEFAULT_KEYS``) so the run drawer's "All plots" section is
    populated and runs are comparable across experiments. Missing values
    are logged as ``None`` (NaN sentinel for the metric store) — never
    silently dropped — so the drawer shows the key with a clear gap
    rather than "(not logged)".

    Auto-computed:
        - ``lr`` is pulled from ``optimizer.param_groups[0]['lr']`` if not
          provided directly.
        - ``time_per_step`` is the wall-clock seconds since the previous
          ``log_defaults`` call for this run.
        - ``samples_per_sec`` = ``batch_size / time_per_step`` when both
          are known.

    Returns the dict that was logged, for tests + debugging.
    """
    if _active is None:
        raise RuntimeError(
            "arui.init() must be called before arui.log_defaults()")
    now = time.time()
    last = _step_clock.get(id(_active))
    time_per_step = (now - last) if last is not None else None
    _step_clock[id(_active)] = now

    lr = _optimizer_lr(optimizer)
    samples_per_sec = None
    if batch_size and time_per_step and time_per_step > 0:
        try:
            samples_per_sec = float(batch_size) / float(time_per_step)
        except Exception:
            samples_per_sec = None

    # Build the payload with EVERY required key. Missing scalars become
    # NaN so the metric store records a point — the drawer can then show
    # the key as present-but-no-data rather than absent entirely.
    def _or_nan(v):
        return float(v) if v is not None else float("nan")

    payload: dict = {
        "val_loss":        _or_nan(val_loss),
        "val_acc":         _or_nan(val_acc),
        "lr":              _or_nan(lr),
        "train_loss":      _or_nan(train_loss),
        "train_acc":       _or_nan(train_acc),
        "time_per_step":   _or_nan(time_per_step),
        "samples_per_sec": _or_nan(samples_per_sec),
    }
    if extra:
        # User overrides win — same-keyed entries replace the defaults.
        for k, v in extra.items():
            try:
                payload[str(k)] = float(v) if v is not None else float("nan")
            except (TypeError, ValueError):
                # Skip non-numeric extras silently; the SDK only logs scalars.
                continue
    _active.log(payload, step=step)
    return payload


def log_artifact(name: str, path: str) -> None:
    if _active is not None:
        _active.log_artifact(name, path)


def finish() -> None:
    if _active is not None:
        _active.finish()
