"""A LaTeX build that still has undefined references / missing citations is
NOT a successful compile -- it would ship ?? / [?] placeholders.
"""
from backend.app.paper_compile import compile_blockers


def test_undefined_references_block():
    log = "LaTeX Warning: There were undefined references.\nOutput written."
    assert "undefined references" in compile_blockers(log)


def test_undefined_citation_blocks():
    log = "LaTeX Warning: Citation `smith2020' on page 3 undefined on input line 5."
    assert "undefined citation" in compile_blockers(log)


def test_undefined_reference_blocks():
    log = "LaTeX Warning: Reference `fig:headline' on page 2 undefined."
    assert "undefined reference" in compile_blockers(log)


def test_missing_bbl_blocks():
    assert "bibliography missing" in compile_blockers(
        "I couldn't open database file refs.bbl")


def test_fatal_error_blocks():
    assert "fatal LaTeX error" in compile_blockers(
        "! LaTeX Error: File `neurips_2026.sty' not found.")


def test_clean_log_has_no_blockers():
    log = ("This is pdfTeX. Output written on build/main.pdf (8 pages).\n"
           "Transcript written on build/main.log.")
    assert compile_blockers(log) == []
