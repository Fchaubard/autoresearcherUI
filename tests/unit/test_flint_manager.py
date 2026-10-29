"""Managed local model lifecycle, resource ownership and installation safety."""
import hashlib
import io
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest


def test_device_selection_never_reuses_a_busy_gpu(arui_env):
    from backend.app.flint_manager import choose_device, GIB
    resources = {"available_ram_bytes": 60 * GIB, "gpus": [
        {"index": 0, "busy": True, "used_mb": 0, "total_mb": 48000},
        {"index": 1, "busy": False, "used_mb": 800, "total_mb": 48000}]}
    assert choose_device(resources) == "cpu"
    resources["available_ram_bytes"] = 10 * GIB
    assert choose_device(resources) is None
    resources["gpus"][1]["used_mb"] = 0
    assert choose_device(resources) == "gpu:1"
    assert choose_device(resources, "cpu") is None


def test_managed_default_and_explicit_external_configuration(arui_env, monkeypatch):
    from backend.app.provider_config import flint_config, flint_managed, requirement_error
    assert flint_managed({})
    assert flint_config({})[:2] == ("http://127.0.0.1:18342/v1", "flint")
    assert "automatic setup" in requirement_error("flint/local")
    assert requirement_error("flint/local", check_ready=False) == ""
    assert not flint_managed({"flint_base_url": "http://localhost:8080/v1"})
    assert flint_config({"flint_mode": "external", "flint_base_url": "http://localhost:8080", "flint_model": "custom"})[:2] == ("http://localhost:8080/v1", "custom")
    with pytest.raises(ValueError, match="server URL"):
        flint_config({"flint_mode": "external"})


def test_managed_limits_cannot_exceed_the_owned_server(arui_env, monkeypatch):
    from backend.app.provider_config import flint_limits
    cfg = {"flint_mode": "managed", "flint_context_tokens": 131072,
           "flint_max_output_tokens": 32768}
    assert flint_limits(cfg) == (16384, 8192)
    cfg["flint_max_output_tokens"] = 4096
    assert flint_limits(cfg) == (16384, 4096)
    cfg["flint_mode"] = "external"
    assert flint_limits(cfg) == (131072, 4096)


def test_fresh_runtime_install_pins_source_and_bounds_build_workers(arui_env, tmp_path, monkeypatch):
    from backend.app import flint_manager as fm
    server = fm.ManagedFlint(tmp_path / "state", tmp_path / "cache")
    server.cache.mkdir()
    commands = []
    monkeypatch.setattr(fm.shutil, "which", lambda name: None if name == "nvcc" else "/usr/bin/" + name)
    monkeypatch.setattr(server, "_run", lambda command, **kwargs: commands.append(command))
    _, cuda = server._runtime()
    assert cuda is False
    assert ["git", "-C", str(server.cache / "llama.cpp"), "fetch", "--depth", "1", "origin", fm.RUNTIME_COMMIT] in commands
    assert any("-DGGML_CUDA=OFF" in command for command in commands)
    assert commands[-1][-2:] == ["-j", "4"]
    assert json.loads((server.cache / "runtime.json").read_text())["commit"] == fm.RUNTIME_COMMIT


def test_test_environment_cannot_install_or_launch(arui_env, tmp_path, monkeypatch):
    from backend.app.flint_manager import ManagedFlint
    server = ManagedFlint(tmp_path / "state", tmp_path / "cache")
    monkeypatch.setattr(server, "_setup", lambda _: pytest.fail("No provisioning in unit environment"))
    monkeypatch.delenv("ARUI_FLINT_ALLOW_SETUP", raising=False)
    assert server.prepare()["status"] == "disabled"
    assert not server.cache.exists()


def test_prepare_is_idempotent_during_setup(arui_env, tmp_path, monkeypatch):
    from backend.app.flint_manager import ManagedFlint
    monkeypatch.setenv("ARUI_FLINT_ALLOW_SETUP", "1")
    server = ManagedFlint(tmp_path / "state", tmp_path / "cache")
    entered = threading.Event()
    calls = []
    def setup(device):
        calls.append(device); entered.set(); server._stop.wait(2)
    monkeypatch.setattr(server, "_setup", setup)
    server.prepare("cpu"); assert entered.wait(1)
    server.prepare("cpu")
    server.prepare("auto")
    assert server._read()["requested_device"] == "auto"
    server.stop(); server._worker.join(1)
    assert calls == ["cpu"]


def test_cancelling_a_download_reports_stopping_until_the_worker_finishes(arui_env, tmp_path, monkeypatch):
    from backend.app.flint_manager import ManagedFlint
    monkeypatch.setenv("ARUI_FLINT_ALLOW_SETUP", "1")
    server = ManagedFlint(tmp_path / "state", tmp_path / "cache")
    entered, release = threading.Event(), threading.Event()
    def setup(_):
        entered.set(); release.wait(2)
    monkeypatch.setattr(server, "_setup", setup)
    server.prepare(); assert entered.wait(1)
    assert server.stop()["status"] == "stopping"
    assert server.prepare()["status"] == "stopping"
    release.set(); server._worker.join(1)
    assert server.status()["status"] == "stopped"


def test_unhealthy_owned_process_stops_before_replacement(arui_env, tmp_path, monkeypatch):
    from backend.app.flint_manager import ManagedFlint
    server = ManagedFlint(tmp_path / "state", tmp_path / "cache")
    order = []
    monkeypatch.setattr(server, "_owned_process", lambda _: object())
    monkeypatch.setattr(server, "_healthy", lambda: False)
    monkeypatch.setattr(server, "_stop_process", lambda: order.append("stop-owned"))
    monkeypatch.setattr(server, "_setup_locked", lambda _: order.append("start-new"))
    server._setup("cpu")
    assert order == ["stop-owned", "start-new"]


def test_two_managers_share_startup_lock(arui_env, tmp_path, monkeypatch):
    from backend.app.flint_manager import ManagedFlint
    one = ManagedFlint(tmp_path / "state", tmp_path / "cache")
    two = ManagedFlint(tmp_path / "state", tmp_path / "cache")
    entered, release = threading.Event(), threading.Event()
    calls = []
    def setup(device):
        calls.append(device); entered.set(); release.wait(2); one._write(status="ready", fake_owned=True)
    for server in (one, two):
        monkeypatch.setattr(server, "_owned_process", lambda state: state.get("fake_owned"))
        monkeypatch.setattr(server, "_healthy", lambda: True)
        monkeypatch.setattr(server, "_setup_locked", setup)
    first = threading.Thread(target=one._setup, args=("cpu",)); first.start(); assert entered.wait(1)
    second = threading.Thread(target=two._setup, args=("cpu",)); second.start(); release.set()
    first.join(2); second.join(2)
    assert calls == ["cpu"]


def test_stop_never_signals_reused_pid_or_unrelated_process(arui_env, tmp_path, monkeypatch):
    from backend.app import flint_manager
    server = flint_manager.ManagedFlint(tmp_path / "state", tmp_path / "cache")
    fake = SimpleNamespace(create_time=lambda: 200, cmdline=lambda: ["/unrelated/train.py"],
                           terminate=lambda: pytest.fail("Unrelated process was signaled"))
    monkeypatch.setattr(flint_manager.psutil, "Process", lambda _: fake)
    server._write(pid=42, process_created_at=100, runtime_path="/llama-server", model_path="/model.gguf")
    assert server.stop()["status"] == "stopped"


def test_explicit_stop_suppresses_recovery_and_failures_back_off(arui_env, tmp_path, monkeypatch):
    from backend.app.flint_manager import ManagedFlint
    server = ManagedFlint(tmp_path / "state", tmp_path / "cache")
    calls = []
    monkeypatch.setattr(server, "prepare", lambda *a, **k: calls.append((a, k)))
    assert server.stop()["manually_stopped"] is True
    server.recover()
    assert not calls
    server._write(manually_stopped=False, status="error", failures=3)
    server.recover(); assert not calls
    server._write(failures=1, next_retry_at=time.time() + 60)
    server.recover(); assert not calls
    server._write(next_retry_at=0)
    server.recover("cpu"); assert calls == [(("cpu",), {"automatic": True})]


def test_checkpoint_resume_and_checksum_are_real(arui_env, tmp_path, monkeypatch):
    from backend.app import flint_manager as fm
    payload = b"synthetic checkpoint bytes"
    monkeypatch.setattr(fm, "CHECKPOINT_BYTES", len(payload))
    monkeypatch.setattr(fm, "CHECKPOINT_SHA256", hashlib.sha256(payload).hexdigest())
    server = fm.ManagedFlint(tmp_path / "state", tmp_path / "cache")
    server.cache.mkdir(); part = server.cache / (fm.CHECKPOINT_NAME + ".part")
    part.write_bytes(payload[:5])
    requested = []
    class Response(io.BytesIO):
        status = 206
    def download(request, **kwargs):
        requested.append(request.get_header("Range")); return Response(payload[5:])
    monkeypatch.setattr(fm.urllib.request, "urlopen", download)
    monkeypatch.setattr(fm.shutil, "disk_usage", lambda _: SimpleNamespace(free=100 * fm.GIB))
    assert server._checkpoint().read_bytes() == payload
    assert requested == ["bytes=5-"]
    assert not part.exists()


def test_low_disk_does_not_start_download(arui_env, tmp_path, monkeypatch):
    from backend.app import flint_manager as fm
    server = fm.ManagedFlint(tmp_path / "state", tmp_path / "cache")
    monkeypatch.setattr(fm.shutil, "disk_usage", lambda _: SimpleNamespace(free=1))
    monkeypatch.setattr(fm.urllib.request, "urlopen", lambda *a, **k: pytest.fail("download started"))
    with pytest.raises(RuntimeError, match="Free storage"):
        server._checkpoint()


def test_status_never_exposes_managed_token(arui_env, tmp_path):
    from backend.app.flint_manager import ManagedFlint
    server = ManagedFlint(tmp_path / "state", tmp_path / "cache")
    server._write(status="starting", token="PRIVATE_TOKEN", model_path="/private/path")
    (server.root / "api-key").write_text("PRIVATE_TOKEN")
    assert "PRIVATE_TOKEN" not in json.dumps(server.status())
    assert "/private/path" not in json.dumps(server.status())


def test_flint_routes_validate_device_and_return_status(arui_env, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend.app import flint_routes
    fake = SimpleNamespace(status=lambda: {"status": "ready", "device": "cpu"},
                           prepare=lambda device: {"status": "starting", "device": device},
                           stop=lambda: {"status": "stopped"})
    monkeypatch.setattr(flint_routes, "manager", lambda: fake)
    app = FastAPI()
    app.include_router(flint_routes.router, prefix="/api")
    client = TestClient(app)
    assert client.get("/api/flint/status").json()["status"] == "ready"
    assert client.post("/api/flint/prepare", json={"device": "cpu"}).json()["status"] == "starting"
    assert client.post("/api/flint/stop", json={}).json()["status"] == "stopped"
    assert client.post("/api/flint/prepare", json=[]).status_code == 400


def test_changing_hardware_restarts_only_owned_server(arui_env, tmp_path, monkeypatch):
    from backend.app.flint_manager import ManagedFlint
    server = ManagedFlint(tmp_path / "state", tmp_path / "cache")
    server._write(status="ready", device="gpu:2", active_requested_device="auto")
    order = []
    monkeypatch.setattr(server, "_owned_process", lambda _: object())
    monkeypatch.setattr(server, "_healthy", lambda: True)
    monkeypatch.setattr(server, "_stop_process", lambda: order.append("stop-owned"))
    monkeypatch.setattr(server, "_setup_locked", lambda device: order.append(device))
    server._setup("cpu")
    assert order == ["stop-owned", "cpu"]


def test_crash_loop_recovery_stops_after_three_attempts(arui_env, tmp_path, monkeypatch):
    from backend.app import flint_manager as fm
    server = fm.ManagedFlint(tmp_path / "state", tmp_path / "cache")
    server._write(status="ready", pid=99999999)
    clock = [1000.0]
    monkeypatch.setattr(fm.time, "time", lambda: clock[0])
    calls = []
    monkeypatch.setattr(server, "prepare", lambda *a, **k: calls.append((a, k)))
    for delay in (0, 30, 60):
        clock[0] += delay
        server.recover()
    assert len(calls) == 3
    clock[0] += 120
    assert server.recover()["status"] == "error"
    assert len(calls) == 3
    assert "Automatic recovery is paused" in server.status()["detail"]
