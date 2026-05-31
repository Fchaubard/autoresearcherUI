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

    @staticmethod
    def _ensure_claude_settings() -> None:
        """Pre-write Claude Code's config files so:

          1. Claude authenticates via our ``apiKeyHelper`` (= read the
             ANTHROPIC_API_KEY env var) instead of falling into OAuth.
          2. The one-time "Bypass Permissions" consent screen is
             pre-accepted, so a fresh node never gets stuck on
             "1. No, exit / 2. Yes, I accept".
          3. The "Welcome to Claude Code" onboarding wizard is skipped.

        Claude Code 1.x splits state across two locations depending on
        version. We write BOTH so the relevant one always exists:
          - ~/.claude.json          (single-file user config, newer)
          - ~/.claude/settings.json (folder-style config, older)

        We merge into existing JSON if present rather than clobber it,
        so a user who's already done OAuth keeps their credentials.
        """
        import json as _json
        # The fields we set. apiKeyHelper is the documented one;
        # the consent flags are best-effort across Claude Code versions
        # (different versions read different keys). Unknown keys are
        # harmless — Claude Code ignores them. If even one of these
        # matches the current version, the consent screen is gone.
        FORCE = {
            "apiKeyHelper": "printenv ANTHROPIC_API_KEY",
            "hasCompletedOnboarding": True,
            "bypassPermissionsModeAccepted": True,
            "dangerouslySkipPermissionsModeAccepted": True,
            "hasAcceptedDangerouslySkipPermissions": True,
            "permissions": {
                "bypassModeAccepted": True,
                "dangerouslySkipPermissionsAccepted": True,
            },
        }

        def _merge_into(path: str) -> None:
            cur: dict = {}
            if os.path.exists(path):
                try:
                    with open(path) as f:
                        cur = _json.load(f) or {}
                    if not isinstance(cur, dict):
                        cur = {}
                except (OSError, ValueError):
                    cur = {}
            # Only overwrite each key if the user hasn't set their own
            # value (idempotent on re-runs); but DO set the consent
            # flags every time since they're booleans with no user
            # preference to preserve.
            for k, v in FORCE.items():
                if k == "apiKeyHelper" and cur.get("apiKeyHelper"):
                    continue
                if k == "permissions" and isinstance(cur.get(k), dict):
                    cur[k].update(v)
                else:
                    cur[k] = v
            try:
                d = os.path.dirname(path)
                if d:
                    os.makedirs(d, exist_ok=True)
                tmp = path + ".tmp"
                with open(tmp, "w") as f:
                    _json.dump(cur, f, indent=2)
                os.replace(tmp, path)
            except OSError as e:
                print(f"[agent] could not write {path}: {e}", flush=True)

        home = os.path.expanduser("~")
        _merge_into(os.path.join(home, ".claude.json"))
        _merge_into(os.path.join(home, ".claude", "settings.json"))
        print("[agent] pre-accepted Claude Code consent + apiKeyHelper "
              "set — no OAuth or bypass-permissions prompt should appear",
              flush=True)

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
            # Tell Claude Code to use the API key directly instead of
            # falling back to its OAuth flow. Claude Code reads
            # ~/.claude/settings.json on startup; when `apiKeyHelper` is
            # set, Claude runs that shell command and uses its stdout as
            # the API key for every request. That kills the OAuth path
            # entirely (no browser, no "Paste code here", no state
            # parameter dance) and means a fresh node only needs the
            # onboarding-saved Claude token to authenticate.
            self._ensure_claude_settings()
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
        # Once Claude Code has booted, hand it the research brief.
        #
        # Claude Code shows a one-time "Bypass Permissions mode" consent
        # prompt the first time --dangerously-skip-permissions is used on a
        # fresh ~/.config/claude. The prompt has two numbered options:
        #     1. No, exit
        #     2. Yes, I accept all responsibility …
        # We auto-accept by typing the literal "2" then Enter. The numeric
        # shortcut is shown in the menu and bypasses any arrow-key quirks
        # (Down + Enter previously confirmed the highlighted "No, exit" on
        # some Claude Code builds because the keystrokes raced the render).
        # On subsequent restarts the consent is remembered, the prompt
        # never appears, and the stray "2" is typed at the REPL — Claude
        # Code's REPL eats single digits without side effects.
        # See https://code.claude.com/docs/en/security
        if not self.agent_cmd:
            msg = ("Read the file _setup_prompt.txt in this directory and "
                   "carry out the research it describes. Do not stop.")
            sess = shlex.quote(self.session)
            # POLL-based auto-accept. Old code used a fixed `sleep 6`
            # before sending "2"+Enter — but on a slow fresh node Claude
            # Code can take 10–15 s to even draw the consent screen, so
            # the keystroke fired before the prompt existed and the user
            # was left staring at "1. No, exit / 2. Yes, I accept".
            # Instead: every 1 s, capture the pane, look for the consent
            # text; when it appears, press 2 + Enter. Then wait for the
            # REPL to be ready (input prompt or welcome banner) and hand
            # over the research brief. Hard cap at 90 s to never loop
            # forever on a real failure.
            script = f"""set -u
SESS={sess}
BRIEF={shlex.quote(msg)}
sent_consent=0
sent_brief=0
for i in $(seq 1 90); do
  sleep 1
  PANE=$(tmux capture-pane -t "$SESS" -p -J -S -300 2>/dev/null || true)
  if [ "$sent_consent" -eq 0 ]; then
    if printf "%s" "$PANE" | grep -qiE 'Yes, *I *accept|Bypass *Permissions' \\
       && printf "%s" "$PANE" | grep -qiE 'No, *exit'; then
      tmux send-keys -t "$SESS" '2' >/dev/null 2>&1
      sleep 0.4
      tmux send-keys -t "$SESS" Enter >/dev/null 2>&1
      sent_consent=1
      sleep 2
      continue
    fi
  fi
  if [ "$sent_brief" -eq 0 ]; then
    if printf "%s" "$PANE" | grep -qE 'How can I help|Welcome to Claude|│ +>|❯ *$|^ *> *$'; then
      sleep 1
      tmux send-keys -t "$SESS" -l "$BRIEF" >/dev/null 2>&1
      sleep 0.5
      tmux send-keys -t "$SESS" Enter >/dev/null 2>&1
      sent_brief=1
      break
    fi
  fi
done
if [ "$sent_brief" -eq 0 ]; then
  tmux send-keys -t "$SESS" -l "$BRIEF" >/dev/null 2>&1
  sleep 0.5
  tmux send-keys -t "$SESS" Enter >/dev/null 2>&1
fi
"""
            subprocess.Popen(["sh", "-c", script])
        return self.session

    def alive(self) -> bool:
        return subprocess.run(
            ["tmux", "has-session", "-t", self.session],
            capture_output=True).returncode == 0
