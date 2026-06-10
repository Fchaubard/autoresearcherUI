"""Bib lint: every \\cite must resolve to a complete, non-placeholder entry."""
from backend.app import paper_lint as L


def _setup(tmp_path, tex, bib):
    (tmp_path / "main.tex").write_text(tex)
    (tmp_path / "refs.bib").write_text(bib)
    return L.lint_bib(tmp_path)


_GOOD = ("@article{good2024,\n  title={A Real Title},\n"
         "  author={Jane Doe and John Roe},\n  year={2024},\n"
         "  journal={NeurIPS}\n}\n")


def test_resolved_complete_citation_is_clean(tmp_path):
    assert _setup(tmp_path, r"We build on \cite{good2024}.", _GOOD) == []


def test_cited_key_with_no_entry_flagged(tmp_path):
    v = _setup(tmp_path, r"See \cite{good2024} and \cite{ghost2099}.", _GOOD)
    keys = {x["key"] for x in v}
    assert "ghost2099" in keys and "good2024" not in keys


def test_missing_required_field_flagged(tmp_path):
    bad = "@article{x2024,\n  title={T},\n  author={A}\n}\n"   # no year
    v = _setup(tmp_path, r"\cite{x2024}", bad)
    assert any("year" in x["rule"] for x in v)


def test_placeholder_field_flagged(tmp_path):
    bad = ("@article{x2024,\n  title={TODO fill in},\n"
           "  author={Author Name},\n  year={2024}\n}\n")
    v = _setup(tmp_path, r"\cite{x2024}", bad)
    assert any("placeholder" in x["rule"] for x in v)


def test_multi_key_cite_and_comment_skipped(tmp_path):
    tex = r"\citep{good2024, alsogood}"
    bib = _GOOD + ("@comment{ignore me}\n"
                   "@inproceedings{alsogood,\n title={Another},\n"
                   " author={Q. Public},\n year={2023}\n}\n")
    v = _setup(tmp_path, tex, bib)
    assert v == []
