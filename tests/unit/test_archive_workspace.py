"""Archive must capture project workspaces now that they live at WORKSPACE_DIR
(the repo root in a real deploy) instead of data/workspace — otherwise a
pod-migration backup would silently drop the agent's code + lessons.
"""


def _mk_project(name):
    from backend.app import config
    p = config.workspace_dir(name)
    (p / "program.md").write_text("# spec\n")
    (p / "train.py").write_text("print('x')\n")
    (p / "agent.log").write_text("boot\n")
    return p


def test_project_dirs_finds_workspaces(arui_env):
    from backend.app import archive
    _mk_project("projA")
    _mk_project("projB")
    names = {d.name for d in archive._project_dirs()}
    assert {"projA", "projB"} <= names


def test_walk_files_includes_project_under_workspace_prefix(arui_env):
    from backend.app import archive
    _mk_project("projA")
    rels = {rel for rel, _sz in archive._walk_files()}
    assert "workspace/projA/program.md" in rels
    assert "workspace/projA/train.py" in rels


def test_stage_trees_symlinks_projects(arui_env):
    from backend.app import archive
    _mk_project("projA")
    archive._stage_dbs("full")           # creates _STAGE
    archive._stage_trees()
    staged = archive._STAGE / "workspace" / "projA"
    assert staged.exists()               # symlink resolves to the real project
    assert (staged / "program.md").exists()
    rels = {rel for rel, _sz in archive._staged_walk()}
    assert "workspace/projA/program.md" in rels
