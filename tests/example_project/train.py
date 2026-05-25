"""tiny-sgd — the example research project for autoresearcherUI's e2e test.

A real but tiny ML task: fit y = 3x + 2 from noisy samples with full-batch
gradient descent. Pure Python, deterministic, runs in well under a second on
CPU. Logs every step through the `arui` SDK and prints a `val_mse:` summary the
orchestrator parses. Hyperparameters come from the JSON file at $ARUI_CONFIG.
"""
import json
import math
import os
import random

import arui

CFG = {"lr": 0.02, "momentum": 0.0, "steps": 60, "seed": 0}
if os.environ.get("ARUI_CONFIG"):
    try:
        with open(os.environ["ARUI_CONFIG"]) as fh:
            CFG.update(json.load(fh))
    except OSError:
        pass

rng = random.Random(int(CFG["seed"]))


def make(n):
    xs, ys = [], []
    for _ in range(n):
        x = rng.uniform(-2.0, 2.0)
        xs.append(x)
        ys.append(3.0 * x + 2.0 + rng.gauss(0.0, 0.3))
    return xs, ys


def mse(w, b, xs, ys):
    return sum((w * x + b - y) ** 2 for x, y in zip(xs, ys)) / len(xs)


trX, trY = make(200)
vaX, vaY = make(80)

lr = float(CFG["lr"])
mom = float(CFG["momentum"])
steps = int(CFG["steps"])
w = b = vw = vb = 0.0

arui.init(project=os.environ.get("ARUI_PROJECT", "tiny-sgd"),
          name=os.environ.get("ARUI_RUN_NAME", "run"), config=CFG)

diverged = False
for step in range(steps):
    gw = gb = 0.0
    for x, y in zip(trX, trY):
        err = w * x + b - y
        gw += 2.0 * err * x
        gb += 2.0 * err
    gw /= len(trX)
    gb /= len(trX)
    vw = mom * vw - lr * gw
    vb = mom * vb - lr * gb
    w += vw
    b += vb
    tl = mse(w, b, trX, trY)
    arui.log({"train_loss": tl}, step=step)
    if not math.isfinite(tl) or tl > 1e6:
        diverged = True
        break

val = 1.0e6 if diverged else mse(w, b, vaX, vaY)
if not math.isfinite(val):
    val = 1.0e6
arui.log({"val_mse": val}, step=steps)
arui.summary["val_mse"] = val
arui.finish()

print(f"val_mse: {val:.6f}")
print(f"weights: w={w:.4f} b={b:.4f} diverged={diverged}")
