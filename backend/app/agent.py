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
import threading

from . import repo


def _install_arui_pth(repo_root: str) -> None:
    """Make `import arui` work from ANY python / ANY cwd, permanently.

    The agent's training runs execute from the project workspace (not the
    repo root) and inside arun's detached tmux sessions. Even though arun now
    forwards PYTHONPATH, we ALSO drop an `arui_repo.pth` into each candidate
    interpreter's site-packages so a bare `python train.py` works too. This
    is exactly the workaround the agent kept hand-rolling (and burning ~10
    min on) every fresh boot — now the platform just does it. Idempotent +
    best-effort."""
    snippet = (
        "import site,os\n"
        f"root={repo_root!r}\n"
        "dirs=set()\n"
        "try:\n"
        " dirs.update(site.getsitepackages())\n"
        "except Exception: pass\n"
        "try:\n"
        " dirs.add(site.getusersitepackages())\n"
        "except Exception: pass\n"
        "for d in dirs:\n"
        " try:\n"
        "  os.makedirs(d,exist_ok=True)\n"
        "  open(os.path.join(d,'arui_repo.pth'),'w').write(root+'\\n')\n"
        " except Exception: pass\n")
    pythons = ["python3", "python",
               os.path.join(repo_root, ".venv", "bin", "python")]
    seen = set()
    for py in pythons:
        if py in seen:
            continue
        seen.add(py)
        try:
            subprocess.run([py, "-c", snippet], capture_output=True, timeout=20)
        except Exception:
            pass


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
                 session: str = "agent", kill_criteria: str = ""):
        self.workspace = os.path.abspath(workspace)
        self.project_name = project_name
        self.ingest_url = ingest_url
        self.repo_root = repo_root
        self.agent_cmd = agent_cmd       # set -> custom/mock agent; None -> real claude
        self.anthropic_key = anthropic_key
        self.setup_prompt = setup_prompt
        self.session = session
        # Free-text kill policy (e.g. "1 hour" / "val_loss plateaus for 500
        # steps"). Exposed to the agent as $ARUI_KILL_CRITERIA so the agent
        # knows what the dashboard monitor will enforce.
        self.kill_criteria = kill_criteria or ""

    @staticmethod
    def _api_key_truncation(key: str) -> str:
        """Reproduce Claude Code's display-truncation for an API key.

        Claude Code's "Do you want to use this API key?" prompt shows
        the key as ``<first-7>...<last-20>`` (e.g. ``sk-ant-...Vx9j…QAA``).
        The ``customApiKeyResponses.approved`` config field is a list of
        those exact truncations — when our truncation is on the list,
        the prompt is skipped entirely.

        If the key is too short to truncate meaningfully, return it
        as-is (worst case the prompt still appears and the poll
        handler picks "1. Yes" anyway).
        """
        k = (key or "").strip()
        if len(k) < 30:
            return k
        return f"{k[:7]}...{k[-20:]}"

    @staticmethod
    def _ensure_claude_settings(anthropic_key: str = "") -> None:
        """Pre-write Claude Code's config files so:

          1. The one-time "Bypass Permissions" consent is pre-accepted,
             so a fresh node never gets stuck on
             "1. No, exit / 2. Yes, I accept".
          2. The "Welcome to Claude Code" onboarding wizard is skipped.
          3. The "Trust this folder" dialog is pre-accepted.
          4. The "Do you want to use this API key?" dialog is skipped
             by registering the key's display-truncation as approved.

        We rely on ``ANTHROPIC_API_KEY`` in the spawned process's env
        (set by ``RealAgent.start()``) for authentication. We do NOT
        write ``apiKeyHelper`` — Claude Code 2.1.159 emits a noisy
        "Auth conflict: both apiKeyHelper AND ANTHROPIC_API_KEY are
        set" warning when both are present, and ANTHROPIC_API_KEY in
        env alone (combined with the customApiKeyResponses approval)
        is enough to bypass every prompt. If we find an existing
        apiKeyHelper from a prior install, we DELETE it on merge.

        Claude Code splits state across two locations depending on
        version. We write BOTH so the relevant one always exists:
          - ~/.claude.json          (single-file user config, newer)
          - ~/.claude/settings.json (folder-style config, older)

        We merge into existing JSON if present rather than clobber it,
        so a user who's already done OAuth keeps their credentials.
        """
        import json as _json
        # The fields we set. The consent flags are best-effort across
        # Claude Code versions (different versions read different keys).
        # Unknown keys are harmless — Claude Code ignores them. If even
        # one of these matches the current version, the consent screen
        # is gone.
        FORCE: dict = {
            "hasCompletedOnboarding": True,
            "bypassPermissionsModeAccepted": True,
            "dangerouslySkipPermissionsModeAccepted": True,
            "hasAcceptedDangerouslySkipPermissions": True,
            # "Trust this folder" dialog (different from bypass-perms;
            # appears the first time Claude is run in any new dir).
            "hasTrustDialogAccepted": True,
            "trustDialogAccepted": True,
            "hasAcceptedTrustDialog": True,
            "permissions": {
                "bypassModeAccepted": True,
                "dangerouslySkipPermissionsAccepted": True,
                "trustDialogAccepted": True,
            },
        }
        # If we know the key, pre-approve its display-truncation so the
        # "Do you want to use this API key?" prompt is skipped.
        key = anthropic_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if key:
            FORCE["customApiKeyResponses"] = {
                "approved": [RealAgent._api_key_truncation(key)],
                "rejected": [],
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
            # Remove a previously-written apiKeyHelper — Claude Code
            # 2.1.159+ warns when both apiKeyHelper AND
            # ANTHROPIC_API_KEY env are present. We use the env var, so
            # apiKeyHelper is redundant and noisy.
            if "apiKeyHelper" in cur:
                del cur["apiKeyHelper"]
            # Apply FORCE: overwrite each key (consent flags are
            # booleans with no user preference to preserve).
            for k, v in FORCE.items():
                if k == "permissions" and isinstance(cur.get(k), dict):
                    cur[k].update(v)
                elif k == "customApiKeyResponses" and isinstance(cur.get(k), dict):
                    # Merge our approved truncation into any user-set list
                    # so we don't clobber prior approvals.
                    existing = cur[k]
                    approved = list(existing.get("approved") or [])
                    for t in v.get("approved", []):
                        if t and t not in approved:
                            approved.append(t)
                    existing["approved"] = approved
                    existing.setdefault("rejected", [])
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
        print("[agent] pre-accepted Claude Code consent dialogs + "
              "approved API-key truncation — no OAuth or "
              "bypass-permissions / trust-folder / 'use this API key?' "
              "prompt should appear",
              flush=True)

    def start(self) -> str:
        """Prepare the workspace and launch the agent in a tmux session.

        The agent runs live IN the pane (no stdout redirect) so the dashboard's
        Live tab can follow the session and the user can type into it. The real
        agent is launched interactively (not `-p`) so it stays attachable and
        chattable; the setup prompt is handed to it once it has booted.
        """
        os.makedirs(self.workspace, exist_ok=True)
        # Make `import arui` work from any python/cwd in the runs this agent
        # will launch (recurring footgun — see _install_arui_pth).
        _install_arui_pth(self.repo_root)
        env = {
            "ARUI_INGEST_URL": self.ingest_url,
            "ARUI_PROJECT": self.project_name,
            "ARUI_REPO": self.repo_root,
            "PYTHONPATH": self.repo_root,
            # rented GPU containers run as root; this lets Claude Code accept
            # --dangerously-skip-permissions in that sandboxed environment.
            "IS_SANDBOX": "1",
        }
        # Surface the user's free-text kill-criteria policy to the agent so
        # it knows what the autoresearcherUI monitor will enforce on every
        # training run it launches. monitor._apply_kill_criteria reads this
        # same policy from the onboarding setting and auto-kills offending
        # runs.
        if self.kill_criteria:
            env["ARUI_KILL_CRITERIA"] = self.kill_criteria
        # If the dashboard has a passcode set, expose it to the agent
        # as ARUI_INGEST_TOKEN so the `arui` SDK + every curl call the
        # agent makes to $ARUI_INGEST_URL/api/* auto-authenticates.
        # Without this, the agent has to forensically discover the
        # passcode (typically by querying the SQLite DB directly),
        # which wastes minutes of agent time on every fresh boot.
        try:
            from . import auth as _auth
            pc = _auth._saved_passcode()
            if pc:
                env["ARUI_INGEST_TOKEN"] = pc
        except Exception as e:                          # noqa: BLE001
            print(f"[agent] could not read passcode for env: {e}",
                  flush=True)
        if self.anthropic_key:
            env["ANTHROPIC_API_KEY"] = self.anthropic_key
            # Pre-write Claude Code's config:
            #  - Pre-accept all consent dialogs (bypass perms, trust
            #    folder, dangerously-skip-permissions)
            #  - Pre-approve this key's display-truncation so the
            #    "Do you want to use this API key?" prompt is skipped
            # Claude Code 2.1.159+ reads ANTHROPIC_API_KEY directly
            # from env when no apiKeyHelper is set, so the env var
            # above is sufficient for auth — and avoids the noisy
            # "Auth conflict" warning that fires when BOTH apiKeyHelper
            # and ANTHROPIC_API_KEY are set.
            self._ensure_claude_settings(self.anthropic_key)
        exports = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())
        log = os.path.join(self.workspace, "agent.log")

        if self.agent_cmd:                       # mock / custom agent
            inner = " ".join(shlex.quote(c) for c in self.agent_cmd)
        else:                                    # real Claude Code — interactive
            pf = os.path.join(self.workspace, "_setup_prompt.txt")
            with open(pf, "w") as f:
                f.write(self.setup_prompt)
            # DEFAULT claude — its normal pretty TUI. Scrollback comes from
            # turning OFF tmux's alternate-screen (below) so the full-screen TUI
            # paints into the scrollback-bearing normal buffer. See
            # author_agent.start for the full rationale.
            inner = "claude --dangerously-skip-permissions"

        full = f"cd {shlex.quote(self.workspace)} && {exports} {inner}"
        subprocess.run(["tmux", "kill-session", "-t", self.session],
                       capture_output=True)
        # Create a bare shell, disable tmux's alternate-screen for this window
        # (so Claude Code's TUI renders into the normal scrollback buffer), then
        # launch Claude. The option must be set before claude enters alt-screen.
        subprocess.run(["tmux", "new-session", "-d", "-s", self.session,
                        "-x", "120", "-y", "40"], check=True)
        subprocess.run(["tmux", "set-window-option", "-t", self.session,
                        "alternate-screen", "off"], capture_output=True)
        subprocess.run(["tmux", "send-keys", "-t", self.session, full, "Enter"],
                       capture_output=True)
        # Mirror the live pane into BOTH a per-session raw-byte file
        # (`pane_stream.term_file(session)`) — what the rail xterm.js
        # streams from for true ANSI rendering — AND `agent.log` for a
        # persistent per-workspace record. One pipe-pane invocation
        # with `tee` does both.
        from . import pane_stream
        # preserve_history=False — agent (re-)boot wipes the raw stream
        # file so the next frontend connection sees a clean Claude REPL
        # rather than the previous boot's output. Other callers (user
        # session attaches, the periodic sweeper) keep the default
        # preserve_history=True so an already-running session keeps its
        # context when freshly attached.
        pane_stream.enable(self.session, mirror_to=log,
                           preserve_history=False)
        # If the frontend's xterm has reported dimensions for this
        # session in a prior life, restore them now — otherwise the
        # rail will see Claude rendering at 120x40 (our spawn default)
        # until the user happens to drag the resize handle, which is
        # bad UX after `/api/agent/restart`.
        pane_stream.apply_remembered_size(self.session)
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
            # Claude Code shows up to THREE distinct consent dialogs on a
            # fresh ~/.claude — we handle each independently so order
            # doesn't matter:
            #
            # (a) "Do you want to use this API key?"  (when
            #     ANTHROPIC_API_KEY is set in env)  →  options are
            #     1.Yes / 2.No (recommended). Default is NO — typing
            #     Enter would reject the key. We must type "1" + Enter.
            #
            # (b) "Trust this folder" (Claude has never seen this dir)
            #     → 1.Yes / 2.No; "Yes" is highlighted default, Enter
            #     is enough.
            #
            # (c) "Bypass Permissions" / "Yes, I accept all responsibility"
            #     (--dangerously-skip-permissions on a fresh install) →
            #     1.No,exit / 2.Yes,I accept. Default is NO — must type
            #     "2" + Enter.
            #
            # Each handler is idempotent and tracked separately so they
            # can fire in any order. Once the REPL prompt is detected,
            # the research brief is typed in.
            script = f"""set -u
SESS={sess}
BRIEF={shlex.quote(msg)}
sent_apikey=0
sent_trust=0
sent_bypass=0
sent_brief=0
for i in $(seq 1 90); do
  sleep 1
  PANE=$(tmux capture-pane -t "$SESS" -p -J -S -300 2>/dev/null || true)

  # (a) "Do you want to use this API key?" — pick 1 (Yes). The
  # default 2 (No, recommended) would REFUSE the key. Detect via
  # the literal prompt + the "(recommended)" hint on option 2.
  if [ "$sent_apikey" -eq 0 ]; then
    if printf "%s" "$PANE" | grep -qiE 'use *this *API *key' \\
       && printf "%s" "$PANE" | grep -qE 'No.*\\(recommended\\)'; then
      tmux send-keys -t "$SESS" '1' >/dev/null 2>&1
      sleep 0.4
      tmux send-keys -t "$SESS" Enter >/dev/null 2>&1
      sent_apikey=1
      sleep 2
      continue
    fi
  fi

  # (b) Trust this folder — default is Yes, just Enter.
  if [ "$sent_trust" -eq 0 ]; then
    if printf "%s" "$PANE" | grep -qiE 'trust *this *folder|Do *you *trust' ; then
      tmux send-keys -t "$SESS" Enter >/dev/null 2>&1
      sent_trust=1
      sleep 2
      continue
    fi
  fi

  # (c) Bypass Permissions consent — option 2 = Yes.
  if [ "$sent_bypass" -eq 0 ]; then
    if printf "%s" "$PANE" | grep -qiE 'Yes, *I *accept|Bypass *Permissions' \\
       && printf "%s" "$PANE" | grep -qiE 'No, *exit'; then
      tmux send-keys -t "$SESS" '2' >/dev/null 2>&1
      sleep 0.4
      tmux send-keys -t "$SESS" Enter >/dev/null 2>&1
      sent_bypass=1
      sleep 2
      continue
    fi
  fi

  # REPL ready — type the research brief in. Detection is broad
  # because Claude Code reformats the welcome screen across versions:
  #   - 2.0.x said "How can I help" + a bare "│ >" prompt
  #   - 2.1.158+ says "Welcome back!" + a "❯ Try ..." placeholder line
  # The "⏵⏵ bypass permissions on" status line appears only AFTER all
  # consent dialogs have cleared and the REPL is fully ready, so it
  # is the single most reliable marker.
  if [ "$sent_brief" -eq 0 ]; then
    if printf "%s" "$PANE" | grep -qE \
         "How can I help|Welcome to Claude|Welcome back|bypass permissions on|^❯ |│ +> |❯ *Try" ; then
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
            # Run the consent-automation script in a reaped daemon thread
            # rather than a detached fire-and-forget Popen: the script polls
            # for up to ~90s, and an unwaited Popen leaves a zombie `sh` that
            # accumulates on every (re)start. subprocess.run inside the thread
            # reaps the child; the timeout bounds it if tmux never settles.
            def _run_consent():
                try:
                    subprocess.run(["sh", "-c", script], timeout=150,
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
                except Exception:                          # noqa: BLE001
                    pass
            threading.Thread(target=_run_consent, daemon=True,
                             name="agent-consent").start()
        return self.session

    def alive(self) -> bool:
        return subprocess.run(
            ["tmux", "has-session", "-t", self.session],
            capture_output=True).returncode == 0
