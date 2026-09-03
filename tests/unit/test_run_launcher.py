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
