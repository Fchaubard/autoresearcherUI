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
