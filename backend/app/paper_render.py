"""Programmatic paper assets: real LaTeX tables + TikZ/pgfplots figures whose
data lives in a sibling ``.csv``.

HARD RULE (operator): NO matplotlib, ever. A figure is a ``<name>.tikz`` file
that ``\\addplot table{<name>.csv}``s its data, plus a ``<name>.csv`` rendered
from the metric DB. This means:
  • the numbers in every plot come from real runs, not a screenshot;
  • the operator (or the agent) edits the ``.csv`` by hand to swap/refresh data
    and just recompiles, no code to touch;
  • a figure can be marked STALE automatically when its underlying runs change
    (``data_signature`` changes) after the operator approved it.

The flow starts with BARE stubs (``bare_table`` / ``bare_figure``) that compile
with TODO placeholders, so the paper has its full skeleton before any ablation
has run; the runs are then planned to fill exactly those holes.
"""
from __future__ import annotations

import hashlib

# ── LaTeX escaping ─────────────────────────────────────────────────────────
_ESC = {"&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_",
        "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}"}


def latex_escape(s) -> str:
    return "".join(_ESC.get(c, c) for c in str(s))


def fmt_num(v, nd: int = 4) -> str:
    try:
        return f"{float(v):.{nd}f}"
    except (TypeError, ValueError):
        return latex_escape(v)


# ── tables ─────────────────────────────────────────────────────────────────
def render_table(*, caption: str, label: str,
                 columns: list[tuple], rows: list[dict],
                 note: str = "") -> str:
    """columns: list of (header, key, num_decimals_or_None). rows: list of
    dicts. Returns a complete ``table`` environment (booktabs)."""
    align = "l" + "r" * (len(columns) - 1)
    head = " & ".join(latex_escape(h) for h, _k, _nd in columns) + r" \\"
    body = []
    for r in rows:
        cells = []
        for _h, k, nd in columns:
            v = r.get(k, "")
            cells.append(fmt_num(v, nd) if (nd is not None and v != "")
                         else latex_escape(v))
        body.append(" & ".join(cells) + r" \\")
    note_tex = (f"\n  \\par\\smallskip\\footnotesize {latex_escape(note)}"
                if note else "")
    return (
        "\\begin{table}[t]\n  \\centering\n"
        f"  \\caption{{{latex_escape(caption)}}}\n"
        f"  \\label{{{label}}}\n"
        f"  \\begin{{tabular}}{{{align}}}\n"
        "    \\toprule\n    " + head + "\n    \\midrule\n    "
        + "\n    ".join(body) + "\n    \\bottomrule\n"
        "  \\end{tabular}" + note_tex + "\n\\end{table}\n")


def bare_table(*, caption: str, label: str, columns: list[tuple],
               n_rows: int = 2) -> str:
    """A compile-clean TODO stub: the real header + placeholder rows, so the
    paper skeleton exists before any run has filled the numbers."""
    rows = [{k: "\\textit{TODO}" for _h, k, _nd in columns}
            for _ in range(n_rows)]
    # force string rendering (no numeric fmt) for placeholders
    cols = [(h, k, None) for h, k, _nd in columns]
    return render_table(caption=caption, label=label, columns=cols, rows=rows,
                        note="TODO: fill from runs once the ablations complete.")


# ── figures: TikZ/pgfplots that read a .csv ────────────────────────────────
def figure_csv(series: list[dict]) -> str:
    """series: [{"name": str, "points": [(x, y), ...]}]. All series are joined
    on a shared, sorted x axis. Returns CSV text: ``x,<s1>,<s2>,...``."""
    names = [s["name"] for s in series]
    xs: list = sorted({x for s in series for x, _y in s.get("points", [])})
    lookup = [{x: y for x, y in s.get("points", [])} for s in series]
    out = ["x," + ",".join(names)]
    for x in xs:
        row = [str(x)] + ["" if x not in lookup[i] else str(lookup[i][x])
                          for i in range(len(series))]
        out.append(",".join(row))
    return "\n".join(out) + "\n"


def figure_tikz(*, name: str, caption: str, label: str,
                xlabel: str, ylabel: str, series_names: list[str],
                kind: str = "line") -> str:
    """A standalone tikzpicture that ``\\addplot table``s each series from
    ``<name>.csv`` (NO matplotlib). kind: line|bar|scatter."""
    axis_opts = [f"xlabel={{{latex_escape(xlabel)}}}",
                 f"ylabel={{{latex_escape(ylabel)}}}",
                 "legend pos=north east", "grid=both",
                 "width=\\linewidth", "height=0.62\\linewidth"]
    if kind == "bar":
        axis_opts += ["ybar", "bar width=6pt"]
    plot_opt = {"line": "", "bar": "", "scatter": "[only marks]"}.get(kind, "")
    plots = []
    for s in series_names:
        plots.append(
            f"    \\addplot{plot_opt} table[x=x, y={s}, col sep=comma]"
            f"{{{name}.csv}};\n    \\addlegendentry{{{latex_escape(s)}}}")
    return (
        "% Auto-generated TikZ figure. Data lives in " + name + ".csv -- edit "
        "the CSV and recompile to refresh (data-driven, vector, no raster).\n"
        "\\begin{figure}[t]\n  \\centering\n  \\begin{tikzpicture}\n"
        "  \\begin{axis}[" + ", ".join(axis_opts) + "]\n"
        + "\n".join(plots) + "\n  \\end{axis}\n  \\end{tikzpicture}\n"
        f"  \\caption{{{latex_escape(caption)}}}\n  \\label{{{label}}}\n"
        "\\end{figure}\n")


def render_figure(*, name: str, caption: str, label: str,
                  xlabel: str, ylabel: str, series: list[dict],
                  kind: str = "line") -> dict:
    """Returns {tikz, csv, tikz_name, csv_name} for a real figure."""
    names = [s["name"] for s in series]
    return {
        "tikz_name": f"{name}.tikz", "csv_name": f"{name}.csv",
        "csv": figure_csv(series),
        "tikz": figure_tikz(name=name, caption=caption, label=label,
                            xlabel=xlabel, ylabel=ylabel,
                            series_names=names, kind=kind),
    }


def bare_figure(*, name: str, caption: str, label: str, xlabel: str,
                ylabel: str, series_names: list[str], kind: str = "line") -> dict:
    """A compile-clean TODO figure: a CSV with a single placeholder row + the
    .tikz that reads it, so the figure slot exists before runs fill it."""
    series = [{"name": s, "points": [(0, 0)]} for s in series_names]
    out = render_figure(name=name, caption="TODO: " + caption, label=label,
                        xlabel=xlabel, ylabel=ylabel, series=series, kind=kind)
    return out


# ── stale detection ────────────────────────────────────────────────────────
def data_signature(rows: list[dict],
                   keys=("id", "headline_metric")) -> str:
    """Stable hash of the (run, metric) data a figure/table depends on. If it
    changes after the operator approved the figure, the figure is STALE and
    must be re-approved (closes the 'stale figure, new numbers' hole)."""
    items = []
    for r in sorted(rows, key=lambda d: str(d.get("id", ""))):
        items.append("|".join(f"{k}={r.get(k, '')}" for k in keys))
    return hashlib.sha256("\n".join(items).encode()).hexdigest()[:16]


# ── writers ────────────────────────────────────────────────────────────────
def write_table(folder, name: str, tex: str) -> str:
    from pathlib import Path
    d = Path(folder) / "tables"
    d.mkdir(parents=True, exist_ok=True)
    p = d / (name if name.endswith(".tex") else f"{name}.tex")
    p.write_text(tex)
    return str(p)


def write_figure(folder, fig: dict) -> dict:
    from pathlib import Path
    d = Path(folder) / "tikz"          # tikz + data live under latex/tikz/
    d.mkdir(parents=True, exist_ok=True)
    (d / fig["tikz_name"]).write_text(fig["tikz"])
    (d / fig["csv_name"]).write_text(fig["csv"])
    return {"tikz": str(d / fig["tikz_name"]), "csv": str(d / fig["csv_name"])}
