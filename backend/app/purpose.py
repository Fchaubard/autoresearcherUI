"""Shared PURPOSE ANCHOR.

The researcher, the PI, the author, and the council all drift off the research
purpose + seed ideas over a long run. This builds one canonical re-anchor block
from the onboarding config (purpose, seed ideas, operator kill/rules) plus any
open ``d-interrupt-focus`` directive, so every prompt surface can re-inject the
same reminder every cycle and the whole system stays on the rails.
"""
from __future__ import annotations


def _onboarding() -> dict:
    from .db import SessionLocal
    from .models import Setting
    db = SessionLocal()
    try:
        row = db.query(Setting).filter(Setting.key == "onboarding").first()
        return dict(row.value) if row and isinstance(row.value, dict) else {}
    finally:
        db.close()


def anchor_block(header: str = "RESEARCH PURPOSE — re-anchor every cycle",
                 include_interrupt: bool = True) -> str:
    """A compact, prominent re-anchor block. Empty string if nothing is set."""
    cfg = _onboarding()
    purpose = (cfg.get("purpose") or "").strip()
    seeds = (cfg.get("seed_ideas") or "").strip()
    rules = (cfg.get("kill_criteria") or "").strip()
    if not (purpose or seeds):
        return ""
    parts = [f"# {header}",
             "Stay strictly on this PURPOSE and these SEED IDEAS and honour the "
             "operator's rules. If any idea, run, or task does not serve them, "
             "drop it. Re-read this before every decision.",
             "", "PURPOSE:", purpose or "(none set)"]
    if seeds:
        parts += ["", "SEED IDEAS:", seeds]
    if rules:
        parts += ["", "OPERATOR RULES / KILL POLICY:", rules]
    if include_interrupt:
        try:
            from . import directives
            d = directives.get("d-interrupt-focus")
            if d and d.get("status", "open") == "open" and d.get("what"):
                parts += ["", "ACTIVE OPERATOR INTERRUPT FOCUS (highest "
                          "priority):", str(d["what"])[:1500]]
        except Exception:                                  # noqa: BLE001
            pass
    return "\n".join(parts)
