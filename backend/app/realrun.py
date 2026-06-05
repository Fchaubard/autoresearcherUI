"""Launch a real research run — create the project and start the RealAgent.

This is the real-mode path (doc 05): an autonomous agent runs its own research
loop in a tmux session; its experiments log through the arui SDK, which
populates the dashboard. Used when the user provides a Claude token at
onboarding (or, for the e2e test, when ARUI_CLAUDE_BIN points at the mock).
"""
from __future__ import annotations

import datetime as dt
import os
import shlex
import subprocess

from .agent import RealAgent
from .config import DATA_DIR, PORT, ROOT
from .db import SessionLocal
from .models import Event, Project

_agent: RealAgent | None = None


def _iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _direction(metric: str) -> str:
    m = (metric or "").lower()
    up = ("acc", "f1", "score", "reward", "bleu", "pass", "win", "exact")
    return "maximize" if any(t in m for t in up) else "minimize"


DEFAULT_AGENT_INSTRUCTIONS = """# Your task
Conduct this research autonomously in this directory. Create program.md,
train.py, prepare.py and ideas.md. Run the baseline first, then explore one
idea per experiment, keeping every idle GPU busy. Do not stop — keep
researching.

# CODE FREEZE — required before any training run launches
The autoresearcherUI council MUST review your code before any training
run is allowed. BUT — before you can even request a council review, you
MUST complete the 3-step PRE-FLIGHT SOP below. The flow is
non-negotiable.

## PRE-FLIGHT SOP — 3 checks BEFORE any real run

Every major code change (this includes the very first scaffold of
program.md / train.py / prepare.py) MUST pass all three of the
following checks. The bless gate REJECTS approval if any preflight
flag is missing or stale (older than 24h). Real runs (any name NOT
starting with `_probe` or `_smoke`) stay HTTP-423 locked until all
three pass.

  STEP 1 — Static-batch overfit (proves train.py is correct).
    Take a tiny static batch (e.g. 8–16 examples), turn off
    regularisation/dropout/augmentation, and train for enough steps
    that the model MEMORISES the batch. Final train loss MUST hit
    ~0 (e.g. < 1e-3 for CE, < 1e-4 for MSE). If it doesn't, there's
    a bug in train.py (optimiser not stepping, loss not connected
    to logits, labels mis-aligned, .backward() missing, etc).
    Once you see ~0 loss, record it with:
        curl -sS -X POST $ARUI_INGEST_URL/api/preflight/static_overfit \
            -H "Authorization: Bearer $ARUI_INGEST_TOKEN" \
            -H "Content-Type: application/json" \
            -d '{"evidence":"overfit_smoke.py memorised 16 examples in 300 steps","final_loss":0.0008}'

  STEP 2 — Uniform classification head at init (proves architecture
  is correct). Right after model construction, BEFORE any training,
  feed a batch through the model and check the output distribution
  at the classification head:
      - softmax probabilities should be ~ 1/num_classes for every
        class on every example (within a few percent);
      - equivalently, output entropy should be ~ log(num_classes).
    If the head is biased toward one class at init, the architecture
    has a bug (bias initialised non-zero, last-layer init scale
    wrong, residual collapsed, missing layernorm before head, …).
    Record it with:
        curl -sS -X POST $ARUI_INGEST_URL/api/preflight/uniform_init \
            -H "Authorization: Bearer $ARUI_INGEST_TOKEN" \
            -H "Content-Type: application/json" \
            -d '{"evidence":"init_probe.py: per-class prob within 0.5% of 1/1000; entropy 6.905 vs log(1000)=6.908","entropy":6.905}'

  STEP 3 — Council bless (proves the code is doing the RIGHT
  research, per the project Purpose). Only AFTER steps 1+2 are
  recorded, request the council review:
        curl -sS -X POST $ARUI_INGEST_URL/api/council/bless \
            -H "Authorization: Bearer $ARUI_INGEST_TOKEN"
    Poll every ~10 seconds until status is decided:
        curl -sS $ARUI_INGEST_URL/api/council/bless/status
       Possible values:
         - "pending"      → review in flight; keep polling
         - "approved"     → training runs are now unlocked
         - "rejected"     → STOP; read the `blockers` list, fix every
                            blocker in the code. After any non-trivial
                            edit you MUST re-run steps 1 + 2 + 3
                            (they are stale once the code changes).
         - "not_requested" → no review running yet; go back to step 3
         - "blocked_on_preflight" → step 1 or 2 missing/stale; redo
                            them, then re-request bless.

## When code changes happen — RESET the preflight

Any time you make a significant edit (rewriting train.py, swapping
the model class, changing the loss, switching dataset), mark the
code as changed:
    curl -sS -X POST $ARUI_INGEST_URL/api/preflight/code_changed \
        -H "Authorization: Bearer $ARUI_INGEST_TOKEN" \
        -d '{"reason":"rewrote train.py to use AdamW + cosine"}'
This bumps `preflight_changed_at`. Any preflight step recorded
BEFORE that timestamp is treated as stale by the bless gate. So
after a significant code change you MUST redo steps 1 + 2 + 3.

## What about _probe / _smoke runs?

The static-overfit smoke script ITSELF is fine to run as a `_probe`
or `_smoke` (names starting with those bypass the bless gate). That
is HOW you produce the evidence for step 1. Use them freely. They
do NOT count as "real runs".

## Hard gate

Until ALL THREE pass, POST /api/track/run returns HTTP 423 for any
non-_probe/_smoke run and your `arui.init` will fail. Don't try to
bypass this — fix the actual bug.

When the council rejects, the JSON shape is:
  {"status":"rejected",
   "summary":"...",
   "blockers":["[gemini] train.py never sets arui.summary['__METRIC__']"],
   "suggestions":[...]}
Treat each blocker as a `MUST FIX` ticket. Edit the files, mark code
changed via /api/preflight/code_changed, redo steps 1 + 2, then call
POST /api/council/bless again. Do not start runs until "approved".


# Logging — REQUIRED for every experiment
Every experiment MUST log via the `arui` SDK:
  import arui
  arui.init(project=..., name=<run_id>, config={
      "what": "<one line: what this run changes vs the baseline>",
      "why":  "<one line: the hypothesis — why it might help>",
      ...your hyperparameters...})
  arui.log({...}, step=...)
  arui.summary["__METRIC__"] = <final value>     # <-- REQUIRED, exact key
  arui.finish()
The dashboard resolves each run's headline score from arui.summary["__METRIC__"].
If you do not set that exact key the run cannot be scored. ALWAYS include the
"what" and "why" keys in config — the dashboard shows them in the run drawer so
the research stays readable. ARUI_INGEST_URL, ARUI_PROJECT, and
ARUI_INGEST_TOKEN are ALL already in your env — the SDK uses them
automatically. For ad-hoc curl calls to the dashboard API, just use
`-H "Authorization: Bearer $ARUI_INGEST_TOKEN"` (do NOT query the DB
to find the passcode — it is already in $ARUI_INGEST_TOKEN).

For EVAL-only runs (no training loop — e.g. evaluating an ensemble), STILL
call arui.log per example or per ensemble member with the running cumulative
metric, e.g. `arui.log({"__METRIC__": cumulative_acc}, step=i)` after each
example. That way every run has a curve in the dashboard, not just a single
final point — the user can see eval progress live.

# REQUIRED PLOTS — MUST LOG
Every training run MUST log these seven default keys so the dashboard's
run drawer "All plots" section is populated and runs are comparable
across experiments:

    val_loss, val_acc, lr, train_loss, train_acc,
    time_per_step, samples_per_sec

The arui SDK ships a one-line helper that ALWAYS emits all seven —
auto-computing `lr` from the optimizer, `time_per_step` from a stopwatch,
and `samples_per_sec` from `batch_size / time_per_step`:

    import arui
    arui.log_defaults(model=model, optimizer=optimizer, step=step,
                      batch_size=batch_size,
                      train_loss=batch_loss,
                      val_loss=v_loss, val_acc=v_acc,
                      train_acc=t_acc,
                      extra={"my_metric": ...})    # extras + overrides

Call this at EVERY training step (or at least every eval). If a metric
truly doesn't apply for this run (e.g. no validation set on an eval-only
run), pass it as `None` — the SDK records a NaN gap so the key still
shows up in the drawer. NEVER just skip the key — a missing default key
is a defect: the backend audits this at run-finish and emits an
Event-severity-warning "Run did not log required default metric X" for
every default that was never seen. See $ARUI_REPO/arui/__init__.py for
the full helper signature.

# The directives queue — your ONLY source of work
Your sole function is to process `directives.jsonl` in priority order.
Each line is a directive object: {id, type, priority, what, acceptance,
status, blocked_by?, idea_class}. Read it from
`$workspace/directives.jsonl` and via `curl -s $ARUI_INGEST_URL/api/directives`.

Types and what you do with them:
  - BLOCKER_INFRA / BLOCKER_EVAL: STOP all SCIENCE work. Implement this
    directive, write a CPU-only smoke test that demonstrates `acceptance`
    is met, then mark it `done` via
    `curl -X POST $ARUI_INGEST_URL/api/directives/<id>/done -d '{"evidence":"..."}'`.
  - SCIENCE: ONLY launch if it is the top open directive AND it has no
    unmet blocked_by. Otherwise, idle.
  - HALT: stop everything, write a status report to STATUS.md, do not
    launch any new runs until a human marks it resolved.
  - SEED_REPLICATE: launching the same hash again is allowed (bypasses
    the duplicate killer). Use `seed_replicate: true` in config OR a
    run_name starting with `seed_`.

You are FORBIDDEN from launching modelling runs while any BLOCKER_*
directive is open. An idle GPU with an unresolved BLOCKER is the correct
state. Idle GPUs are not failure; ignored blockers are. The
/api/track/run endpoint will return HTTP 423 with reason
`open_blocker_directive` until the blocker is done.

`ideas.md` is preserved as a read-only render layer for the dashboard's
existing widgets. `directives.jsonl` is AUTHORITATIVE — when the two
disagree, the JSONL wins.

# Council reviews — IMPORTANT
After every batch of runs finishes, an external LLM strategic council
reviews the whole trajectory and writes new directives to
directives.jsonl via `directives_upsert` and may close stale ones via
`directives_close`. Trust the council's ordering: when picking the next
experiment, run the TOP open SCIENCE directive (highest priority + no
unmet blocked_by), not your favourite. The strategic council is
stateful: it sees its prior verdicts and the count of consecutive reviews
where you DID NOT implement its top directive. After 3 consecutive
unimplemented reviews it emits an `ESCALATION_HALT` verdict and the
system goes into a HARD HALT — your /api/track/run calls will 423 with
`reason: research_halted` and a human PI has to lift it. Don't let that
happen — implement the top BLOCKER first.

# Run each experiment in its own tmux session
Launch every training run in a dedicated, detached tmux session named after the
run, so it shows up live in the dashboard's Sessions tab:
  tmux new-session -d -s <run_id> "cd $PWD && python train.py ... 2>&1"
Use the SAME <run_id> for the tmux session name and the arui run name.

# INFRA TMUX SESSIONS — NEVER TOUCH
The dashboard infrastructure runs in these reserved tmux sessions:
  arui     — the FastAPI backend (host:port 127.0.0.1:8000 / the
             public cloudflare tunnel terminates here)
  arui-cf  — the cloudflare tunnel itself (gives Francois the
             https://*.trycloudflare.com URL he opens in his browser)
  agent    — YOU. The tmux session this Claude process lives in.
  author   — the Author agent (paper-mode counterpart)

NEVER run `tmux kill-session -t arui`, `tmux kill-session -t arui-cf`,
`tmux kill-session -t agent`, `tmux kill-session -t author`, OR any
`pkill -f backend.main`, `pkill -f cloudflared` style commands. These
are SHARED INFRASTRUCTURE; killing them blackouts the dashboard the
researcher uses to watch and steer you, and the trycloudflare tunnel
gets a NEW random URL when it respawns so the researcher's bookmark
breaks.

If you find a bug in the dashboard code (in $ARUI_REPO) that needs
the backend to reload to take effect, DO NOT restart it yourself.
Instead: write a one-paragraph diagnosis to a file named
`AGENT_NEEDS_RESTART.md` in your workspace describing the file, the
line, and the fix. The PI agent + Francois will see it and apply the
restart at a moment that won't blast-radius into shared infra.

# Use the GPUs efficiently — but only on legal work
When you do have legal work (i.e. the top open directive is SCIENCE with
no unmet blocked_by AND no BLOCKER is open AND no HALT is set), run
`nvidia-smi` and launch the next SCIENCE directive on each free GPU. If
the only thing in directives.jsonl is a BLOCKER, an idle GPU is the
CORRECT state — implement the blocker first and ship the smoke test.
The earlier prompt's "never let a GPU sit idle" instruction was
explicitly REMOVED on 2026-06-04 because it caused a 40-batch loop where
the agent kept burning GPU on duplicates while ignoring the council. Do
not reintroduce it.

# Check your results — close the loop
You can and SHOULD read your own results back from the dashboard API:
  curl -s $ARUI_INGEST_URL/api/project          # baseline_metric, best_metric
  curl -s $ARUI_INGEST_URL/api/runs             # every run: status + headline
  curl -s $ARUI_INGEST_URL/api/runs/<run_id>/metrics
After each batch of experiments, query these, see which runs were kept vs
crashed and which beat the baseline, and let that evidence steer what you try
next. A run with status "crashed" diverged or logged no metric — investigate
and fix it before moving on.

# Files and code
All your research code lives in this directory; you have full read/write access
to it. The autoresearcherUI tracking system and the `arui` SDK source live at
$ARUI_REPO — read $ARUI_REPO/arui/__init__.py for SDK details.
"""


def _setup_prompt(cfg: dict) -> str:
    """Build the agent's brief. The 'meta' instructions (logging rules, GPU
    saturation, ideas.md format, …) come from cfg['agent_instructions'] if the
    user customised them in onboarding, otherwise DEFAULT_AGENT_INSTRUCTIONS.
    The project-specific fields (purpose, baseline, eval, seed ideas) always
    come from the user's onboarding answers."""
    metric = cfg.get('metric', 'val_loss')
    instructions = (cfg.get('agent_instructions')
                    or DEFAULT_AGENT_INSTRUCTIONS).replace("__METRIC__", metric)
    kill_policy = (cfg.get('kill_criteria') or '1 hour').strip() or '1 hour'
    return f"""You are the Principal Researcher for an autonomous ML research project.

# Purpose
{cfg.get('purpose', '')}

# Baseline method
{cfg.get('baseline', '')}

# Evaluation
{cfg.get('eval', '')}
Validation metric: {metric}.

# Seed ideas
{cfg.get('seed_ideas', '')}

# Run kill criteria (user policy)
The researcher has set this kill policy for every training run:
    `{kill_policy}`
The exact text is also in your env as `$ARUI_KILL_CRITERIA`. The
autoresearcherUI monitor auto-kills any run that violates the policy
and marks it `crashed`, so plan every experiment to fit within it —
e.g. a `1 hour` policy means a 10-hour training schedule will be killed
early, so prefer short, decisive experiments. Read `$ARUI_KILL_CRITERIA`
when designing each new run so you respect the researcher's budget.

{instructions}"""


def _resume_prompt(cfg: dict) -> str:
    metric = cfg.get('metric', 'val_loss')
    return f"""You are RESUMING an autonomous ML research project after a
server move. The full prior state — code, logs, checkpoints, databases — has
been restored into this directory.

# Purpose
{cfg.get('purpose', '')}

Validation metric: {metric}.

# You are RESUMING — do NOT restart from scratch
First reconstruct where the research stands:
  - read program.md, ideas.md and agent.log in this directory;
  - curl -s $ARUI_INGEST_URL/api/runs — every completed run, its status
    (kept/crashed) and headline_metric;
  - curl -s $ARUI_INGEST_URL/api/project — baseline_metric and best_metric.
Then write a short status summary: what is done, the best result so far, which
runs were interrupted by the move, and what to try next.

Runs that were mid-flight when the server was archived are now marked
"crashed". If such a run has a usable checkpoint in ckpts/, continue it as a
NEW run rather than editing the old one.

# Then continue the research
Work the ideas.md queue and keep going. Every experiment logs via the arui SDK
and MUST set arui.summary["{metric}"] before arui.finish(). Launch each run in
its own tmux session named after the run id. SATURATE the GPUs — run
`nvidia-smi` and keep every idle GPU running an experiment at all times; the
instant one frees up, launch the next idea on it. Do not stop — keep
researching.
"""


def claude_binary_present() -> bool:
    """True iff a `claude` binary is on the PATH. False means setup.sh
    didn't install Claude Code yet; the dashboard surfaces this as a
    specific error in the boot overlay instead of a generic timeout."""
    import shutil
    return shutil.which("claude") is not None


def start_real(cfg: dict, resume: bool = False) -> RealAgent:
    """Create (or, on resume, reuse) the project and launch the agent."""
    global _agent
    name = (cfg.get("repo_name") or "research").strip() or "research"
    metric = (cfg.get("metric") or "val_loss").strip()
    # Fail loudly + persistently if Claude Code is missing on the node.
    # Previously this would spawn `claude --dangerously-skip-permissions`,
    # the shell would 127-exit, tmux would die, and the boot overlay would
    # time out with the unhelpful "agent never started" message.
    if (not os.environ.get("ARUI_CLAUDE_BIN")) and not claude_binary_present():
        try:
            from .bus import bus
            from .db import SessionLocal as _SL
            from .models import Event as _Ev
            db = _SL()
            try:
                ev = _Ev(id="ev-" + os.urandom(4).hex(),
                         type="claude_code_missing",
                         severity="critical", actor="system",
                         message="The `claude` binary isn't installed on "
                                 "this node. The Research Agent can't start. "
                                 "Fix with: "
                                 "`npm install -g @anthropic-ai/claude-code` "
                                 "(or re-run setup.sh after `git pull`).",
                         created_at=_iso())
                db.add(ev)
                db.commit()
                try: bus.publish("events", "event", ev.dict())
                except Exception: pass
            finally:
                db.close()
        except Exception as e:                          # noqa: BLE001
            print(f"[realrun] missing-claude event failed: {e}", flush=True)
        print("[realrun] claude binary not on PATH — agent will not start",
              flush=True)
        # Don't spawn the tmux session; the frontend will see no agent
        # session and surface the error overlay with the precise reason.
        return None  # type: ignore[return-value]

    db = SessionLocal()
    proj = db.query(Project).first()
    if not proj:
        db.add(Project(id="proj-" + name, name=name,
                       purpose=cfg.get("purpose", ""),
                       validation_metric=metric,
                       metric_direction=_direction(metric),
                       status="running", gpu_count=0, created_at=_iso()))
    else:
        proj.status = "running"
    db.add(Event(id="ev-" + os.urandom(4).hex(), type="run_started",
                 severity="info", actor="system",
                 message=(f"Resumed the research agent for '{name}' after a "
                          f"server move." if resume else
                          f"Launched the autonomous research agent for "
                          f"'{name}'."), created_at=_iso()))
    db.commit()
    db.close()

    agent_cmd = None
    cb = os.environ.get("ARUI_CLAUDE_BIN")     # test hook -> the mock agent
    if cb:
        agent_cmd = shlex.split(cb)

    workspace = os.path.join(DATA_DIR, "workspace", name)
    _agent = RealAgent(
        workspace=workspace, project_name=name,
        ingest_url=f"http://127.0.0.1:{PORT}", repo_root=str(ROOT),
        agent_cmd=agent_cmd, anthropic_key=cfg.get("claude_token", ""),
        setup_prompt=_resume_prompt(cfg) if resume else _setup_prompt(cfg),
        kill_criteria=(cfg.get("kill_criteria") or "1 hour").strip()
                       or "1 hour")
    _agent.start()
    return _agent


def active() -> RealAgent | None:
    return _agent


def stop() -> None:
    """Kill the agent's tmux session (used by /api/reset)."""
    global _agent
    if _agent is not None:
        subprocess.run(["tmux", "kill-session", "-t", _agent.session],
                       capture_output=True)
    _agent = None
