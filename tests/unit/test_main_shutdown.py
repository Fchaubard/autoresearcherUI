"""Backend process-lifecycle contracts."""
from __future__ import annotations


def test_run_bounds_graceful_shutdown(arui_env, monkeypatch):
    import backend.main as main

    calls = []
    monkeypatch.setattr(main, "_check_port_or_die", lambda _port: None)
    monkeypatch.setattr(main.uvicorn, "run",
                        lambda *args, **kwargs: calls.append((args, kwargs)))

    main.run()

    assert calls[0][1]["timeout_graceful_shutdown"] == 10
