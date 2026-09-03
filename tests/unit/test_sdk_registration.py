from __future__ import annotations

import urllib.error

import pytest


def test_init_fails_closed_when_server_rejects(monkeypatch):
    import arui
    arui._active = None

    def reject(path, payload):
        raise urllib.error.HTTPError("http://x/api/track/run", 423, "Locked",
                                    {}, None)

    monkeypatch.setattr(arui, "_post", reject)
    with pytest.raises(arui.RunRegistrationError) as exc:
        arui.init(name="blocked", config={})
    assert exc.value.status == 423
    assert arui._active is None


def test_finish_is_idempotent(monkeypatch):
    import arui
    sent = []
    monkeypatch.setattr(arui, "_post", lambda path, payload:
                        sent.append((path, payload)) or {"ok": True})
    run = arui.Run("p", "n", {}, "n")
    run.finish()
    run.finish()
    assert [p for p, _ in sent].count("/api/track/finish") == 1
