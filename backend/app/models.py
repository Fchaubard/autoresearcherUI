"""SQLAlchemy ORM models - the relational metadata (doc 08 / doc 11 D2).

Metric time-series are intentionally NOT modelled here; they live in DuckDB.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import JSON, Boolean, Column, Float, Integer, String, Text

from .db import Base


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Project(Base):
    __tablename__ = "project"
    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    repo_url = Column(String, default="")
    repo_path = Column(String, default="")
    purpose = Column(Text, default="")
    validation_metric = Column(String, default="val_fid")
    metric_direction = Column(String, default="minimize")   # minimize|maximize
    time_budget_sec = Column(Integer, default=3600)
    status = Column(String, default="running")
    baseline_run_id = Column(String, default="")
    gpu_count = Column(Integer, default=0)
    created_at = Column(String, default=lambda: _now().isoformat())

    def dict(self) -> dict:
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


class Idea(Base):
    __tablename__ = "idea"
    id = Column(String, primary_key=True)
    project_id = Column(String, index=True)
    idea_id = Column(String, index=True)          # the run name / join key
    description = Column(Text, default="")
    why = Column(Text, default="")
    ev = Column(Float, default=0.0)               # expected value of improvement
    status = Column(String, default="not_implemented")
    hpps = Column(JSON, default=dict)
    results_vs_baseline = Column(Text, default="")
    analysis = Column(Text, default="")
    conclusion = Column(Text, default="")
    next_ideas = Column(Text, default="")
    manual_priority = Column(Integer, default=0)  # >0 pins above EV ordering
    source = Column(String, default="agent")      # seed|agent|human
    created_at = Column(String, default=lambda: _now().isoformat())
    started_at = Column(String, default="")
    ended_at = Column(String, default="")

    def dict(self) -> dict:
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


class Run(Base):
    __tablename__ = "run"
    id = Column(String, primary_key=True)
    project_id = Column(String, index=True)
    idea_id = Column(String, index=True)          # FK -> idea.id
    run_name = Column(String, default="")
    status = Column(String, default="queued")
    is_baseline = Column(Boolean, default=False)
    gpu_index = Column(Integer, default=-1)
    tmux_session = Column(String, default="")
    git_commit = Column(String, default="")
    config = Column(JSON, default=dict)
    headline_metric = Column(Float, default=None)
    baseline_delta = Column(Float, default=None)
    peak_vram_mb = Column(Float, default=None)
    started_at = Column(String, default="")
    ended_at = Column(String, default="")
    created_at = Column(String, default=lambda: _now().isoformat())
    # Paper-mode extensions. context='research' (default) or 'paper'. When
    # context='paper', a run is owned by the Paper Runner and links to its
    # claim/figure. integration_status tracks whether its numbers are in
    # the LaTeX yet (separate from run_status).
    context = Column(String, default="research")
    paper_claim_id = Column(String, default="")
    paper_figure_id = Column(String, default="")
    paper_role = Column(String, default="")        # main|ablation|scaling|cross|baseline
    task_type = Column(String, default="compute")  # compute|analysis|infra
    integration_status = Column(String, default="pending")  # pending|integrated|stale
    n_seeds = Column(Integer, default=1)
    depends_on = Column(JSON, default=list)        # run_ids this run blocks on
    compare_to_run_id = Column(String, default="")
    compare_to_baseline_id = Column(String, default="")
    gpus_required = Column(Integer, default=1)
    est_time_sec = Column(Integer, default=0)
    paper_seed_group = Column(String, default="")  # all seeds in a bundle share this

    def dict(self) -> dict:
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


class Gpu(Base):
    __tablename__ = "gpu"
    index = Column(Integer, primary_key=True)
    model = Column(String, default="NVIDIA A40")
    total_vram_mb = Column(Integer, default=49140)
    util_pct = Column(Float, default=0.0)
    vram_used_mb = Column(Float, default=0.0)
    temp_c = Column(Float, default=40.0)
    current_run_id = Column(String, default="")
    sampled_at = Column(String, default=lambda: _now().isoformat())

    def dict(self) -> dict:
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


class Event(Base):
    __tablename__ = "event"
    id = Column(String, primary_key=True)
    type = Column(String, default="info")
    severity = Column(String, default="info")     # info|warning|critical
    actor = Column(String, default="system")      # agent|human|system
    message = Column(Text, default="")
    run_id = Column(String, default="")
    idea_id = Column(String, default="")
    created_at = Column(String, default=lambda: _now().isoformat())

    def dict(self) -> dict:
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


class ChatMessage(Base):
    __tablename__ = "chat_message"
    id = Column(String, primary_key=True)
    role = Column(String, default="agent")        # researcher|agent
    content = Column(Text, default="")
    created_at = Column(String, default=lambda: _now().isoformat())

    def dict(self) -> dict:
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


class JournalEntry(Base):
    """The auto-written Research Journal (doc 11 D12)."""
    __tablename__ = "journal_entry"
    id = Column(String, primary_key=True)
    date = Column(String, default="")
    title = Column(String, default="")
    body = Column(Text, default="")
    created_at = Column(String, default=lambda: _now().isoformat())

    def dict(self) -> dict:
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


class Setting(Base):
    """Key/value store for non-secret runtime config, incl. the onboarding form."""
    __tablename__ = "setting"
    key = Column(String, primary_key=True)
    value = Column(JSON, default=dict)
    updated_at = Column(String, default=lambda: _now().isoformat())

    def dict(self) -> dict:
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


# ════════════════════════════════════════════════════════════════════════
# Paper Mode — additive tables (doc 13). Research mode never touches these.
# Phase A1 of the Paper Mode rollout per docs/13-paper-mode-spec-v3.md.
# ════════════════════════════════════════════════════════════════════════


class PaperMeta(Base):
    """Per-project paper-mode metadata (venue, deadline, budgets, phase).
    One row when paper mode has ever been entered for this project."""
    __tablename__ = "paper_meta"
    id = Column(String, primary_key=True)
    venue = Column(String, default="NeurIPS 2026")
    style_id = Column(String, default="neurips_2025")
    deadline_iso = Column(String, default="")
    anonymize = Column(Boolean, default=True)
    authors_json = Column(JSON, default=list)        # [{name, affiliation}]
    gpu_budget_hours = Column(Float, default=800.0)
    llm_budget_daily_usd = Column(Float, default=20.0)
    title_preference = Column(String, default="auto")
    paper_folder = Column(String, default="latex")
    # Phase: proposal | scaffold | daily | reviewer_sim | submission |
    #        rebuttal | camera_ready | archived
    phase = Column(String, default="proposal")
    created_at = Column(String, default=lambda: _now().isoformat())
    updated_at = Column(String, default=lambda: _now().isoformat())

    def dict(self) -> dict:
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


class PaperProposal(Base):
    """Async pre-flip council assessment artifact. One row per
    'are we ready to write?' check. status flows in_progress → ready."""
    __tablename__ = "paper_proposal"
    id = Column(String, primary_key=True)
    created_at = Column(String, default=lambda: _now().isoformat())
    status = Column(String, default="in_progress")    # in_progress|ready|accepted|rejected
    council_responses = Column(JSON, default=dict)    # {reviewer: {...}}
    accepted_at = Column(String, default="")
    rejected_at = Column(String, default="")

    def dict(self) -> dict:
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


class PaperClaim(Base):
    """A claim the paper makes. Coverage is computed from its linked
    figures and runs."""
    __tablename__ = "paper_claim"
    id = Column(String, primary_key=True)
    idx = Column(Integer, default=0)
    title = Column(String, default="")
    summary_md = Column(Text, default="")
    status = Column(String, default="active")     # active|killed|completed|parked
    evidence_strength = Column(String, default="unclear")   # strong|suggestive|anecdotal
    novelty = Column(String, default="unclear")             # high|medium|low|unclear
    council_provenance = Column(String, default="")
    ready = Column(Boolean, default=False)        # user toggleable "publication-ready"
    rationale_md = Column(Text, default="")
    killed_reason = Column(Text, default="")
    created_at = Column(String, default=lambda: _now().isoformat())

    def dict(self) -> dict:
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


class PaperFigure(Base):
    """A figure or table in the paper. Backed by one or more paper_run
    rows (joined via paper_figure_id on Run)."""
    __tablename__ = "paper_figure"
    id = Column(String, primary_key=True)
    claim_id = Column(String, index=True, default="")
    kind = Column(String, default="line")         # line|bar|table|scatter
    title = Column(String, default="")
    caption_md = Column(Text, default="")
    panels_json = Column(JSON, default=list)
    style_id = Column(String, default="default")
    status = Column(String, default="planned")    # planned|drafted|done|stale
    integration_status = Column(String, default="pending")
    last_render_at = Column(String, default="")
    path = Column(String, default="")             # e.g. paper/figures/fig3.pdf
    created_at = Column(String, default=lambda: _now().isoformat())

    def dict(self) -> dict:
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


class PaperBaseline(Base):
    """External baselines (cited from other papers) or in-repo reproductions."""
    __tablename__ = "paper_baseline"
    id = Column(String, primary_key=True)
    name = Column(String, default="")
    type = Column(String, default="external")     # run|external
    citation_key = Column(String, default="")
    value = Column(Float, default=None)
    variance = Column(Float, default=None)
    reproduce_status = Column(String, default="not_started")
    notes_md = Column(Text, default="")
    created_at = Column(String, default=lambda: _now().isoformat())

    def dict(self) -> dict:
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


class PaperCitation(Base):
    """Bibliography entry with provenance. Lit Agent fills this."""
    __tablename__ = "paper_citation"
    key = Column(String, primary_key=True)            # bibtex citation key
    bibtex_md = Column(Text, default="")
    source = Column(String, default="manual")         # arxiv|scholar|semantic_scholar|manual
    arxiv_id = Column(String, default="")
    semantic_scholar_id = Column(String, default="")
    doi = Column(String, default="")
    title = Column(String, default="")
    authors = Column(String, default="")
    year = Column(String, default="")
    abstract_md = Column(Text, default="")
    relevance_md = Column(Text, default="")
    cited_in_sections = Column(JSON, default=list)
    pulled_at = Column(String, default=lambda: _now().isoformat())
    user_approved_at = Column(String, default="")

    def dict(self) -> dict:
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


class PaperSection(Base):
    """Per-section health and status."""
    __tablename__ = "paper_section"
    id = Column(String, primary_key=True)
    slug = Column(String, default="")
    title = Column(String, default="")
    file_path = Column(String, default="")
    status = Column(String, default="draft")
        # draft | writing | blocked | ready | needs_review
    blocked_on_claim_id = Column(String, default="")
    blocked_on_run_id = Column(String, default="")
    last_agent_pass_at = Column(String, default="")
    last_user_edit_at = Column(String, default="")
    agent_notes_md = Column(Text, default="")

    def dict(self) -> dict:
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


class PaperVersion(Base):
    """Pinned snapshots (v0, v1-internal, v2-submitted, …)."""
    __tablename__ = "paper_version"
    id = Column(String, primary_key=True)
    label = Column(String, default="")
    created_at = Column(String, default=lambda: _now().isoformat())
    latex_commit_sha = Column(String, default="")
    snapshot_json = Column(JSON, default=dict)
    claims_summary_md = Column(Text, default="")
    headline_metrics_json = Column(JSON, default=dict)
    frozen_pdf_path = Column(String, default="")

    def dict(self) -> dict:
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


class PaperDecision(Base):
    """The central UX: a queue of items needing user input. The Author
    Agent's primary OUTPUT is filing decisions, not running runs (Paper
    Runner does that). Each row is one approve/reject/defer interaction."""
    __tablename__ = "paper_decision"
    id = Column(String, primary_key=True)
    created_at = Column(String, default=lambda: _now().isoformat())
    source = Column(String, default="agent")
        # agent | lit | council | reviewer_sim | system | user
    kind = Column(String, default="")
        # cite_paper | kill_claim | add_ablation | approve_text
        # | approve_figure | merge_section | budget_overrun | …
    title = Column(String, default="")
    body_md = Column(Text, default="")
    default_action = Column(String, default="approve")  # approve|reject
    options_json = Column(JSON, default=list)
        # [{label, action, est_cost}, ...]
    priority = Column(Integer, default=0)
    status = Column(String, default="pending")
        # pending | approved | rejected | deferred | stale
    resolved_at = Column(String, default="")
    resolution_note = Column(Text, default="")
    linked_claim_id = Column(String, default="")
    linked_figure_id = Column(String, default="")
    linked_run_id = Column(String, default="")
    linked_citation_key = Column(String, default="")
    linked_commit_sha = Column(String, default="")

    def dict(self) -> dict:
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


class PaperReviewSim(Base):
    """A council-as-reviewer simulation run on a pinned version."""
    __tablename__ = "paper_review_sim"
    id = Column(String, primary_key=True)
    version_id = Column(String, default="")
    ran_at = Column(String, default=lambda: _now().isoformat())
    model = Column(String, default="")
    content_md = Column(Text, default="")
    suggested_decisions_json = Column(JSON, default=list)

    def dict(self) -> dict:
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


class PaperBudgetEvent(Base):
    """One row per GPU-hour consumed or LLM call made in paper mode.
    Aggregated by the Today view's cost dashboard."""
    __tablename__ = "paper_budget_event"
    id = Column(String, primary_key=True)
    at = Column(String, default=lambda: _now().isoformat())
    kind = Column(String, default="gpu")        # gpu | llm
    category = Column(String, default="")        # ablation | author_agent | lit | reviewer_sim
    run_id = Column(String, default="")
    cost_units = Column(Float, default=0.0)      # gpu-hours or tokens
    cost_usd = Column(Float, default=0.0)
    note = Column(String, default="")

    def dict(self) -> dict:
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


class DatasetRegistry(Base):
    """Versioned dataset entries that feed reproducibility appendix."""
    __tablename__ = "dataset_registry"
    name = Column(String, primary_key=True)
    version = Column(String, default="")
    hash = Column(String, default="")
    license = Column(String, default="")
    preprocessing_hash = Column(String, default="")
    size_bytes = Column(Integer, default=0)
    download_url = Column(String, default="")
    prep_cmd = Column(Text, default="")
    added_at = Column(String, default=lambda: _now().isoformat())

    def dict(self) -> dict:
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


class ModeHistory(Base):
    """Every research ↔ paper transition. Captures the Paper Snapshot
    so re-entering paper mode can resume from where we left off."""
    __tablename__ = "mode_history"
    id = Column(String, primary_key=True)
    from_mode = Column(String, default="")
    to_mode = Column(String, default="")
    at = Column(String, default=lambda: _now().isoformat())
    reason_md = Column(Text, default="")
    snapshot_json = Column(JSON, default=dict)

    def dict(self) -> dict:
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}
