"""Unit tests for backend.app.paper_compile."""
from __future__ import annotations


def test_status_no_pdf(arui_env):
    from backend.app import paper_compile
    s = paper_compile.status()
    assert "pdf_exists" in s
    assert s["pdf_exists"] is False


def test_status_pdf_present(arui_env, make_project, setting_setter):
    from backend.app import paper_compile, paper
    make_project(name="myproj")
    setting_setter("onboarding", {"repo_name": "myproj"})
    folder = paper.paper_folder()
    (folder / "build").mkdir(parents=True, exist_ok=True)
    (folder / "build" / "main.pdf").write_bytes(b"%PDF-1.4 ...")
    s = paper_compile.status()
    assert s["pdf_exists"] is True


def test_is_stale_no_pdf(arui_env, make_project, setting_setter):
    from backend.app import paper_compile, paper
    make_project(name="myproj")
    setting_setter("onboarding", {"repo_name": "myproj"})
    folder = paper.paper_folder()
    # no PDF → stale
    assert paper_compile._is_stale(folder) is True


def test_is_stale_pdf_older_than_tex(arui_env, make_project, setting_setter):
    import os
    import time
    from backend.app import paper_compile, paper
    make_project(name="myproj")
    setting_setter("onboarding", {"repo_name": "myproj"})
    folder = paper.paper_folder()
    (folder / "build").mkdir(parents=True, exist_ok=True)
    pdf = folder / "build" / "main.pdf"
    pdf.write_bytes(b"pdf")
    # Backdate the PDF mtime
    old = time.time() - 3600
    os.utime(pdf, (old, old))
    (folder / "main.tex").write_text(r"\documentclass{article}\begin{document}x")
    assert paper_compile._is_stale(folder) is True


def test_is_stale_pdf_newer_than_tex(arui_env, make_project, setting_setter):
    import os
    import time
    from backend.app import paper_compile, paper
    make_project(name="myproj")
    setting_setter("onboarding", {"repo_name": "myproj"})
    folder = paper.paper_folder()
    (folder / "main.tex").write_text("x")
    # Backdate tex
    old = time.time() - 3600
    os.utime(folder / "main.tex", (old, old))
    (folder / "build").mkdir(parents=True, exist_ok=True)
    (folder / "build" / "main.pdf").write_bytes(b"pdf")
    assert paper_compile._is_stale(folder) is False


def test_needs_rebuild_no_folder(arui_env):
    from backend.app import paper_compile
    assert paper_compile.needs_rebuild() is False


def test_build_no_main_tex(arui_env, make_project, setting_setter):
    """No main.tex → build returns ok=False with explanatory log."""
    from backend.app import paper_compile, paper
    make_project(name="myproj")
    setting_setter("onboarding", {"repo_name": "myproj"})
    paper.paper_folder()
    s = paper_compile.build(force=True)
    assert s["ok"] is False
    assert "main.tex" in s["log"]


def test_build_neither_tool_available(arui_env, make_project, setting_setter,
                                        monkeypatch):
    """When latexmk + pdflatex are both missing, the build returns
    a clear error message."""
    from backend.app import paper_compile, paper
    make_project(name="myproj")
    setting_setter("onboarding", {"repo_name": "myproj"})
    folder = paper.paper_folder()
    (folder / "main.tex").write_text(
        r"\documentclass{article}\begin{document}hello\end{document}")
    monkeypatch.setattr(paper_compile, "_have_latexmk", lambda: False)
    monkeypatch.setattr(paper_compile, "_have_pdflatex", lambda: False)
    s = paper_compile.build(force=True)
    assert s["ok"] is False
    assert "latexmk" in s["log"] or "pdflatex" in s["log"]


def test_pdf_bytes_none_when_missing(arui_env):
    from backend.app import paper_compile
    assert paper_compile.pdf_bytes() is None


def test_pdf_bytes_returns_content(arui_env, make_project, setting_setter):
    from backend.app import paper_compile, paper
    make_project(name="myproj")
    setting_setter("onboarding", {"repo_name": "myproj"})
    folder = paper.paper_folder()
    (folder / "build").mkdir(parents=True, exist_ok=True)
    payload = b"%PDF-1.4 fake content"
    (folder / "build" / "main.pdf").write_bytes(payload)
    assert paper_compile.pdf_bytes() == payload
