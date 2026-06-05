"""Watchdog package — non-LLM monitoring harness for running experiments.

PR 4 of the state-control rewrite (2026-06-05). Solves the "agent kicks
off a run and goes back to the REPL — nobody is watching" problem
Francois flagged.

Mental model:
  • The watchdog is a small, fast, deterministic supervisor that scans
    every RUNNING experiment every ~60s and applies a list of "scripts"
    to it. Each script is a pure ``check(run, metrics, params) -> Issue?``
    function with a default-params dict and a human-readable
    description.
  • Default scripts ship with the package: no_metric_flow, nan_loss,
    diverging, gpu_oom, crashed_silently, done_signal.
  • Each project can override per-script params via the
    ``watchdog.config`` Setting (PR 5 lets the agent review + adjust
    these at onboarding via the council).
  • When a script fires, the watchdog can (a) emit an Issue Event, (b)
    optionally kill the run, and (c) PAGE the agent via tmux send-keys
    so the agent wakes up to diagnose / relaunch / move on. The agent
    NEVER polls metrics — it's interrupt-driven.

This keeps the agent's token budget for thinking, not babysitting, and
gives the operator visibility into "is anything actually wrong?"
without the agent having to notice on its own.
"""
from .config import (DEFAULT_CONFIG, get_config,         # noqa: F401
                      get_script_params, list_scripts,
                      set_config)
from .runner import tick, run_once                       # noqa: F401
