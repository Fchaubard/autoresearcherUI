# tiny-sgd — autonomous research (e2e test fixture)

This is the example research project autoresearcherUI uses as its end-to-end
integration test. It is deliberately tiny and CPU-only so the whole research
loop runs in seconds and can gate every merge to `main`.

## Purpose
Find the gradient-descent hyperparameters that best fit `y = 3x + 2` from noisy
samples. The optimizer (`train.py`) is fixed; the agent explores learning rate,
momentum, and step count.

## Metric
`val_mse` — mean squared error on a held-out validation set. **Lower is better.**

## Files
- `prepare.py` — data preparation (a no-op here; data is generated in-process).
- `train.py` — the trainer. Reads hyperparameters from `$ARUI_CONFIG`, logs via
  the `arui` SDK, and prints a `val_mse:` summary line.
- `ideas.md` — the idea backlog, each block carrying its `HPPs` as JSON.

## The loop
The orchestrator runs the `baseline` idea first, then the remaining ideas
highest-EV first, comparing each `val_mse` to the baseline and keeping or
discarding accordingly. See `docs/05-autoresearch-engine.md`.
