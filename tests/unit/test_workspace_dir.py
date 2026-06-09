"""Tests for the centralized project-workspace location helper.

The workspace bytes are STORED under WORKSPACE_DIR/<name> (kept inside data/
so archive/restore stay simple), and in a real deploy workspace_dir() also
surfaces the project as a repo-root symlink ./<name>/ for findability. In
tests ARUI_WORKSPACE_DIR is set, which both isolates storage AND disables the
repo-root symlink so we never write into the real repo.
"""


def test_workspace_dir_is_under_configured_base(arui_env):
    from backend.app import config
    p = config.workspace_dir("proj-x")
    assert p == config.WORKSPACE_DIR / "proj-x"
    assert p.is_dir()
    # in tests WORKSPACE_DIR is the tmp data/workspace, never the repo root
    assert str(arui_env) in str(p)


def test_workspace_dir_no_symlink_into_repo_during_tests(arui_env):
    import os
    from backend.app import config
    config.workspace_dir("proj-y")
    # ARUI_WORKSPACE_DIR is set in tests, so the repo-root convenience symlink
    # must be skipped — nothing gets created at the real ROOT.
    assert os.environ.get("ARUI_WORKSPACE_DIR")
    assert not (config.ROOT / "proj-y").exists()


def test_read_only_path_matches_workspace_dir(arui_env):
    from backend.app import config
    # Code that only needs the path (no creation) uses WORKSPACE_DIR / name;
    # it must agree with workspace_dir()'s location.
    assert config.WORKSPACE_DIR / "p" == config.workspace_dir("p")
