"""The Principal Researcher abstraction (doc 05 §5.1).

The agent is pluggable behind a small interface so the orchestration logic can
be exercised without an LLM or GPUs:

  • FakeAgent — scripted and deterministic. It works against a pre-built
    experiment repo, parses ideas.md, and "implements" an idea by returning its
    hyperparameters. This is what the e2e integration test uses.
  • RealAgent — drives the real Claude Code CLI in a tmux session. It is the
    M3 real-mode milestone and is intentionally a documented stub here; it
    needs the `claude` binary, real tokens, and GPUs, none of which the
    hardware-free test path has.
"""
from __future__ import annotations

import os

from . import repo


class FakeAgent:
    """A scripted stand-in for the Principal Researcher. Given an experiment
    repo whose program.md / train.py / ideas.md already exist, it parses the
    ideas and maps each one to its hyperparameters — exactly the seam a real
    LLM agent fills by writing code into train.py."""

    def __init__(self, project_dir: str):
        self.project_dir = project_dir

    def bootstrap(self) -> list[dict]:
        """Parse the pre-built experiment repo and return its idea list."""
        with open(os.path.join(self.project_dir, "ideas.md")) as f:
            return repo.parse_ideas_md(f.read())

    def implement(self, idea: dict) -> dict:
        """'Write the code' for an idea. For the fake agent this is just the
        idea's hyperparameters; a real agent edits train.py and commits here."""
        return dict(idea.get("hpps") or {})

    def analyze(self, idea: dict, result: dict) -> str:
        """Produce an analysis paragraph from a completed run."""
        if result.get("crashed"):
            return ("The run failed to produce a metric — likely diverged or "
                    "crashed. Discard.")
        imp = result.get("improvement", 0.0)
        metric = result.get("metric", "the metric")
        if imp > 1e-6:
            return (f"Improved {metric} by {imp:.4f} versus baseline — these "
                    f"hyperparameters converge faster and more stably. Keep.")
        if imp < -1e-6:
            return (f"Regressed {metric} by {-imp:.4f} versus baseline — the "
                    f"settings are too aggressive or under-converged. Discard.")
        return "Within noise of the baseline — inconclusive."


class RealAgent:
    """Drives the real Claude Code CLI (docs 04–05). Not exercised by the e2e
    test. Implementing these methods is the M3 real-mode milestone:

      bootstrap() — launch `claude --dangerously-skip-permissions` in a tmux
        session, feed it prompts/setup_prompt.md.j2, and let it create the
        GitHub repo and write program.md / train.py / prepare.py / ideas.md.
      implement(idea) — instruct the agent to edit train.py for the idea and
        commit; return the resulting config.
    """

    def __init__(self, project_dir: str, tmux_session: str = "agent"):
        self.project_dir = project_dir
        self.tmux_session = tmux_session

    def bootstrap(self) -> list[dict]:
        raise NotImplementedError(
            "RealAgent.bootstrap is the M3 milestone — see "
            "docs/04-onboarding-and-agent-bootstrap.md §4.6.")

    def implement(self, idea: dict) -> dict:
        raise NotImplementedError(
            "RealAgent.implement is the M3 milestone — see "
            "docs/05-autoresearch-engine.md §5.2.")

    def analyze(self, idea: dict, result: dict) -> str:
        return ""
