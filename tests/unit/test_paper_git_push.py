"""commit_paper_changes commits code + latex/ into the ENCLOSING project repo
(no nested repo) and, when a push token exists, onto a dedicated branch
(autoresearch/<project>). Push itself is best-effort and not exercised here.
"""
import subprocess


def _git(root, *a):
    subprocess.run(["git", "-C", str(root), *a], capture_output=True)


def _mk_repo(tmp_path, name):
    root = tmp_path / name
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "code.py").write_text("x = 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    latex = root / "latex"
    latex.mkdir()
    (latex / "main.tex").write_text("hello")
    return root, latex


def test_push_branch_is_current_branch(arui_env, tmp_path):
    # dedicated project repo -> push to its own current branch (e.g. main)
    from backend.app import paper
    root, _ = _mk_repo(tmp_path, "ddd")
    subprocess.run(["git", "-C", str(root), "checkout", "-q", "-B", "main"],
                   capture_output=True)
    assert paper._push_branch(root) == "main"


def test_commit_into_enclosing_project_repo(arui_env, tmp_path, monkeypatch):
    from backend.app import paper
    monkeypatch.delenv("ARUI_GIT_PUSH_TOKEN", raising=False)
    root, latex = _mk_repo(tmp_path, "proj")
    sha = paper.commit_paper_changes(latex, "author: add main")
    assert sha
    # latex/ is tracked by the PROJECT repo, not its own nested repo
    assert not (latex / ".git").exists()
    tracked = subprocess.run(["git", "-C", str(root), "ls-files"],
                             capture_output=True, text=True).stdout
    assert "latex/main.tex" in tracked


def test_no_token_stays_on_current_branch(arui_env, tmp_path, monkeypatch):
    from backend.app import paper
    monkeypatch.delenv("ARUI_GIT_PUSH_TOKEN", raising=False)
    root, latex = _mk_repo(tmp_path, "p2")
    paper.commit_paper_changes(latex, "m")
    br = subprocess.run(["git", "-C", str(root), "rev-parse",
                         "--abbrev-ref", "HEAD"],
                        capture_output=True, text=True).stdout.strip()
    assert not br.startswith("autoresearch/")
