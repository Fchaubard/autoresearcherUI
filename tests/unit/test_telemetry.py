"""Anonymous PostHog telemetry: opt-out logic, payload shape (no PII, no person
profiles), and that it never raises / never blocks. No network in tests.
"""
import uuid


def test_disabled_by_env(monkeypatch):
    from backend.app import telemetry
    for v in ("ARUI_TELEMETRY_DISABLED", "DO_NOT_TRACK", "CI"):
        monkeypatch.delenv(v, raising=False)
    assert telemetry.telemetry_disabled() is False
    monkeypatch.setenv("ARUI_TELEMETRY_DISABLED", "1")
    assert telemetry.telemetry_disabled() is True
    monkeypatch.setenv("ARUI_TELEMETRY_DISABLED", "0")
    monkeypatch.setenv("DO_NOT_TRACK", "true")
    assert telemetry.telemetry_disabled() is True
    monkeypatch.setenv("DO_NOT_TRACK", "0")
    monkeypatch.setenv("CI", "true")          # CI opts out automatically
    assert telemetry.telemetry_disabled() is True


def test_payload_shape_and_no_person_profile():
    from backend.app import telemetry
    p = telemetry.build_payload("command_run",
                                {"command": "train", "success": True})
    assert p["event"] == "command_run"
    assert p["api_key"].startswith("phc_")
    uuid.UUID(p["distinct_id"])               # a valid random anon id
    props = p["properties"]
    assert props["$process_person_profile"] is False
    assert props["project"] == "autoresearcherui"
    assert props["command"] == "train" and props["success"] is True
    # no PII keys leak in
    for bad in ("username", "email", "repo_path", "api_key", "stack",
                "filename", "hostname"):
        assert bad not in props


def test_distinct_id_is_fresh_each_call():
    from backend.app import telemetry
    a = telemetry.build_payload("x")["distinct_id"]
    b = telemetry.build_payload("x")["distinct_id"]
    assert a != b                              # no stable user id


def test_capture_is_noop_when_disabled(monkeypatch):
    from backend.app import telemetry
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    calls = []
    monkeypatch.setattr(telemetry, "_send", lambda *a, **k: calls.append(a))
    telemetry.capture("evt", {"x": 1})
    import time
    time.sleep(0.05)
    assert calls == []                         # never sent when disabled


def test_send_swallows_network_errors(monkeypatch):
    from backend.app import telemetry

    def _boom(*a, **k):
        raise OSError("network down")
    monkeypatch.setattr(telemetry.urllib.request, "urlopen", _boom)
    telemetry._send("evt", {})                 # must not raise
