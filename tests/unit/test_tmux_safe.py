"""Safe tmux helpers: name validation + protected-session guarding.

The critical property: a generic control (run-kill, subagent tooling) can never
kill the core infra sessions - above all the main research ``agent`` - by name.
"""
import backend.app.tmux_safe as ts


def test_valid_name_accepts_run_ids():
    assert ts.valid_name("pr-abc123")
    assert ts.valid_name("diff_m70=lr1e-4=seed0")   # axis sweep ids use '='
    assert ts.valid_name("autoresearch.mar5")


def test_valid_name_rejects_bad():
    assert not ts.valid_name("")
    assert not ts.valid_name("has space")
    assert not ts.valid_name("semi;colon")
    assert not ts.valid_name("-leadingdash")         # ssh/tmux option-ish
    assert not ts.valid_name("x" * 81)               # too long


def test_protected_set_covers_core_sessions():
    for core in ("agent", "author", "arui", "arui-cf", "cf"):
        assert ts.is_protected(core), core


def test_kill_refuses_protected_without_optin(monkeypatch):
    called = {"n": 0}
    def _boom(*a, **k):
        called["n"] += 1
        raise AssertionError("tmux must NOT be invoked for a protected session")
    monkeypatch.setattr(ts.subprocess, "run", _boom)
    ok, msg = ts.kill_session("agent")
    assert ok is False
    assert "protected" in msg
    assert called["n"] == 0                           # never shelled out


def test_kill_refuses_invalid_name(monkeypatch):
    monkeypatch.setattr(ts.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("should not run")))
    ok, msg = ts.kill_session("bad name!")
    assert ok is False and "invalid" in msg


def test_kill_allows_protected_with_optin(monkeypatch):
    class R:  # noqa: D401
        returncode = 0
        stderr = ""
    monkeypatch.setattr(ts.subprocess, "run", lambda *a, **k: R())
    ok, msg = ts.kill_session("agent", allow_protected=True)
    assert ok is True and "killed" in msg


def test_kill_normal_session_shells_out(monkeypatch):
    seen = {}
    class R:
        returncode = 0
        stderr = ""
    def _run(argv, **k):
        seen["argv"] = argv
        return R()
    monkeypatch.setattr(ts.subprocess, "run", _run)
    ok, _ = ts.kill_session("pr-xyz")
    assert ok is True
    assert seen["argv"][:2] == ["tmux", "kill-session"]
    assert seen["argv"][-1] == "pr-xyz"
