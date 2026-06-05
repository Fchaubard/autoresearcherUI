"""Health package — single source of truth for "is the research loop OK?".

PR 2 of the state-control rewrite (2026-06-05). Replaces the
``stuck_detector`` keyword classifier + the duplicated idle logic in
``pi.py`` with one ``service.compute()`` that returns a
``HealthSnapshot`` derived strictly from DB / GPU / metric ground truth.

The pill, the modal, the PI nudges, and the idle-GPU email all consume
the SAME snapshot — no more competing signals, no more flapping.
"""
from .schema import Phase, Issue, HealthSnapshot          # noqa: F401
from .service import compute, tick                         # noqa: F401
