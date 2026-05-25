#!/usr/bin/env python3
"""Mock Claude Code agent — exercises the RealAgent path with no LLM and no GPU.

RealAgent launches an autonomous agent process in a tmux session. In production
that process is the real `claude` CLI; for the e2e test it is this script. It
behaves like a real autonomous researcher: it runs a sequence of genuine
experiments, each logging through the arui SDK exactly as a real agent's
train.py would — so the whole RealAgent → tmux → arui → dashboard pipeline is
tested end to end, deterministically, without an LLM.
"""
import math
import os
import random
import sys
import time

sys.path.insert(0, os.environ.get("ARUI_REPO", "."))
import arui  # noqa: E402

PROJECT = os.environ.get("ARUI_PROJECT", "research")


def experiment(name, lr, momentum, steps, seed):
    """A real (tiny) optimisation experiment: fit y = 3x + 2 with SGD."""
    rng = random.Random(seed)
    tr = [(x, 3 * x + 2 + rng.gauss(0, 0.3))
          for x in (rng.uniform(-2, 2) for _ in range(200))]
    va = [(x, 3 * x + 2 + rng.gauss(0, 0.3))
          for x in (rng.uniform(-2, 2) for _ in range(80))]
    w = b = vw = vb = 0.0
    arui.init(project=PROJECT, name=name,
              config={"lr": lr, "momentum": momentum, "steps": steps,
                      "seed": seed})
    diverged = False
    for s in range(steps):
        gw = gb = 0.0
        for x, y in tr:
            e = w * x + b - y
            gw += 2 * e * x
            gb += 2 * e
        gw /= len(tr)
        gb /= len(tr)
        vw = momentum * vw - lr * gw
        vb = momentum * vb - lr * gb
        w += vw
        b += vb
        tl = sum((w * x + b - y) ** 2 for x, y in tr) / len(tr)
        arui.log({"train_loss": tl}, step=s)
        if not math.isfinite(tl) or tl > 1e6:
            diverged = True
            break
    val = 1.0e6 if diverged else sum((w * x + b - y) ** 2 for x, y in va) / len(va)
    if not math.isfinite(val):
        val = 1.0e6
    arui.log({"val_mse": val}, step=steps)
    arui.summary["val_mse"] = val
    arui.finish()
    return val


# The agent's exploration sequence (an exploration order, baseline first).
PLAN = [
    ("baseline", 0.008, 0.0, 40, 0),
    ("more-steps-200", 0.008, 0.0, 200, 0),
    ("more-steps-600", 0.008, 0.0, 600, 0),
    ("lr-0.05", 0.05, 0.0, 200, 0),
    ("lr-0.15", 0.15, 0.0, 200, 0),
    ("momentum-0.9", 0.05, 0.9, 200, 0),
    ("lr0.15-mom0.9", 0.15, 0.9, 300, 0),
    ("aggressive-lr", 0.9, 0.0, 200, 0),
    ("lr0.12-mom0.9-400", 0.12, 0.9, 400, 0),
    ("seed-7", 0.15, 0.9, 400, 7),
]

if __name__ == "__main__":
    print(f"[mock-agent] starting research loop for project '{PROJECT}'",
          flush=True)
    for name, lr, mom, steps, seed in PLAN:
        print(f"[mock-agent] experiment: {name}", flush=True)
        vm = experiment(name, lr, mom, steps, seed)
        print(f"[mock-agent]   {name}: val_mse = {vm:.4f}", flush=True)
        time.sleep(0.15)
    print("[mock-agent] research loop complete", flush=True)
