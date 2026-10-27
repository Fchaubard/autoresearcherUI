"""Discover and stop this project's real workloads, including untracked jobs.

Ownership requires a cwd inside the configured workspace and a recognized
training/queue command, or a registered run's exact tmux session. Shells and
other projects are never selected just because their names contain 'train'.
Signals target individual PID/start-time identities, never a shared process
group. Queue supervisors are frozen and stopped before workers are inspected.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import signal
import subprocess
import time

from . import tmux_safe


@dataclass(frozen=True)
class Process:
    pid: int
    ppid: int
    started: str
    cwd: Path
    argv: tuple[str, ...]


@dataclass
class Workload:
    label: str
    processes: list[Process]
    queue: bool = False
    session: str = ""


def _process(pid: int) -> Process | None:
    try:
        path = Path("/proc") / str(pid)
        # comm can contain spaces and parentheses; fields after its final ')'
        # start at state (field 3); starttime is field 22.
        stat = (path / "stat").read_text().rsplit(")", 1)[1].split()
        if stat[0] in {"Z", "X"} or path.stat().st_uid != os.geteuid():
            return None
        argv = tuple(s.decode(errors="replace") for s in
                     (path / "cmdline").read_bytes().split(b"\0") if s)
        return Process(pid, int(stat[1]), stat[19],
                       (path / "cwd").resolve(strict=True), argv)
    except (OSError, ValueError, IndexError):
        return None


def snapshot() -> dict[int, Process]:
    if not Path("/proc").is_dir():
        raise RuntimeError("Process verification requires Linux /proc")
    return {p.pid: p for entry in Path("/proc").iterdir()
            if entry.name.isdecimal() and (p := _process(int(entry.name)))}


def panes() -> list[tuple[str, int, Path]]:
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-a", "-F",
             "#{session_name}\t#{pane_pid}\t#{pane_current_path}"],
            capture_output=True, text=True, timeout=5)
    except FileNotFoundError:
        return []
    if result.returncode:
        error = result.stderr or ""
        if "no server running" in error or "No such file" in error:
            return []
        raise RuntimeError("Cannot inspect tmux sessions: " + error.strip()[:200])
    result_panes = []
    for line in (result.stdout or "").splitlines():
        name, pid, cwd = line.split("\t", 2)
        result_panes.append((name, int(pid), Path(cwd).resolve()))
    return result_panes


def _descendants(pid: int, processes: dict[int, Process]) -> set[int]:
    result = {pid}
    while True:
        children = {p.pid for p in processes.values() if p.ppid in result}
        if children <= result:
            return result
        result |= children


def _kind(process: Process) -> str:
    """Classify the executable/script, not arbitrary strings in arguments."""
    if not process.argv:
        return ""
    exe = Path(process.argv[0]).name
    script = exe
    if re.match(r"python(?:\d+(?:\.\d+)*)?$", exe):
        script = ""
        for arg in process.argv[1:]:
            if arg == "-c":
                return ""  # inline analysis/user code is not a registered job
            if not arg.startswith("-"):
                script = Path(arg).name
                break
    elif exe in {"bash", "sh", "zsh", "fish", "tmux", "tee"}:
        return ""
    if re.search(r"(?:^|[_.-])(?:queue|dispatcher|dispatch)(?:[_.-]|$)", script):
        return "queue"
    if re.search(r"(?:^|[_.-])(?:train|training|trainer|torchrun|accelerate|experiment|sweep|eval|evaluate)(?:[_.-]|$)", script):
        return "worker"
    return ""


def _interactive_shell(process: Process | None) -> bool:
    if not process or not process.argv:
        return False
    if Path(process.argv[0]).name.lstrip("-") not in {"bash", "sh", "zsh", "fish", "dash"}:
        return False
    return not any(arg.startswith("-") and "c" in arg[1:] for arg in process.argv[1:])


def active_session_workloads(session: str) -> list[int]:
    """Read-only restart guard: training/queue PIDs beneath exact session.

    A model change kills this session, so even a child whose cwd changed or
    whose project differs must be protected. Work in independent tmux sessions
    is unaffected and does not delay a model change. Inspection errors raise
    rather than being misreported as an idle session.
    """
    if not tmux_safe.valid_name(session):
        raise ValueError("Invalid agent session")
    session_panes = [(name, pid, cwd) for name, pid, cwd in panes() if name == session]
    if not session_panes:
        return []
    processes = snapshot()
    descendants = set().union(*(_descendants(pid, processes) for _, pid, _ in session_panes))
    return sorted(p.pid for p in processes.values() if p.pid in descendants and _kind(p))


def discover(root: Path | None, registered: set[str] | None = None) -> list[Workload]:
    processes = snapshot()
    session_panes = panes()
    registered = registered or set()
    forbidden = {os.getpid()}
    # Never signal this backend or any shell/supervisor hosting it.
    current = processes.get(os.getpid())
    while current and current.ppid not in forbidden:
        forbidden.add(current.ppid)
        current = processes.get(current.ppid)
    for name, pid, _cwd in session_panes:
        if tmux_safe.is_protected(name):
            protected = _descendants(pid, processes)
            if name in {"agent", "author"} and root is not None:
                # A CLI can launch a training subprocess without tmux. Keep
                # the agent and its tool shell alive, but allow its explicitly
                # recognizable project workers to be stopped individually.
                worker_ids = set().union(*(
                    _descendants(p.pid, processes) for p in processes.values()
                    if p.pid in protected and p.pid != pid
                    and p.cwd.is_relative_to(root) and _kind(p)))
                protected -= worker_ids
            forbidden |= protected

    def owned(p: Process) -> bool:
        return root is not None and p.cwd.is_relative_to(root)

    workloads: list[Workload] = []
    taken: set[int] = set()
    grouped: dict[str, list[tuple[int, Path]]] = {}
    for name, pid, cwd in session_panes:
        grouped.setdefault(name, []).append((pid, cwd))
    for name, entries in grouped.items():
        if tmux_safe.is_protected(name) or not tmux_safe.valid_name(name):
            continue
        ids = set().union(*(_descendants(pid, processes) for pid, _ in entries))
        members = [processes[pid] for pid in ids if pid in processes and pid not in forbidden]
        recognized = [p for p in members if owned(p) and _kind(p)]
        # If a session contains another project's pane, do not kill the
        # session. Its individually owned workloads are handled below.
        all_owned = root is not None and all(cwd.is_relative_to(root) for _, cwd in entries)
        all_job_panes = all(
            not _interactive_shell(processes.get(pid)) and
            (any(p.pid in _descendants(pid, processes) for p in recognized)
             or (name in registered and len(entries) == 1))
            for pid, _ in entries)
        if not all_owned or not all_job_panes or not (recognized or name in registered):
            continue
        queue = any(_kind(p) == "queue" for p in recognized)
        workloads.append(Workload(name, members, queue=queue, session=name))
        taken |= ids
    for p in sorted(processes.values(), key=lambda item: item.pid):
        kind = _kind(p)
        if p.pid in taken or p.pid in forbidden or not owned(p) or not kind:
            continue
        ids = _descendants(p.pid, processes) - forbidden - taken
        members = [processes[pid] for pid in ids if pid in processes]
        workloads.append(Workload(f"pid:{p.pid}", members, queue=kind == "queue"))
        taken |= ids
    return workloads


def _alive(p: Process) -> bool:
    try:
        stat = (Path("/proc") / str(p.pid) / "stat").read_text().rsplit(")", 1)[1].split()
        return stat[0] not in {"Z", "X"} and stat[19] == p.started
    except (OSError, ValueError, IndexError):
        return False


def _signal(p: Process, sig: int, errors: list[str]) -> None:
    if not _alive(p):
        return
    try:
        if hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"):
            fd = os.pidfd_open(p.pid)
            try:
                # Opening first and then checking the birth identity closes
                # the PID reuse race between inspection and signaling.
                if _alive(p):
                    signal.pidfd_send_signal(fd, sig)
            finally:
                os.close(fd)
        else:
            os.kill(p.pid, sig)
    except ProcessLookupError:
        pass
    except OSError as exc:
        errors.append(f"Cannot signal process {p.pid}: {exc}")


def _stop(workloads: list[Workload], errors: list[str], grace: float) -> list[str]:
    processes = {p.pid: p for work in workloads for p in work.processes}
    # Freeze every supervisor/tree before termination, preventing a queue
    # from treating its worker's exit as permission to launch the next job.
    for p in sorted(processes.values(), key=lambda item: item.pid):
        _signal(p, signal.SIGSTOP, errors)
    # Reap children born in the brief discovery-to-freeze interval, including
    # descendants whose cwd changed away from the project.
    if workloads:
        fresh = snapshot()
        for work in workloads:
            ids = set().union(*(_descendants(p.pid, fresh) for p in work.processes
                                if p.pid in fresh and fresh[p.pid].started == p.started))
            added = [fresh[pid] for pid in ids if pid in fresh and pid not in processes]
            work.processes.extend(added)
            for p in added:
                processes[p.pid] = p
                _signal(p, signal.SIGSTOP, errors)
    for work in workloads:
        if work.session:
            ok, message = tmux_safe.kill_session(work.session)
            if not ok and tmux_safe.is_alive(work.session):
                errors.append(message)
    queue_pids = {p.pid for work in workloads if work.queue for p in work.processes
                  if _kind(p) != "worker"}
    for p in processes.values():
        # A dispatcher's TERM handler may retry the job. It must never get
        # another turn after the operator interrupts the queue.
        _signal(p, signal.SIGKILL if p.pid in queue_pids else signal.SIGTERM, errors)
        _signal(p, signal.SIGCONT, errors)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and any(_alive(p) for p in processes.values()):
        time.sleep(0.05)
    for p in processes.values():
        _signal(p, signal.SIGKILL, errors)
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and any(_alive(p) for p in processes.values()):
        time.sleep(0.05)
    stopped = []
    for work in workloads:
        if any(_alive(p) for p in work.processes) or (work.session and tmux_safe.is_alive(work.session)):
            errors.append(f"Workload still running: {work.label}")
        else:
            stopped.append(work.label)
    return stopped


def stop_project(root: Path | None, registered: set[str] | None = None,
                 *, grace: float = 2.0) -> dict:
    errors: list[str] = []
    queues: list[str] = []
    workers: list[str] = []
    remaining: list[str] = []
    try:
        found = discover(root, registered)
        queue_work = [w for w in found if w.queue]
        queue_processes = {p.pid: p for w in queue_work for p in w.processes}
        child_workers = []
        for p in queue_processes.values():
            if _kind(p) != "worker":
                continue
            ancestor = queue_processes.get(p.ppid)
            nested = False
            while ancestor:
                nested |= _kind(ancestor) == "worker"
                ancestor = queue_processes.get(ancestor.ppid)
            if not nested:
                child_workers.append(p)
        queues = _stop(queue_work, errors, grace)
        workers = [f"pid:{p.pid}" for p in child_workers if not _alive(p)]
        # Discover again after queues are stopped: workers can live in their
        # own tmux sessions, not under the dispatcher's process tree.
        workers.extend(_stop([w for w in discover(root, registered) if not w.queue], errors, grace))
        remaining = [w.label for w in discover(root, registered)]
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        errors.append(f"Could not verify project shutdown: {exc}")
    return {"killed": queues + workers, "queues_stopped": queues,
            "workers_stopped": workers, "remaining": remaining,
            "errors": list(dict.fromkeys(errors))}
