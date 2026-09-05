from __future__ import annotations

import io
import urllib.error


def test_launcher_streams_output_and_reports_exact_exit(tmp_path, monkeypatch):
    from arui import launcher
    reports = []

    class Child:
        pid = 123456789
        stdout = io.BytesIO(b"line one\nline two\n")
        def wait(self):
            return 23

    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *a, **k: Child())
    monkeypatch.setattr(launcher, "_report",
                        lambda run_id, code: reports.append((run_id, code)) or True)
    log = tmp_path / "run.log"
    assert launcher.run(["fake-command"], "run-1", str(log)) == 23
    assert log.read_bytes() == b"line one\nline two\n"
    assert reports == [("run-1", 23)]


def test_exit_report_survives_backend_reload_longer_than_old_window(
        monkeypatch):
    """A completed run must keep its tmux wrapper alive while a realistically
    slow backend restart releases DuckDB and begins accepting callbacks."""
    from arui import launcher
    calls = []
    clock = [0.0]

    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): return False

    def slow_reload(*args, **kwargs):
        calls.append(clock[0])
        if clock[0] < 20:
            raise urllib.error.URLError("backend reloading")
        return Response()

    monkeypatch.setattr(launcher.urllib.request, "urlopen", slow_reload)
    monkeypatch.setattr(launcher.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(launcher.time, "sleep",
                        lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setenv("ARUI_EXIT_REPORT_RETRY_SEC", "60")
    assert launcher._report("finished-run", 0) is True
    assert calls[-1] >= 20


def test_exit_report_is_bounded_when_backend_stays_down(monkeypatch):
    from arui import launcher
    clock = [0.0]
    monkeypatch.setattr(
        launcher.urllib.request, "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(
            urllib.error.URLError("still down")))
    monkeypatch.setattr(launcher.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(launcher.time, "sleep",
                        lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setenv("ARUI_EXIT_REPORT_RETRY_SEC", "20")
    assert launcher._report("finished-run", 0) is False
    assert clock[0] >= 20


def test_exit_report_stops_retrying_permanent_unknown_run(monkeypatch):
    from arui import launcher
    clock = [0.0]

    def unknown(req, **kwargs):
        raise urllib.error.HTTPError(req.full_url, 404, "unknown run", {}, None)

    monkeypatch.setattr(launcher.urllib.request, "urlopen", unknown)
    monkeypatch.setattr(launcher.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(launcher.time, "sleep",
                        lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setenv("ARUI_EXIT_REPORT_RETRY_SEC", "300")
    assert launcher._report("untracked-probe", 0) is False
    assert 10 <= clock[0] < 30


def test_sdk_post_retries_transient_transport_failure(monkeypatch):
    import arui
    calls = []

    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return b'{"ok": true}'

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) < 3:
            raise urllib.error.URLError("backend reloading")
        return Response()

    monkeypatch.setattr(arui.urllib.request, "urlopen", flaky)
    monkeypatch.setattr(arui.time, "sleep", lambda *_: None)
    assert arui._post("/api/test", {"x": 1}) == {"ok": True}
    assert len(calls) == 3


def test_sdk_retains_unacknowledged_metric_batch(monkeypatch):
    import arui
    attempts = []

    def flaky(path, payload):
        attempts.append(list(payload.get("points", [])))
        if len(attempts) == 1:
            raise OSError("backend unavailable")
        return {"ok": True}

    monkeypatch.setattr(arui, "_post", flaky)
    run = arui.Run("p", "n", {}, "n")
    points = [{"key": "score", "step": 1, "value": 2.0}]
    assert run._send(points) is False
    assert run._send(points) is True
    assert attempts == [points, points]
    run._stop.set()
    run._t.join(timeout=2)


def test_arun_delegates_to_supervised_launcher():
    from backend.app.config import ROOT
    source = (ROOT / "bin" / "arun").read_text()
    assert "-m arui.launcher" in source
    assert ' -- "$@"' in source
    # Arguments are passed as an argv array, not evaluated as shell syntax.
    # Valid program arguments such as Python's ``x > 0`` must not be rejected
    # by the obsolete pre-launch redirection substring scanner.
    assert 'do NOT put a' not in source
