"""Paper Mode core (doc 13, v3).

The high-level helpers for the paper-writing workflow. Keeps the
business logic in one place so api.py stays thin. Nothing in this
module touches research-mode state — it operates on the paper_*
tables and the paper/ folder on disk only.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import base64
import shlex
import re
import subprocess
import threading
from pathlib import Path

from .config import DATA_DIR, WORKSPACE_DIR
from .db import SessionLocal
from .models import (PaperBaseline, PaperBudgetEvent, PaperCitation,
                     PaperClaim, PaperDecision, PaperFigure, PaperMeta,
                     PaperProposal, PaperReviewSim, PaperSection,
                     PaperVersion, Project, Run, Setting)

# ── repo / paths ─────────────────────────────────────────────────────────


def _meta(db) -> PaperMeta | None:
    """Single meta row per project. Created on the first paper-mode flip."""
    return db.query(PaperMeta).first()


def paper_folder(db=None) -> Path | None:
    """Absolute path to the paper/ workspace for the current project."""
    own_db = db is None
    if own_db:
        db = SessionLocal()
    try:
        proj = db.query(Project).first()
        meta = db.query(PaperMeta).first()
        if not proj:
            return None
        sub = (meta.paper_folder if meta else None) or "latex"
        # LaTeX lives under <WORKSPACE>/<repo>/latex/  (tikz under latex/tikz/),
        # tracked inside the project repo so every edit is committed + pushed.
        cfg = db.query(Setting).filter(Setting.key == "onboarding").first()
        repo = ((cfg.value.get("repo_name") if cfg and isinstance(cfg.value, dict)
                 else None) or proj.name or "project").strip()
        p = WORKSPACE_DIR / repo / sub
        p.mkdir(parents=True, exist_ok=True)
        return p
    finally:
        if own_db:
            db.close()


def project_mode() -> str:
    """'research' (default) | 'paper'. Read from the Setting row."""
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(Setting.key == "project_mode").first()
        if row and isinstance(row.value, dict):
            return row.value.get("mode", "research") or "research"
        return "research"
    finally:
        db.close()


def set_project_mode(mode: str) -> None:
    if mode not in ("research", "paper"):
        raise ValueError(f"bad mode {mode!r}")
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(Setting.key == "project_mode").first()
        if row:
            row.value = {"mode": mode}
        else:
            db.add(Setting(key="project_mode", value={"mode": mode}))
        db.commit()
    finally:
        db.close()


def _set_onboarding_key(key: str, value) -> None:
    """Best-effort write of a single key on the onboarding Setting row."""
    from sqlalchemy.orm.attributes import flag_modified
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(Setting.key == "onboarding").first()
        if not row:
            row = Setting(key="onboarding", value={})
            db.add(row)
        cur = dict(row.value) if isinstance(row.value, dict) else {}
        cur[key] = value
        row.value = cur
        flag_modified(row, "value")
        db.commit()
    finally:
        db.close()


def enter_paper_mode(meta: dict | None = None, proposal_id: str = "",
                     reason: str = "user accepted paper proposal") -> dict:
    """Flip the project into paper mode and spin up the writing pipeline.

    This is the single source of truth for "enter paper mode": the
    POST /api/paper/enter route delegates here, AND the council's
    completion-review worker calls it directly when a research conclusion is
    APPROVED with a WRITE_PAPER recommendation (the fully-autonomous handoff).
    Idempotent — returns ``already_in_paper_mode`` without side effects if we
    are already there.

    Side effects mirror the original route exactly: write/patch PaperMeta,
    record a ModeHistory snapshot, mark the chosen proposal accepted (others
    superseded), flip project_mode, populate claims from the proposal, kill
    the research agent/coord tmux (in-flight runs finish naturally), keep PI
    enabled, switch the email cadence to daily, spawn the Author Agent +
    Paper Runner, kick off lit discovery, and seed the paper phase machine.
    """
    from .models import ModeHistory
    meta = meta or {}
    proposal_id = proposal_id or ""
    if project_mode() == "paper":
        return {"status": "already_in_paper_mode"}
    db = SessionLocal()
    try:
        m = db.query(PaperMeta).first()
        if not m:
            m = PaperMeta(
                id="pm-" + os.urandom(4).hex(),
                venue=meta.get("venue") or "NeurIPS 2026",
                style_id=meta.get("style_id") or "neurips_2025",
                deadline_iso=meta.get("deadline_iso") or "",
                anonymize=bool(meta.get("anonymize", True)),
                authors_json=meta.get("authors") or [],
                gpu_budget_hours=float(meta.get("gpu_budget_hours") or 800),
                llm_budget_daily_usd=float(
                    meta.get("llm_budget_daily_usd") or 20),
                title_preference=meta.get("title_preference") or "auto",
                phase="scaffold")
            db.add(m)
        else:
            for k, v in meta.items():
                if v is None:
                    continue
                if hasattr(m, k):
                    setattr(m, k, v)
            m.phase = "scaffold"
        db.add(ModeHistory(
            id="mh-" + os.urandom(4).hex(),
            from_mode="research", to_mode="paper",
            reason_md=reason,
            snapshot_json={"proposal_id": proposal_id}))
        now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
        if proposal_id:
            chosen = db.query(PaperProposal).filter(
                PaperProposal.id == proposal_id).first()
            if chosen:
                chosen.status = "accepted"
                chosen.accepted_at = now_iso
            for other in (db.query(PaperProposal)
                          .filter(PaperProposal.id != proposal_id)
                          .filter(PaperProposal.status.in_(
                              ("ready", "in_progress"))).all()):
                other.status = "superseded"
                other.rejected_at = now_iso
        db.commit()
    finally:
        db.close()
    set_project_mode("paper")
    try:
        from . import telemetry
        telemetry.capture("paper_mode_entered")
    except Exception:                                   # noqa: BLE001
        pass
    claims_added = populate_claims_from_proposal(proposal_id)
    # Pause the research loop so it stops launching experiments and starving
    # the Paper Runner. In-flight training runs are NOT killed.
    for sess in ("agent", "coord"):
        try:
            subprocess.run(["tmux", "kill-session", "-t", sess],
                           capture_output=True, timeout=5)
        except Exception:                               # noqa: BLE001
            pass
    _set_onboarding_key("pi_agent_enabled", True)
    # Paper mode → daily digest by default (hourly is too noisy for writing).
    try:
        db2 = SessionLocal()
        try:
            row = db2.query(Setting).filter(
                Setting.key == "onboarding").first()
            cfg2 = dict(row.value) if row and isinstance(row.value, dict) \
                else {}
            cur_cad = str(cfg2.get("cadence") or "").strip().lower()
        finally:
            db2.close()
        if cur_cad in ("", "immediate", "1h", "4h", "12h"):
            _set_onboarding_key("cadence", "24h")
            print(f"[paper] auto-switched cadence {cur_cad!r} → '24h' "
                  "for paper mode", flush=True)
    except Exception as e:                              # noqa: BLE001
        print(f"[paper] cadence auto-switch skipped: {e}", flush=True)
    # Spawn the writing pipeline.
    from . import author_agent
    from . import paper_runner
    ar = author_agent.start(proposal_id=proposal_id)
    paper_runner.start()
    threading.Thread(target=kickoff_lit_discover, daemon=True,
                     name="lit-discover-initial").start()
    try:
        from . import paper_phase as _pp
        _pp.set_phase("paper.whittle_claims", actor="system",
                      progress={"claims": {"active": claims_added},
                                "lit": {"citations": 0, "approved": 0,
                                        "pending": 0}},
                      detail={"trigger": reason})
    except Exception as e:                                  # noqa: BLE001
        print(f"[paper] phase seed failed: {e}", flush=True)
    return {"status": "entered_paper_mode", "author_agent": ar,
            "claims_added": claims_added, "runs_added": 0}


# ── git in paper/ ────────────────────────────────────────────────────────


def _run_git(folder: Path, *args: str, timeout: int = 20) -> str:
    """Run a git command inside the paper/ folder."""
    out = subprocess.run(
        ["git", "-C", str(folder), *args],
        capture_output=True, text=True, timeout=timeout)
    return out.stdout.strip() if out.returncode == 0 else ""


def _enclosing_git_root(folder: Path) -> Path | None:
    """The git repo that already contains `folder`, or None."""
    out = subprocess.run(
        ["git", "-C", str(folder), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True)
    return Path(out.stdout.strip()) if out.returncode == 0 else None


def ensure_paper_repo(folder: Path) -> None:
    """If latex/ already lives inside the project's git repo (the cloned
    research repo), do NOT create a nested repo — the project repo tracks
    latex/ so commits cover code + LaTeX together and can be pushed. Only
    init a standalone repo if latex/ is not inside any git repo."""
    folder.mkdir(parents=True, exist_ok=True)
    if _enclosing_git_root(folder) is not None:
        return
    if not (folder / ".git").exists():
        try:
            subprocess.run(["git", "init", "-q", str(folder)],
                           capture_output=True, timeout=15)
            subprocess.run(
                ["git", "-C", str(folder), "config", "user.email",
                 "author-agent@autoresearcher.local"],
                capture_output=True, timeout=5)
            subprocess.run(
                ["git", "-C", str(folder), "config", "user.name",
                 "Author Agent"],
                capture_output=True, timeout=5)
            (folder / "README.md").write_text(
                "# Paper workspace\n\nAutogenerated by autoresearcherUI.\n")
            subprocess.run(["git", "-C", str(folder), "add", "."],
                           capture_output=True, timeout=10)
            subprocess.run(
                ["git", "-C", str(folder), "commit", "-q", "-m",
                 "init: paper workspace"],
                capture_output=True, timeout=15)
        except Exception as e:
            print(f"[paper] git init failed: {e}", flush=True)


def _push_token() -> str:
    """GitHub token used to push the project. From env ARUI_GIT_PUSH_TOKEN or
    the Setting `git.push_token`. Empty disables push (local commits only)."""
    t = os.environ.get("ARUI_GIT_PUSH_TOKEN", "").strip()
    if t:
        return t
    try:
        from .models import Setting
        db = SessionLocal()
        try:
            row = (db.query(Setting)
                   .filter(Setting.key == "git.push_token").first())
            if row and isinstance(row.value, dict):
                return (row.value.get("token") or "").strip()
        finally:
            db.close()
    except Exception:                                      # noqa: BLE001
        pass
    return ""


def _push_branch(root: Path) -> str:
    # Push to the project repo's CURRENT branch. The project is its OWN
    # dedicated GitHub repo (separate from the app), so its own main is the
    # right target, no special branch needed.
    b = _run_git(root, "rev-parse", "--abbrev-ref", "HEAD")
    return b if b and b != "HEAD" else "main"


def commit_paper_changes(folder: Path, message: str,
                         author: str = "Author Agent") -> str:
    """Commit code + LaTeX into the PROJECT repo and push to origin on a
    dedicated branch (autoresearch/<project>). Returns the new SHA, or ''.

    Staging is scoped to already-tracked files (code edits) + the latex/ tree
    so we never sweep huge untracked data/checkpoint blobs into the repo.
    Push is best-effort: a missing token / non-GitHub origin / network error
    degrades to a local commit (logged), never blocks the author."""
    root = _enclosing_git_root(folder) or folder
    tok = _push_token()
    try:
        branch = _push_branch(root) if tok else ""
        if branch:
            cur = _run_git(root, "rev-parse", "--abbrev-ref", "HEAD")
            if cur != branch:
                subprocess.run(["git", "-C", str(root), "checkout", "-B",
                                branch], capture_output=True, timeout=15)
        # stage tracked-file edits (code) + the whole latex/ subtree
        subprocess.run(["git", "-C", str(root), "add", "-u"],
                       capture_output=True, timeout=20)
        try:
            rel = str(folder.resolve().relative_to(root.resolve()))
        except Exception:                                  # noqa: BLE001
            rel = str(folder)
        subprocess.run(["git", "-C", str(root), "add", "-A", "--", rel],
                       capture_output=True, timeout=20)
        subprocess.run(
            ["git", "-C", str(root), "-c", f"user.name={author}", "-c",
             "user.email=author-agent@autoresearcher.local",
             "commit", "-q", "-m", message],
            capture_output=True, timeout=20)
        sha = _run_git(root, "rev-parse", "HEAD")
        if branch:
            origin = _run_git(root, "remote", "get-url", "origin")
            if origin.startswith("https://github.com/"):
                # Authenticate via an http.extraHeader passed through the
                # environment (GIT_CONFIG_*), NOT embedded in the remote URL.
                # This keeps the token out of the URL, the process argv (ps),
                # and git's own error output.
                hdr = "Authorization: Basic " + base64.b64encode(
                    f"x-access-token:{tok}".encode()).decode()
                _env = dict(os.environ)
                _env["GIT_CONFIG_COUNT"] = "1"
                _env["GIT_CONFIG_KEY_0"] = "http.extraHeader"
                _env["GIT_CONFIG_VALUE_0"] = hdr
                out = subprocess.run(
                    ["git", "-C", str(root), "push", origin,
                     f"HEAD:{branch}"], capture_output=True, text=True,
                    timeout=90, env=_env)
                if out.returncode != 0:
                    err = (out.stderr or "")[-300:]
                    if tok:
                        err = err.replace(tok, "***")
                    print(f"[paper] push failed: {err}", flush=True)
        return sha
    except Exception as e:                                  # noqa: BLE001
        print(f"[paper] commit/push failed: {e}", flush=True)
        return ""


def list_commits(folder: Path, limit: int = 30) -> list[dict]:
    if not (folder / ".git").exists():
        return []
    raw = _run_git(folder, "log", f"-n{limit}", "--pretty=format:%H\t%an\t%at\t%s")
    out = []
    for ln in raw.splitlines():
        parts = ln.split("\t", 3)
        if len(parts) < 4:
            continue
        sha, an, at, subj = parts
        out.append({"sha": sha[:10], "full_sha": sha, "author": an,
                    "at": dt.datetime.fromtimestamp(int(at),
                                                    tz=dt.timezone.utc).isoformat(),
                    "subject": subj})
    return out


def diff(folder: Path, sha_a: str, sha_b: str = "HEAD") -> str:
    return _run_git(folder, "diff", sha_a, sha_b)


# ── snapshot / restore ────────────────────────────────────────────────────


def take_snapshot() -> dict:
    """Capture the current paper state for mode_history / paper_version."""
    db = SessionLocal()
    try:
        snap = {
            "at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "claims": [c.dict() for c in db.query(PaperClaim).all()],
            "figures": [f.dict() for f in db.query(PaperFigure).all()],
            "decisions_open": [d.dict() for d in db.query(PaperDecision)
                                .filter(PaperDecision.status == "pending").all()],
            "sections": [s.dict() for s in db.query(PaperSection).all()],
            "run_ids_paper": [r.id for r in db.query(Run).filter(
                Run.context == "paper").all()],
        }
        # the git SHA of the paper folder, if any
        folder = paper_folder(db)
        if folder and (folder / ".git").exists():
            snap["latex_commit_sha"] = _run_git(folder, "rev-parse", "HEAD")
        return snap
    finally:
        db.close()


# ── decision queue helpers ───────────────────────────────────────────────


_DECISION_KINDS_V1 = {"cite_paper", "approve_text", "add_ablation",
                      "kill_claim", "budget_overrun", "approve_figure"}


def file_decision(*, source: str, kind: str, title: str, body_md: str = "",
                  default_action: str = "approve",
                  options: list | None = None, priority: int = 0,
                  linked_claim_id: str = "", linked_figure_id: str = "",
                  linked_run_id: str = "", linked_citation_key: str = "",
                  ) -> str:
    """Create a pending decision. Returns the new decision id."""
    if kind not in _DECISION_KINDS_V1:
        print(f"[paper] unknown decision kind {kind!r} — filing anyway",
              flush=True)
    did = "pd-" + os.urandom(5).hex()
    db = SessionLocal()
    try:
        db.add(PaperDecision(
            id=did, source=source, kind=kind, title=title, body_md=body_md,
            default_action=default_action, options_json=options or [],
            priority=int(priority),
            linked_claim_id=linked_claim_id, linked_figure_id=linked_figure_id,
            linked_run_id=linked_run_id,
            linked_citation_key=linked_citation_key))
        db.commit()
    finally:
        db.close()
    try:
        from .bus import bus
        bus.publish("paper", "decision_added", {"id": did, "kind": kind})
    except Exception:
        pass
    # AUTOPILOT (operator: no human approvals / no decision queue). Auto-resolve
    # with the default action so the side-effects still apply (e.g. cite_paper
    # -> PaperCitation) but nothing ever sits pending for a human to click.
    try:
        resolve_decision(did, default_action or "approve")
    except Exception as e:                                  # noqa: BLE001
        print(f"[paper] auto-resolve decision failed: {e}", flush=True)
    return did


def resolve_decision(decision_id: str, action: str, note: str = "") -> bool:
    """Mark a decision approved/rejected/deferred. Hook side-effects per
    kind — this is where 'cite_paper' approval flips PaperCitation."""
    db = SessionLocal()
    try:
        d = db.query(PaperDecision).filter(PaperDecision.id == decision_id).first()
        if not d:
            return False
        if action not in ("approve", "reject", "defer"):
            return False
        d.status = {"approve": "approved", "reject": "rejected",
                    "defer": "deferred"}[action]
        d.resolved_at = dt.datetime.now(dt.timezone.utc).isoformat()
        d.resolution_note = note
        # Side effects per kind on approval
        if action == "approve":
            _apply_decision_side_effects(db, d)
        db.commit()
    finally:
        db.close()
    try:
        from .bus import bus
        bus.publish("paper", "decision_resolved",
                    {"id": decision_id, "action": action})
    except Exception:
        pass
    return True


def _apply_decision_side_effects(db, d: PaperDecision) -> None:
    """When the user approves a decision, propagate the change."""
    if d.kind == "cite_paper" and d.linked_citation_key:
        cit = db.query(PaperCitation).filter(
            PaperCitation.key == d.linked_citation_key).first()
        if cit:
            cit.user_approved_at = dt.datetime.now(dt.timezone.utc).isoformat()
    elif d.kind == "kill_claim" and d.linked_claim_id:
        cl = db.query(PaperClaim).filter(
            PaperClaim.id == d.linked_claim_id).first()
        if cl:
            cl.status = "killed"
            cl.killed_reason = d.resolution_note or "user approved kill"


# ── populate from proposal ────────────────────────────────────────────────


def populate_claims_from_proposal(proposal_id: str = "") -> int:
    """Convert the council's per-reviewer claims into PaperClaim rows.
    Picks the proposal by id, or the most recent ready proposal.
    Idempotent: a claim with the same normalized title is not re-added.
    Returns the number of NEW claims inserted."""
    db = SessionLocal()
    try:
        q = db.query(PaperProposal)
        p = (q.filter(PaperProposal.id == proposal_id).first()
             if proposal_id else
             q.filter(PaperProposal.status.in_(("ready", "accepted")))
              .order_by(PaperProposal.created_at.desc()).first())
        if not p:
            return 0
        existing = {(c.title or "").strip().lower()
                    for c in db.query(PaperClaim).all()}
        added = 0
        # Walk reviewer_dict → claims[].  Merge across reviewers.
        responses = p.council_responses or {}
        merged: dict[str, dict] = {}
        for rev, body in responses.items():
            if not isinstance(body, dict):
                continue
            for cl in (body.get("claims") or []):
                title = (cl.get("title") or "").strip()
                if not title or len(title) < 8:
                    continue
                key = title.lower()
                if key in merged:
                    # widen evidence — strongest wins
                    rank = {"anecdotal":1,"suggestive":2,"strong":3,"unclear":0}
                    if rank.get(cl.get("evidence_strength"),0) > \
                       rank.get(merged[key].get("evidence_strength"),0):
                        merged[key]["evidence_strength"] = cl.get(
                            "evidence_strength","unclear")
                    merged[key]["council_provenance"] += f",{rev}"
                else:
                    merged[key] = {
                        "title": title,
                        "summary_md": cl.get("summary") or "",
                        "evidence_strength":
                            cl.get("evidence_strength") or "unclear",
                        "novelty": (body.get("novelty") or "unclear"),
                        "council_provenance": rev,
                        "rationale_md": body.get("rationale_md") or "",
                    }
        for idx, (k, cl) in enumerate(merged.items()):
            if k in existing:
                continue
            cid = "pc-" + os.urandom(4).hex()
            db.add(PaperClaim(
                id=cid, idx=idx, title=cl["title"],
                summary_md=cl["summary_md"],
                evidence_strength=cl["evidence_strength"],
                novelty=cl["novelty"],
                council_provenance=cl["council_provenance"],
                rationale_md=cl["rationale_md"],
                status="active"))
            added += 1
        if added:
            db.commit()
        return added
    finally:
        db.close()


REVIEWER_SIM_THRESHOLD = 5.0     # median NeurIPS-tier simulated score to pass


def reviewer_sim_median():
    """Median 'score' across the simulated reviewer pass, or None if it has
    never run on this paper. Drives the bundle gate."""
    import statistics
    db = SessionLocal()
    try:
        scores = []
        for r in db.query(PaperReviewSim).all():
            try:
                s = json.loads(r.content_md or "{}").get("score")
                if isinstance(s, (int, float)):
                    scores.append(float(s))
            except Exception:                              # noqa: BLE001
                pass
        return statistics.median(scores) if scores else None
    finally:
        db.close()


def bundle_blockers(folder=None, waive=()) -> list[dict]:
    """Everything that must pass before the paper can be BUNDLED for
    submission. Returns a list of {gate, detail}; empty == clear to bundle.
    `waive` is a set of gate names the OPERATOR explicitly overrides.

    Gates are AUTOMATIC quality lints only (no human approval): (1) compile
    clean (no undefined refs), (2) no em-dash / AI-slop prose, (3) complete +
    consistent citations, (4) assets are TikZ/CSV not raster. The reviewer
    simulator is NOT a gate anymore — it's advisory feedback the author/PI act
    on, never a human approval the paper waits on."""
    from . import paper_lint, paper_compile
    waive = set(waive or ())
    folder = folder or paper_folder()
    out: list[dict] = []
    if not folder:
        return [{"gate": "folder", "detail": "no paper folder yet"}]
    st = paper_compile.status()
    if not st.get("ok"):
        bl = ", ".join(st.get("blockers") or
                       (["build is stale / not compiled"]
                        if not st.get("pdf_exists") else ["compile not ok"]))
        out.append({"gate": "compile", "detail": "latest build not ok: " + bl})
    pv = paper_lint.lint_paper_dir(folder)
    if pv:
        out.append({"gate": "prose", "detail": paper_lint.format_violations(pv)})
    bv = paper_lint.lint_bib(folder)
    if bv:
        out.append({"gate": "bib", "detail": paper_lint.format_violations(bv)})
    av = paper_lint.lint_assets(folder)
    if av:
        out.append({"gate": "assets", "detail": paper_lint.format_violations(av)})
    # NOTE: reviewer_sim is intentionally NOT a gate (operator: no reviewer
    # gating, no human approvals — the paper just rips). reviewer_sim_median()
    # is still computed for advisory display, but never blocks the bundle.
    return [b for b in out if b["gate"] not in waive]


def paper_ingest_env_prefix(project: str) -> str:
    """Env prefix every paper run needs so (a) `import arui` works and (b) the
    arui SDK can actually register + log to the dashboard.

    CRITICAL: ARUI_INGEST_URL must be the BASE url. The SDK appends
    ``/api/track/run`` itself (see arui/__init__.py ``_BASE`` + ``_post``);
    pointing it at ``.../api/track`` doubled the path to ``/api/track/api/
    track/run`` so paper runs silently logged NOTHING. We also forward the
    ingest token so logging keeps working once a passcode is set."""
    from .config import ROOT, PORT
    tok = ""
    try:
        from .auth import _saved_passcode
        tok = _saved_passcode()
    except Exception:                                   # noqa: BLE001
        pass
    parts = [
        f"PYTHONPATH={ROOT}:${{PYTHONPATH:-}}",
        f"ARUI_REPO={ROOT}",
        f"ARUI_INGEST_URL=http://127.0.0.1:{PORT}",
        f"ARUI_PROJECT={project}",
    ]
    if tok:
        parts.append(f"ARUI_INGEST_TOKEN={tok}")
    return " ".join(parts)


def _default_run_cmd_for_project(role: str, suffix: str, seed: int,
                                  claim_title: str) -> str:
    """Build a runnable shell command for a paper-ablation row. v1 uses
    a heuristic based on the project's program.md / claim title. The
    Author Agent can override by editing the run's config.cmd.
    Returns "" if we can't sensibly default — the caller will then file
    an add_ablation DECISION instead of a queued run, so the user can
    fill in the cmd before approval."""
    # Look at the workspace's program.md for a sample invocation.
    folder = paper_folder()
    if not folder:
        return ""
    prog = folder.parent / "program.md"
    if not prog.exists():
        return ""
    try:
        text = prog.read_text(errors="ignore").lower()
    except OSError:
        return ""
    # Heuristic: if program.md / train.py exists, default to a sensible
    # invocation. The Author Agent can override config['cmd'] per run.
    train_py = folder.parent / "train.py"
    if not train_py.exists() and "train.py" not in text:
        return ""
    title = claim_title.lower()
    mode = ("diff" if any(w in title for w in
                          ("diffusion", "ensemble", "mdm", "discrete diff"))
            else "ar")
    # Resolve the repo root so we can set PYTHONPATH the same way agent.py
    # does for the research agent — without it the project's `import arui`
    # in train.py raises ModuleNotFoundError and the run crashes immediately.
    workspace = str(folder.parent)
    return (f"cd {shlex.quote(workspace)} && "
            + paper_ingest_env_prefix(folder.parent.name) + " "
            + f"python train.py --mode {mode} "
            + f"--name pr_{suffix}_s{seed} --seed {seed}")


def queue_ablations_for_claims(default_seeds: int = 3) -> int:
    """For every active claim that has no paper_runs yet, queue a default
    set of ablation runs: 1 headline + 2 ablations × N seeds, each with
    a real shell command derived from program.md. The Paper Runner picks
    them off the queue onto idle GPUs one at a time.
    Idempotent. Returns the number of NEW Run rows inserted."""
    from .models import Run
    db = SessionLocal()
    try:
        added = 0
        for c in db.query(PaperClaim).filter(
                PaperClaim.status == "active").all():
            existing = db.query(Run).filter(
                Run.context == "paper",
                Run.paper_claim_id == c.id).count()
            if existing:
                continue
            for role, name_suffix in [("headline", "headline"),
                                       ("ablation", "ablation_a"),
                                       ("ablation", "ablation_b")]:
                for s in range(1, default_seeds + 1):
                    rid = f"pr-{c.id[-6:]}-{name_suffix}-s{s}"
                    if db.query(Run).filter(Run.id == rid).first():
                        continue
                    cmd = _default_run_cmd_for_project(
                        role, name_suffix, s, c.title)
                    db.add(Run(
                        id=rid,
                        run_name=f"{c.title[:30]} · {name_suffix} · s{s}",
                        status="queued",
                        context="paper",
                        paper_claim_id=c.id,
                        paper_role=role,
                        n_seeds=1,
                        config={"seed": s, "claim": c.title[:80],
                                "role": role, "ablation": name_suffix,
                                "cmd": cmd},
                        gpus_required=1,
                        est_time_sec=int(c.summary_md and 7200 or 3600),
                    ))
                    added += 1
        if added:
            db.commit()
        try:
            write_projections()
        except Exception:
            pass
        return added
    finally:
        db.close()


def kickoff_lit_discover() -> int:
    """One-shot call into the Lit Agent if it hasn't run yet and claims exist.
    Returns the number of cite_paper decisions filed."""
    try:
        from . import lit_agent
        # Skip if we already have some citations
        db = SessionLocal()
        try:
            n_existing = db.query(PaperCitation).count()
            n_claims = db.query(PaperClaim).filter(
                PaperClaim.status == "active").count()
        finally:
            db.close()
        if n_existing >= 5 or n_claims == 0:
            return 0
        return lit_agent.auto_discover_for_claims(max_per_claim=4)
    except Exception as e:                       # noqa: BLE001
        print(f"[paper] lit auto-discover failed: {e}", flush=True)
        return 0


# ── markdown projections ─────────────────────────────────────────────────


def render_claims_md(db) -> str:
    lines = ["# Claims\n\n",
             "| status | id | title | strength | novelty | ready |\n",
             "|--------|----|-------|----------|---------|-------|\n"]
    for c in db.query(PaperClaim).order_by(PaperClaim.idx).all():
        lines.append(
            f"| {c.status} | {c.id} | {c.title} | {c.evidence_strength}"
            f" | {c.novelty} | {'★' if c.ready else ''} |\n")
    return "".join(lines)


def render_runs_md(db) -> str:
    lines = ["# Paper runs\n\n",
             "| status | run_id | claim | figure | role | task | dataset"
             " | model | n_seeds | est_time |\n",
             "|--------|--------|-------|--------|------|------|--------"
             "|-------|---------|----------|\n"]
    rows = db.query(Run).filter(Run.context == "paper").all()
    for r in rows:
        cfg = r.config if isinstance(r.config, dict) else {}
        ds = cfg.get("dataset", "") or ""
        mdl = cfg.get("model", "") or ""
        et = (f"{r.est_time_sec//60}m" if r.est_time_sec else "-")
        lines.append(
            f"| {r.status} | {r.id} | {r.paper_claim_id or '-'} "
            f"| {r.paper_figure_id or '-'} | {r.paper_role or '-'}"
            f" | {r.task_type} | {ds} | {mdl} | {r.n_seeds} | {et} |\n")
    return "".join(lines)


def render_figures_md(db) -> str:
    lines = ["# Figures\n\n",
             "| status | fig_id | claim | kind | title | path |\n",
             "|--------|--------|-------|------|-------|------|\n"]
    for f in db.query(PaperFigure).all():
        lines.append(
            f"| {f.status} | {f.id} | {f.claim_id or '-'} | {f.kind} "
            f"| {f.title} | {f.path or '-'} |\n")
    return "".join(lines)


def write_projections() -> None:
    """Refresh the markdown projections inside paper/ for the Author Agent
    and human readers. Called from the agent's event handlers."""
    folder = paper_folder()
    if not folder:
        return
    db = SessionLocal()
    try:
        (folder / "claims.md").write_text(render_claims_md(db))
        (folder / "paper_runs.md").write_text(render_runs_md(db))
        (folder / "paper_figures.md").write_text(render_figures_md(db))
    finally:
        db.close()


# ── budget bookkeeping ───────────────────────────────────────────────────


def log_budget_event(kind: str, category: str, cost_units: float,
                     cost_usd: float = 0.0, run_id: str = "",
                     note: str = "") -> None:
    db = SessionLocal()
    try:
        db.add(PaperBudgetEvent(
            id="be-" + os.urandom(4).hex(),
            kind=kind, category=category, run_id=run_id,
            cost_units=float(cost_units), cost_usd=float(cost_usd),
            note=note))
        db.commit()
    finally:
        db.close()


def budget_summary() -> dict:
    """Aggregate GPU-hours and LLM USD spent in paper mode so far."""
    db = SessionLocal()
    try:
        gpu_h = 0.0
        llm_usd = 0.0
        for e in db.query(PaperBudgetEvent).all():
            if e.kind == "gpu":
                gpu_h += e.cost_units
            elif e.kind == "llm":
                llm_usd += e.cost_usd
        meta = _meta(db)
        return {
            "gpu_hours_used": round(gpu_h, 2),
            "gpu_hours_budget": meta.gpu_budget_hours if meta else 0,
            "llm_usd_today": round(llm_usd, 2),
            "llm_usd_daily_budget": meta.llm_budget_daily_usd if meta else 0,
        }
    finally:
        db.close()


# ── days-till-deadline ───────────────────────────────────────────────────


def days_till_deadline() -> float | None:
    # Paper mode is QUALITY-gated, not time-gated (operator decision): there is
    # no conference deadline concept. Always None so no "N days till deadline"
    # countdown / progress bar / email ever renders.
    return None
