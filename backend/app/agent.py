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
import shlex
import subprocess

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
    """Launches an autonomous research agent in a tmux session and lets it run
    its own research loop (docs 04 §4.6, doc 05).

    In production the agent is the real Claude Code CLI
    (`claude --dangerously-skip-permissions`), fed the setup prompt; for the
    e2e test it is a deterministic mock agent. Either way the agent's
    experiments log through the arui SDK, and that is what populates the
    dashboard — so the same launch path serves both.
    """

    def __init__(self, workspace: str, project_name: str, ingest_url: str,
                 repo_root: str, agent_cmd: list[str] | None = None,
                 anthropic_key: str = "", setup_prompt: str = "",
                 session: str = "agent"):
        self.workspace = os.path.abspath(workspace)
        self.project_name = project_name
        self.ingest_url = ingest_url
        self.repo_root = repo_root
        self.agent_cmd = agent_cmd       # set -> custom/mock agent; None -> real claude
        self.anthropic_key = anthropic_key
        self.setup_prompt = setup_prompt
        self.session = session

    def start(self) -> str:
        """Prepare the workspace and launch the agent in a tmux session.

        The agent runs live IN the pane (no stdout redirect) so the dashboard's
        Live tab can follow the session and the user can type into it. The real
        agent is launched interactively (not `-p`) so it stays attachable and
        chattable; the setup prompt is handed to it once it has booted.
        """
        os.makedirs(self.workspace, exist_ok=True)
        env = {
            "ARUI_INGEST_URL": self.ingest_url,
            "ARUI_PROJECT": self.project_name,
            "ARUI_REPO": self.repo_root,
            "PYTHONPATH": self.repo_root,
            # rented GPU containers run as root; this lets Claude Code accept
            # --dangerously-skip-permissions in that sandboxed environment.
            "IS_SANDBOX": "1",
        }
        if self.anthropic_key:
            env["ANTHROPIC_API_KEY"] = self.anthropic_key
        exports = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())
        log = os.path.join(self.workspace, "agent.log")

        if self.agent_cmd:                       # mock / custom agent
            inner = " ".join(shlex.quote(c) for c in self.agent_cmd)
        else:                                    # real Claude Code — interactive
            pf = os.path.join(self.workspace, "_setup_prompt.txt")
            with open(pf, "w") as f:
                f.write(self.setup_prompt)
            inner = "claude --dangerously-skip-permissions"

        full = f"cd {shlex.quote(self.workspace)} && {exports} {inner}"
        subprocess.run(["tmux", "kill-session", "-t", self.session],
                       capture_output=True)
        subprocess.run(["tmux", "new-session", "-d", "-s", self.session,
                        "-x", "210", "-y", "52", full], check=True)
        # mirror the live pane into agent.log for a persistent record
        subprocess.run(["tmux", "pipe-pane", "-t", self.session, "-o",
                        f"cat >> {shlex.quote(log)}"], capture_output=True)
        # once Claude Code has booted, hand it the research brief.
        #
        # Claude Code now shows a one-time "Bypass Permissions mode" consent
        # prompt the first time --dangerously-skip-permissions is used on a
        # fresh ~/.config/claude. The prompt has two options:
        #     1. No, exit
        #     2. Yes, I accept all responsibility for actions taken …
        # The cursor starts on "No, exit". We auto-dismiss by sending
        # Down+Enter once the binary has spawned — that selects "Yes" and
        # confirms. On subsequent restarts the consent is remembered and
        # Down+Enter is a no-op (Down is interpreted as REPL history nav).
        # See https://code.claude.com/docs/en/security
        if not self.agent_cmd:
            msg = ("Read the file _setup_prompt.txt in this directory and "
                   "carry out the research it describes. Do not stop.")
            sess = shlex.quote(self.session)
            script = (
                # 1) wait for `claude` to draw its splash screen
                "sleep 4 && "
                # 2) auto-accept the bypass-permissions consent if shown
                f"tmux send-keys -t {sess} Down && sleep 0.3 && "
                f"tmux send-keys -t {sess} Enter && "
                # 3) wait for the REPL to actually be ready
                "sleep 8 && "
                # 4) hand it the research brief
                f"tmux send-keys -t {sess} -l {shlex.quote(msg)} && "
                "sleep 1 && "
                f"tmux send-keys -t {sess} Enter")
            subprocess.Popen(["sh", "-c", script])
        return self.session

    def alive(self) -> bool:
        return subprocess.run(
            ["tmux", "has-session", "-t", self.session],
            capture_output=True).returncode == 0
