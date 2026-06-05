"""Typed data structures for the health system.

We use ``@dataclass``es with plain JSON-able primitives so the result
can be returned from a FastAPI route without any custom serialiser, and
so the unit tests can build expected values inline.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


# Mirror of ``arui.PHASES`` (keep in sync). Repeated here so backend code
# doesn't have to import the SDK package.
PHASES = (
    "bootstrap",
    "planning",
    "launching_runs",
    "watching_runs",
    "council_review",
    "idle_waiting_direction",
    "concluding",
    "complete",
    "error",
)


@dataclass(frozen=True)
class Phase:
    """Agent-reported lifecycle phase plus its timestamp + detail."""
    phase: str = "bootstrap"
    at: str = ""                 # ISO timestamp
    detail: dict = field(default_factory=dict)
    fallback_used: bool = False  # True iff phase was derived (not POSTed)


# Severity ordering — higher numbers win when the modal picks the top
# issue to surface. Keep this small.
SEV_INFO = 0
SEV_WARNING = 1
SEV_CRITICAL = 2


@dataclass
class Issue:
    """A single thing the operator should know about. The modal renders
    a list of these; each carries enough evidence + actions that the
    operator can click and act without first opening the agent pane."""
    code: str                              # snake_case key, e.g. "no_metric_flow"
    severity: int                          # SEV_INFO / WARNING / CRITICAL
    summary: str                           # one-line human-readable
    evidence: dict = field(default_factory=dict)   # JSON-able diagnostic blob
    since: str = ""                        # ISO timestamp the issue first fired
    actions: list[dict] = field(default_factory=list)
    # Each action: {label: "Kill all", method: "POST",
    #               href: "/api/runs/.../kill", body?: {...}}

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class HealthSnapshot:
    """The full health picture at one instant. ``service.compute()``
    returns this; the dashboard pill + modal both read from it."""
    phase: Phase
    summary: str                                # one-line for the pill
    issues: list[Issue] = field(default_factory=list)
    facts: dict = field(default_factory=dict)   # raw inputs (for debugging)

    def as_dict(self) -> dict:
        return {
            "phase": asdict(self.phase),
            "summary": self.summary,
            "issues": [i.as_dict() for i in self.issues],
            "facts": self.facts,
        }

    @property
    def top_severity(self) -> int:
        return max((i.severity for i in self.issues), default=SEV_INFO)
