"""A REAL Gantt scheduler for paper-mode ablation runs.

Given the paper runs (each with an ``est_time_sec``, ``gpus_required`` and
``depends_on`` edges) and the number of GPUs available, compute a genuine
resource-constrained, dependency-aware schedule: when each run starts and
ends, and on which GPU. This is the data behind the Gantt chart, a real
finish-time schedule rather than an LLM-asserted ETA.

The schedule is a greedy list-schedule: at each step pick the dependency-ready
task that can start earliest (tie-break: longest job first / LPT), place it on
the GPU(s) that free earliest. Good enough to give the operator an honest
"this is ~N GPU-hours and finishes here" picture and a dependency-correct bar
chart.
"""
from __future__ import annotations


def _critical_path(placed: dict) -> list:
    """Longest dependency chain by finish time (the schedule's bottleneck)."""
    if not placed:
        return []
    end_id = max(placed, key=lambda i: placed[i]["end_sec"])
    chain = [end_id]
    cur = end_id
    while placed[cur]["depends_on"]:
        deps = [d for d in placed[cur]["depends_on"] if d in placed]
        if not deps:
            break
        cur = max(deps, key=lambda d: placed[d]["end_sec"])
        chain.append(cur)
    return list(reversed(chain))


def schedule(runs: list[dict], n_gpus: int, now_sec: float = 0.0) -> dict:
    """runs: [{id, name?, est_time_sec, gpus_required?, depends_on?, status?}].
    Returns {n_gpus, makespan_sec, tasks:[{id,name,gpu,gpus,start_sec,end_sec,
    est_time_sec,depends_on,status}], critical_path:[id,...]}."""
    n_gpus = max(1, int(n_gpus or 1))
    by_id = {r["id"]: r for r in runs}
    gpu_free = [float(now_sec)] * n_gpus
    finish: dict = {}
    placed: dict = {}
    order: list = []
    remaining = set(by_id)
    done: set = set()

    def plan(rid):
        r = by_id[rid]
        deps = [d for d in (r.get("depends_on") or []) if d in by_id]
        dep_done = max([finish.get(d, now_sec) for d in deps] or [now_sec])
        g = max(1, min(int(r.get("gpus_required") or 1), n_gpus))
        chosen = sorted(range(n_gpus), key=lambda i: gpu_free[i])[:g]
        start = max(dep_done, max(gpu_free[i] for i in chosen))
        return start, chosen, g

    while remaining:
        ready = [rid for rid in remaining
                 if all(d in done or d not in by_id
                        for d in (by_id[rid].get("depends_on") or []))]
        if not ready:                       # dependency cycle -> break it
            ready = list(remaining)
        best = min(ready, key=lambda rid: (plan(rid)[0],
                                           -int(by_id[rid].get("est_time_sec") or 0),
                                           str(rid)))
        start, chosen, _g = plan(best)
        dur = max(0, int(by_id[best].get("est_time_sec") or 0))
        end = start + dur
        for i in chosen:
            gpu_free[i] = end
        finish[best] = end
        placed[best] = {
            "id": best, "name": by_id[best].get("name") or best,
            "gpu": chosen[0], "gpus": chosen,
            "start_sec": start, "end_sec": end, "est_time_sec": dur,
            "depends_on": [d for d in (by_id[best].get("depends_on") or [])
                           if d in by_id],
            "status": by_id[best].get("status", ""),
        }
        order.append(best)
        remaining.discard(best)
        done.add(best)

    makespan = (max(finish.values()) - now_sec) if finish else 0.0
    return {
        "n_gpus": n_gpus,
        "makespan_sec": makespan,
        "total_gpu_sec": sum(p["est_time_sec"] * len(p["gpus"])
                             for p in placed.values()),
        "tasks": [placed[i] for i in order],
        "critical_path": _critical_path(placed),
    }
