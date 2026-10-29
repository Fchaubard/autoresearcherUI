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


def test_paper_history_and_diff_are_scoped_inside_project_repo(arui_env, tmp_path):
    from backend.app import paper
    root, latex = _mk_repo(tmp_path, "project-history")
    _git(root, "add", "latex")
    _git(root, "commit", "-q", "-m", "paper first draft")
    first = paper._run_git(root, "rev-parse", "HEAD")
    (root / "code.py").write_text("unrelated code change\n")
    _git(root, "add", "code.py")
    _git(root, "commit", "-q", "-m", "code only")
    (latex / "main.tex").write_text("improved paper\n")
    (root / "code.py").write_text("another unrelated change\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "paper and code")
    history = paper.list_commits(latex)
    assert [entry["subject"] for entry in history] == ["paper and code", "paper first draft"]
    assert paper.list_commits(latex, limit=1) == history[:1]
    diff = paper.diff(latex, first)
    assert "diff --git a/main.tex b/main.tex" in diff
    assert "+improved paper" in diff
    assert "code.py" not in diff
    assert "unrelated" not in diff


def test_paper_snapshot_records_parent_repo_commit(arui_env, tmp_path, monkeypatch):
    from backend.app import paper
    root, latex = _mk_repo(tmp_path, "snapshot")
    _git(root, "add", "latex")
    _git(root, "commit", "-q", "-m", "paper draft")
    monkeypatch.setattr(paper, "paper_folder", lambda db=None: latex)
    assert paper.take_snapshot()["latex_commit_sha"] == paper._run_git(root, "rev-parse", "HEAD")


def test_paper_history_works_for_standalone_repo_and_git_worktree(arui_env, tmp_path):
    from backend.app import paper
    root, latex = _mk_repo(tmp_path, "parent")
    _git(root, "add", "latex")
    _git(root, "commit", "-q", "-m", "paper draft")
    worktree = tmp_path / "linked-worktree"
    _git(root, "worktree", "add", "--detach", str(worktree))
    assert (worktree / ".git").is_file()
    assert paper.list_commits(worktree / "latex")[0]["subject"] == "paper draft"
    # The old standalone paper repository format remains readable too.
    assert paper.list_commits(root)[0]["subject"] == "paper draft"


def test_paper_diff_revision_cannot_be_used_as_git_option(arui_env, tmp_path):
    from backend.app import paper
    root, latex = _mk_repo(tmp_path, "safe-diff")
    output = tmp_path / "must-not-exist"
    assert paper.diff(latex, "--output=" + str(output)) == ""
    assert not output.exists()
