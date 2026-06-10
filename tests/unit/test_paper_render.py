"""Programmatic tables + TikZ/pgfplots-from-CSV figures. No matplotlib, ever."""
from backend.app import paper_render as R


def test_render_table_has_real_rows_and_escapes():
    tex = R.render_table(
        caption="Main results", label="tab:main",
        columns=[("Method", "method", None), ("ASR", "asr", 3)],
        rows=[{"method": "wdr_f0.05", "asr": 0.0},
              {"method": "baseline", "asr": 0.9907}])
    assert "\\begin{table}" in tex and "\\label{tab:main}" in tex
    assert "0.000" in tex and "0.991" in tex
    assert "wdr\\_f0.05" in tex            # underscore escaped


def test_bare_table_is_todo_stub():
    tex = R.bare_table(caption="c", label="tab:x",
                       columns=[("Method", "m", None), ("ASR", "asr", 3)])
    assert "TODO" in tex and "\\begin{tabular}" in tex


def test_figure_csv_joins_series_on_shared_x():
    csv = R.figure_csv([
        {"name": "wdr", "points": [(1, 0.8), (2, 0.2)]},
        {"name": "fineprune", "points": [(1, 0.9), (3, 0.5)]}])
    lines = csv.strip().splitlines()
    assert lines[0] == "x,wdr,fineprune"
    # x values are the sorted union {1,2,3}; missing cells blank
    assert lines[1].startswith("1,0.8,0.9")
    assert lines[2].startswith("2,0.2,")      # fineprune missing at x=2
    assert lines[3].startswith("3,,0.5")      # wdr missing at x=3


def test_figure_tikz_reads_csv_and_has_no_matplotlib():
    fig = R.render_figure(
        name="fig_asr", caption="ASR vs epochs", label="fig:asr",
        xlabel="epochs", ylabel="ASR",
        series=[{"name": "wdr", "points": [(1, 0.8), (2, 0.0)]}])
    tikz = fig["tikz"]
    assert "\\addplot table[x=x, y=wdr, col sep=comma]{fig_asr.csv}" in tikz
    assert "\\begin{tikzpicture}" in tikz and "\\begin{axis}" in tikz
    # the hard rule: never matplotlib / raster includes
    low = (tikz + fig["csv"]).lower()
    for banned in ("matplotlib", "pyplot", "savefig", ".png", "includegraphics"):
        assert banned not in low
    assert fig["csv_name"] == "fig_asr.csv"


def test_data_signature_changes_when_metric_changes():
    a = R.data_signature([{"id": "r1", "headline_metric": 0.0},
                          {"id": "r2", "headline_metric": 0.99}])
    same = R.data_signature([{"id": "r2", "headline_metric": 0.99},
                             {"id": "r1", "headline_metric": 0.0}])
    changed = R.data_signature([{"id": "r1", "headline_metric": 0.5},
                                {"id": "r2", "headline_metric": 0.99}])
    assert a == same            # order-independent
    assert a != changed         # a run's metric moved -> figure is stale


def test_lint_assets_flags_matplotlib_and_todo(tmp_path):
    from backend.app import paper_lint as L
    figs = tmp_path / "figures"; figs.mkdir()
    (figs / "good.tikz").write_text(
        "\\addplot table[x=x, y=a, col sep=comma]{good.csv};")
    (figs / "bad.py").write_text(
        "import matplotlib.pyplot as plt\nplt.savefig('x.png')\n")
    tabs = tmp_path / "tables"; tabs.mkdir()
    (tabs / "t.tex").write_text("\\begin{tabular}\nTODO\n\\end{tabular}")
    srcs = {x["source"] for x in L.lint_assets(tmp_path)}
    assert "figures/bad.py" in srcs      # matplotlib/savefig flagged
    assert "tables/t.tex" in srcs        # leftover TODO flagged
    assert "figures/good.tikz" not in srcs


def test_lint_assets_clean_for_tikz_csv(tmp_path):
    from backend.app import paper_lint as L
    figs = tmp_path / "figures"; figs.mkdir()
    (figs / "good.tikz").write_text(
        "\\addplot table[x=x, y=a, col sep=comma]{good.csv};")
    assert L.lint_assets(tmp_path) == []


def test_writers_create_files(tmp_path):
    R.write_table(tmp_path, "main", "\\begin{table}\\end{table}")
    assert (tmp_path / "tables" / "main.tex").exists()
    fig = R.bare_figure(name="f1", caption="c", label="fig:1",
                        xlabel="x", ylabel="y", series_names=["a"])
    R.write_figure(tmp_path, fig)
    assert (tmp_path / "figures" / "f1.tikz").exists()
    assert (tmp_path / "figures" / "f1.csv").exists()
