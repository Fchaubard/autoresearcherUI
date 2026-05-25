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


def _setup_prompt(cfg: dict) -> str:
    metric = cfg.get('metric', 'val_loss')
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

# Your task
Conduct this research autonomously in this directory. Create program.md,
train.py, prepare.py and ideas.md. Run the baseline first, then explore one
idea per experiment, keeping every idle GPU busy. Do not stop — keep
researching.

# Logging — REQUIRED for every experiment
Every experiment MUST log via the `arui` SDK:
  import arui
  arui.init(project=..., name=<run_id>, config={{
      "what": "<one line: what this run changes vs the baseline>",
      "why":  "<one line: the hypothesis — why it might help>",
      ...your hyperparameters...}})
  arui.log({{...}}, step=...)
  arui.summary["{metric}"] = <final value>     # <-- REQUIRED, exact key
  arui.finish()
The dashboard resolves each run's headline score from arui.summary["{metric}"].
If you do not set that exact key the run cannot be scored. ALWAYS include the
"what" and "why" keys in config — the dashboard shows them in the run drawer so
the research stays readable. ARUI_INGEST_URL and ARUI_PROJECT are in your env.

# The ideas.md queue
Keep ideas.md as markdown tables with the columns
`| status | idea_id | what | why |`. Use status `pending` for not-yet-run
ideas and `done` once run. Maintain a healthy backlog of `pending` rows — the
dashboard surfaces every pending row as a queued experiment the researcher can
see, rerank or veto.

# Run each experiment in its own tmux session
Launch every training run in a dedicated, detached tmux session named after the
run, so it shows up live in the dashboard's Sessions tab:
  tmux new-session -d -s <run_id> "cd $PWD && python train.py ... 2>&1"
Use the SAME <run_id> for the tmux session name and the arui run name.

# Saturate the GPUs — this is critical
Run `nvidia-smi` to see every GPU on this node. At ALL times every idle GPU
must be running one of your experiments — keep as many experiments running
concurrently as there are free GPUs. The moment a run finishes, immediately
launch the next idea on that freed GPU. Before finishing any step, check
`nvidia-smi`; if any GPU is idle, start an experiment on it now. A GPU sitting
idle is wasted research — never let that happen.

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


def start_real(cfg: dict, resume: bool = False) -> RealAgent:
    """Create (or, on resume, reuse) the project and launch the agent."""
    global _agent
    name = (cfg.get("repo_name") or "research").strip() or "research"
    metric = (cfg.get("metric") or "val_loss").strip()

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
        setup_prompt=_resume_prompt(cfg) if resume else _setup_prompt(cfg))
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
