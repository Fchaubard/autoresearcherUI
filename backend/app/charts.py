"""Server-side chart rendering for notification emails (matplotlib / Agg).

Produces PNG bytes embedded inline in the HTML emails — the progress chart of
the headline metric, and training-loss curves for recent runs.
"""
from __future__ import annotations

import io

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt          # noqa: E402

from . import metrics                    # noqa: E402
from .db import SessionLocal             # noqa: E402
from .models import Project, Run         # noqa: E402

_BG = "#0B0D10"
_FG = "#E6E8EB"
_GRID = "#23272E"
_ACCENT = "#6366F1"
_OK = "#34D399"
_MUTED = "#7a818b"


def _style(fig, ax) -> None:
    fig.patch.set_facecolor(_BG)
    ax.set_facecolor(_BG)
    for s in ax.spines.values():
        s.set_color(_GRID)
    ax.tick_params(colors=_MUTED, labelsize=8)
    ax.grid(True, color=_GRID, linewidth=0.6, alpha=0.7)
    ax.xaxis.label.set_color(_MUTED)
    ax.yaxis.label.set_color(_MUTED)


def _png(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=_BG, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def progress_png() -> bytes | None:
    """Headline-metric progress: every experiment + the running-best frontier."""
    db = SessionLocal()
    try:
        proj = db.query(Project).first()
        if not proj:
            return None
        runs = [r for r in db.query(Run).all()
                if r.headline_metric is not None and r.status != "crashed"]
    finally:
        db.close()
    runs.sort(key=lambda r: r.created_at or "")
    if not runs:
        return None
    maximize = proj.metric_direction == "maximize"
    base = next((r.headline_metric for r in runs if r.is_baseline), None)
    xs = list(range(len(runs)))
    ys = [r.headline_metric for r in runs]
    front, best = [], None
    for v in ys:
        if best is None or (v > best if maximize else v < best):
            best = v
        front.append(best)
    fig, ax = plt.subplots(figsize=(7.2, 3.3), dpi=130)
    _style(fig, ax)
    ax.scatter(xs, ys, s=24, color=_MUTED, zorder=2)
    ax.plot(xs, front, color=_OK, linewidth=2.4, zorder=3)
    if base is not None:
        ax.axhline(base, color=_ACCENT, linestyle="--", linewidth=1.3,
                   alpha=0.85)
        ax.text(0, base, "  baseline", color=_ACCENT, fontsize=8,
                va="bottom", ha="left")
    ax.set_xlabel("experiment")
    ax.set_ylabel(proj.validation_metric or "metric")
    ax.set_title("Autoresearch progress", color=_FG, fontsize=11,
                 fontweight="bold", loc="left", pad=8)
    return _png(fig)


def losses_png() -> bytes | None:
    """Training-loss curves for the most recent handful of runs."""
    db = SessionLocal()
    try:
        runs = db.query(Run).all()
    finally:
        db.close()
    runs = [r for r in runs if r.status in ("kept", "running", "crashed")]
    runs.sort(key=lambda r: r.created_at or "", reverse=True)
    series = []
    for r in runs:
        for key in ("train_loss", "loss", "val_loss"):
            pts = (metrics.query(r.id, [key]) or {}).get(key) or []
            if len(pts) >= 3:
                series.append((r.run_name or r.id, pts))
                break
        if len(series) >= 6:
            break
    if not series:
        return None
    fig, ax = plt.subplots(figsize=(7.2, 2.9), dpi=130)
    _style(fig, ax)
    for name, pts in series:
        ax.plot([p[0] for p in pts], [p[1] for p in pts], linewidth=1.6,
                label=name[:22])
    ax.set_xlabel("step")
    ax.set_ylabel("loss")
    try:
        if all(p[1] > 0 for _n, s in series for p in s):
            ax.set_yscale("log")
    except Exception:
        pass
    leg = ax.legend(fontsize=7, facecolor="#14171C", edgecolor=_GRID)
    for t in leg.get_texts():
        t.set_color(_FG)
    ax.set_title("Recent training curves", color=_FG, fontsize=11,
                 fontweight="bold", loc="left", pad=8)
    return _png(fig)
