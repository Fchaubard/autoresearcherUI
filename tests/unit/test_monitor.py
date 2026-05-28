"""Unit tests for backend.app.monitor."""
from __future__ import annotations

import datetime as dt


def test_safe_name_accepts_normal_ids(arui_env):
    from backend.app.monitor import _safe_name
    assert _safe_name("run-abc_123")
    assert _safe_name("baseline")
    assert _safe_name("foo.bar")
    assert _safe_name("foo=bar")


def test_safe_name_rejects_bad_input(arui_env):
    from backend.app.monitor import _safe_name
    assert not _safe_name("")
    assert not _safe_name(None)  # type: ignore[arg-type]
    assert not _safe_name("a b")
    assert not _safe_name("a;rm -rf /")
    assert not _safe_name("a'b")
    assert not _safe_name("../etc/passwd")


def test_epoch_parses_iso(arui_env):
    from backend.app.monitor import _epoch
    iso = "2026-01-01T00:00:00+00:00"
    t = _epoch(iso)
    assert t is not None
    assert isinstance(t, float)


def test_epoch_assumes_utc_for_naive(arui_env):
    from backend.app.monitor import _epoch
    t1 = _epoch("2026-01-01T00:00:00")
    t2 = _epoch("2026-01-01T00:00:00+00:00")
    assert t1 == t2


def test_epoch_handles_none(arui_env):
    from backend.app.monitor import _epoch
    assert _epoch(None) is None
    assert _epoch("") is None
    assert _epoch("not-an-iso") is None


def test_system_stats_returns_dict_with_gpus(arui_env):
    """system_stats should always return a dict with a 'gpus' list."""
    from backend.app import monitor
    s = monitor.system_stats()
    assert isinstance(s, dict)
    assert "gpus" in s
    assert isinstance(s["gpus"], list)


def test_system_stats_includes_db_gpu_rows(arui_env, db_session):
    from backend.app import monitor
    from backend.app.models import Gpu
    db_session.add(Gpu(index=0, model="A40", util_pct=42.0, temp_c=55.0))
    db_session.add(Gpu(index=1, model="A40", util_pct=10.0, temp_c=60.0))
    db_session.commit()
    s = monitor.system_stats()
    assert len(s["gpus"]) == 2
    by_idx = {g["index"]: g for g in s["gpus"]}
    assert by_idx[0]["util_pct"] == 42.0
    assert by_idx[1]["temp_c"] == 60.0


def test_run_log_rejects_unsafe_name(arui_env, fake_subprocess):
    from backend.app import monitor
    out = monitor.run_log("bad name with space")
    assert out["alive"] is False
    assert out["text"] == ""
    assert out["lines"] == 0


def test_run_log_reads_persisted_file(arui_env, fake_subprocess, tmp_path):
    """When tmux isn't alive, the log comes from the on-disk file."""
    from backend.app import monitor
    from backend.app.config import DATA_DIR
    # Make tmux has-session return non-zero (not alive)

    class _CP:
        returncode = 1
        stdout = ""
        stderr = ""

    fake_subprocess.set_handler(lambda args, **kw: _CP())
    logs = DATA_DIR / "run_logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "myrun.log").write_text("line1\nline2\nline3\n")
    out = monitor.run_log("myrun")
    assert "line1" in out["text"]
    assert out["lines"] == 3


def test_run_log_tail_limits_lines(arui_env, fake_subprocess):
    """Tail should cap the number of returned lines."""
    from backend.app import monitor
    from backend.app.config import DATA_DIR
    logs = DATA_DIR / "run_logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "myrun.log").write_text("\n".join(f"l{i}" for i in range(1000)))

    class _CP:
        returncode = 1
        stdout = ""
        stderr = ""

    fake_subprocess.set_handler(lambda args, **kw: _CP())
    out = monitor.run_log("myrun", tail=10)
    assert out["lines"] == 1000
    assert out["shown"] == 10


def test_parse_ideas_returns_empty_when_no_workspace(arui_env, setting_setter):
    from backend.app.monitor import _parse_ideas
    setting_setter("onboarding", {"repo_name": "nope"})
    # no ideas.md exists → empty
    assert _parse_ideas({"repo_name": "nope"}) == {}


def test_parse_ideas_bullets(arui_env):
    """Bullet ideas under an idea-ish header are picked up."""
    from backend.app.monitor import _parse_ideas
    from backend.app.config import DATA_DIR
    ws = DATA_DIR / "workspace" / "myrepo"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "ideas.md").write_text(
        "# Ideas to try\n"
        "- `foo_run` — try bigger batch\n"
        "- [x] `already_done` — skip me\n"
    )
    out = _parse_ideas({"repo_name": "myrepo"})
    assert "foo_run" in out
    desc, pending = out["foo_run"]
    assert pending is True
    assert "bigger batch" in desc
    assert out["already_done"][1] is False


def test_parse_ideas_table(arui_env):
    """Markdown-table ideas with a status column are picked up."""
    from backend.app.monitor import _parse_ideas
    from backend.app.config import DATA_DIR
    ws = DATA_DIR / "workspace" / "myrepo"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "ideas.md").write_text(
        "# Plan\n\n"
        "| status | id | what |\n"
        "|--------|----|------|\n"
        "| pending | bigger_lr | try 3e-3 |\n"
        "| done | older | skip me |\n"
    )
    out = _parse_ideas({"repo_name": "myrepo"})
    assert out["bigger_lr"][1] is True
    assert out["older"][1] is False
