"""Provision and own one local Flint server without displacing research jobs.

Only the pinned official checkpoint/runtime are installed. Progress and process
identity survive backend restarts; arbitrary listening servers are never adopted
or stopped. Model inference is never replaced with another model or provider.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request

import psutil

CHECKPOINT_REVISION = "9c47151036bc2c34331142dda04488bceab2bc9f"
CHECKPOINT_NAME = "flint-int4-q4_1.gguf"
CHECKPOINT_BYTES = 25_992_966_976
CHECKPOINT_SHA256 = "43c07baccdba42eb7528a9630ee15699b1942c297291dc1b39a061f508ee1a4b"
CHECKPOINT_URL = ("https://huggingface.co/RecursiveSI/flint-int4-gguf/resolve/"
                  + CHECKPOINT_REVISION + "/" + CHECKPOINT_NAME)
RUNTIME_COMMIT = "cf24317bea431b807dc93319d74c61bed6e20aff"
GIB = 1024 ** 3
ACTIVE_STATES = {"checking", "downloading", "verifying", "building", "starting", "stopping", "waiting_for_resources"}


def hardware() -> dict:
    """Read availability; a GPU must have no compute processes to be eligible."""
    result = {"available_ram_bytes": psutil.virtual_memory().available,
              "cpu_threads": min(4, os.cpu_count() or 1), "gpus": []}
    try:
        rows = subprocess.run(["nvidia-smi", "--query-gpu=index,uuid,memory.total,memory.used",
                               "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5)
        processes = subprocess.run(["nvidia-smi", "--query-compute-apps=gpu_uuid,pid",
                                    "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5)
        if rows.returncode or processes.returncode:
            return result
        busy = {line.split(",")[0].strip() for line in processes.stdout.splitlines() if "," in line}
        for line in rows.stdout.splitlines():
            index, uuid, total, used = [part.strip() for part in line.split(",")]
            result["gpus"].append({"index": int(index), "total_mb": int(total), "used_mb": int(used),
                                   "busy": uuid in busy})
        try:
            # The app reserves GPU indices in its DB before the training child
            # appears in nvidia-smi. Respect that launch window as well.
            from .db import SessionLocal
            from .models import Run
            with SessionLocal() as db:
                reserved = {int(index) for index, in db.query(Run.gpu_index).filter(
                    Run.status.in_(("running", "queued")), Run.gpu_index.isnot(None)).all()}
            for gpu in result["gpus"]:
                gpu["busy"] = gpu["busy"] or gpu["index"] in reserved
        except Exception:
            # An unreadable scheduler state must not be treated as idle GPUs.
            for gpu in result["gpus"]:
                gpu["busy"] = True
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return result


def choose_device(resources: dict, requested: str = "auto") -> str | None:
    if requested not in {"auto", "cpu"}:
        raise ValueError("Choose automatic hardware or CPU for managed Flint")
    if requested == "auto":
        for gpu in resources.get("gpus", []):
            if not gpu["busy"] and gpu["used_mb"] < 600 and gpu["total_mb"] - gpu["used_mb"] >= 32768:
                return "gpu:" + str(gpu["index"])
    return "cpu" if resources.get("available_ram_bytes", 0) >= 48 * GIB else None


class ManagedFlint:
    def __init__(self, root: Path, cache: Path):
        self.root, self.cache = Path(root), Path(cache)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None

    @property
    def state_path(self):
        return self.root / "state.json"

    def _read(self) -> dict:
        try:
            value = json.loads(self.state_path.read_text())
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, **changes) -> dict:
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Keep the merge atomic across manager instances, independently of
            # the long-running setup lock. A stale status update must not erase
            # a Stop flag written by a different worker.
            with (self.root / "state.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                state = {**self._read(), **changes, "updated_at": time.time()}
                temp = self.root / ("state-" + secrets.token_hex(4) + ".tmp")
                temp.write_text(json.dumps(state))
                temp.chmod(0o600)
                temp.replace(self.state_path)
                return state

    def _owned_process(self, state: dict):
        try:
            process = psutil.Process(int(state["pid"]))
            command = process.cmdline()
            if (abs(process.create_time() - float(state["process_created_at"])) < .01
                    and str(self.root / "api-key") in command
                    and state.get("model_path") in command
                    and command[0] == state.get("runtime_path")):
                return process
        except (KeyError, ValueError, TypeError, psutil.Error):
            pass
        return None

    def connection(self) -> tuple[str, str, str]:
        state = self._read()
        port = int(state.get("port") or 18342)
        try:
            token = (self.root / "api-key").read_text().strip()
        except OSError:
            token = ""
        return f"http://127.0.0.1:{port}/v1", "flint", token

    def _healthy(self) -> bool:
        url, model, token = self.connection()
        try:
            health = urllib.request.Request(url.removesuffix("/v1") + "/health",
                                            headers={"Authorization": "Bearer " + token})
            with urllib.request.urlopen(health, timeout=1) as response:
                if json.load(response).get("status") != "ok":
                    return False
            request = urllib.request.Request(url + "/models", headers={"Authorization": "Bearer " + token})
            with urllib.request.urlopen(request, timeout=1) as response:
                return model in {item.get("id") for item in json.load(response).get("data", [])}
        except (OSError, ValueError, TypeError):
            return False

    def status(self) -> dict:
        state = self._read()
        phase = state.get("status", "not_installed")
        detail = state.get("detail", "Choose Flint to install and start it automatically.")
        if phase == "ready" and not self._owned_process(state):
            phase, detail = "stopped", "Flint is stopped. Start it to use the local model."
        if self._stop.is_set() and self._worker and self._worker.is_alive():
            phase, detail = "stopping", "Stopping Flint setup. The downloaded model will be kept."
        # Public state deliberately excludes process paths and the local API key.
        return {"status": phase, "detail": detail, "managed": True,
                "manually_stopped": bool(state.get("manually_stopped")),
                "device": state.get("device"), "progress": state.get("progress"),
                "download_bytes": state.get("download_bytes", 0), "download_total_bytes": CHECKPOINT_BYTES,
                "checkpoint": "RecursiveSI/flint-int4-gguf", "model": "flint/local",
                "can_retry": phase in {"error", "stopped", "not_installed"},
                "context_tokens": 16384, "max_output_tokens": 8192}

    def is_ready(self) -> bool:
        return self.status()["status"] == "ready" and self._healthy()

    def _cancelled(self) -> bool:
        # The durable flag also cancels an installer owned by another backend
        # worker. A restart cannot erase the user's explicit Stop instruction.
        return self._stop.is_set() or bool(self._read().get("manually_stopped"))

    @staticmethod
    def _device_changed(state: dict, requested: str) -> bool:
        active = state.get("active_requested_device", state.get("requested_device", requested))
        return active != requested or (requested == "cpu" and str(state.get("device", "cpu")).startswith("gpu:"))

    def prepare(self, device: str = "auto", *, automatic: bool = False) -> dict:
        if device not in {"auto", "cpu"}:
            raise ValueError("Choose automatic hardware or CPU for managed Flint")
        if os.environ.get("ARUI_DISABLE_BG") == "1" and os.environ.get("ARUI_FLINT_ALLOW_SETUP") != "1":
            self._write(status="disabled", detail="Local model setup is disabled in this test environment.")
            return self.status()
        with self._lock:
            current = self.status()
            if automatic and self._read().get("manually_stopped"):
                return current
            if (current["status"] == "ready" and self._healthy()
                    and not self._device_changed(self._read(), device)):
                return current
            if self._worker and self._worker.is_alive():
                if self._read().get("requested_device") != device:
                    self._write(requested_device=device)
                return self.status()
            self._stop.clear()
            extra = {} if automatic else {"failures": 0, "manually_stopped": False,
                                         "recovery_history": [], "next_retry_at": 0}
            self._write(status="checking", detail="Checking the local model, runtime and available hardware.",
                        progress=None, requested_device=device, **extra)
            self._worker = threading.Thread(target=self._setup, args=(device,), name="managed-flint", daemon=True)
            self._worker.start()
            return self.status()

    def _run(self, command: list[str], *, env: dict | None = None, timeout: float = 1200):
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / "setup.log").open("ab") as log:
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True)
            deadline = time.monotonic() + timeout
            while process.poll() is None:
                if self._stop.wait(.25) or self._cancelled() or time.monotonic() > deadline:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                    raise RuntimeError("Flint setup was stopped or exceeded its time limit. Retry setup to continue.")
            if process.returncode:
                raise RuntimeError("Flint runtime installation failed. Check the local setup log and retry.")

    def _verify_checkpoint(self, path: Path):
        stat = path.stat()
        marker = self.cache / "checkpoint-verified.json"
        signature = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha256": CHECKPOINT_SHA256}
        try:
            if json.loads(marker.read_text()) == signature:
                return
        except (OSError, ValueError):
            pass
        self._write(status="verifying", detail="Verifying the official Flint checkpoint checksum.")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                if self._cancelled():
                    raise RuntimeError("Flint setup stopped.")
                digest.update(chunk)
        if stat.st_size != CHECKPOINT_BYTES or digest.hexdigest() != CHECKPOINT_SHA256:
            raise RuntimeError("The Flint checkpoint checksum does not match the official release. Remove the incomplete cache file and retry.")
        marker.write_text(json.dumps(signature))

    def _checkpoint(self) -> Path:
        self.cache.mkdir(parents=True, exist_ok=True)
        target = self.cache / CHECKPOINT_NAME
        existing = self.cache / "pinned-runtime" / CHECKPOINT_NAME
        if not target.exists() and existing.is_file():
            target.symlink_to(existing)
        if target.exists():
            self._verify_checkpoint(target)
            return target.resolve()
        part = target.with_suffix(".gguf.part")
        offset = part.stat().st_size if part.exists() else 0
        needed = max(0, CHECKPOINT_BYTES - offset) + 8 * GIB
        if shutil.disk_usage(self.cache).free < needed:
            raise RuntimeError("Flint needs 26 GB for its model plus 8 GB free for installation. Free storage and retry; existing downloads are kept.")
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        self._write(status="downloading", detail="Downloading the official Flint model (26 GB, once).", progress=offset / CHECKPOINT_BYTES)
        with urllib.request.urlopen(urllib.request.Request(CHECKPOINT_URL, headers=headers), timeout=60) as response:
            append = offset > 0 and response.status == 206
            if not append:
                offset = 0
            last = 0.0
            with part.open("ab" if append else "wb") as stream:
                while chunk := response.read(2 * 1024 * 1024):
                    if self._cancelled():
                        raise RuntimeError("Flint setup stopped. Retry to resume the download.")
                    stream.write(chunk)
                    offset += len(chunk)
                    if time.monotonic() - last > 1:
                        self._write(download_bytes=offset, progress=min(1, offset / CHECKPOINT_BYTES))
                        last = time.monotonic()
        self._verify_checkpoint(part)
        part.replace(target)
        return target.resolve()

    def _runtime(self) -> tuple[Path, bool]:
        cached = self.cache / "pinned-runtime" / "build" / "bin" / "llama-server"
        if cached.is_file():
            source = self.cache / "pinned-runtime" / "llama.cpp"
            result = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5)
            if result.returncode == 0 and result.stdout.strip() == RUNTIME_COMMIT:
                return cached.resolve(), (cached.parent / "libggml-cuda.so").exists()
        source = self.cache / "llama.cpp"
        binary = self.cache / "build" / "bin" / "llama-server"
        manifest = self.cache / "runtime.json"
        try:
            info = json.loads(manifest.read_text())
            if binary.is_file() and info.get("commit") == RUNTIME_COMMIT:
                return binary.resolve(), bool(info.get("cuda"))
        except (OSError, ValueError):
            pass
        self._write(status="building", detail="Installing the pinned local inference runtime.", progress=None)
        if not shutil.which("git") or not shutil.which("c++"):
            if os.geteuid() == 0 and shutil.which("apt-get"):
                self._run(["apt-get", "update"])
                self._run(["apt-get", "install", "-y", "git", "build-essential", "python3-venv"])
            else:
                raise RuntimeError("Automatic runtime installation needs Git and a C++ compiler. Install those system packages, then retry setup.")
        if not source.exists():
            self._run(["git", "init", str(source)])
            self._run(["git", "-C", str(source), "remote", "add", "origin", "https://github.com/ggml-org/llama.cpp.git"])
        self._run(["git", "-C", str(source), "fetch", "--depth", "1", "origin", RUNTIME_COMMIT])
        self._run(["git", "-C", str(source), "checkout", "--detach", RUNTIME_COMMIT])
        cmake = shutil.which("cmake")
        if not cmake:
            tools = self.cache / "build-tools"
            self._run([sys.executable, "-m", "venv", str(tools)])
            self._run([str(tools / "bin" / "pip"), "install", "cmake==4.4.4", "ninja==1.13.0"])
            cmake = str(tools / "bin" / "cmake")
        cuda = bool(shutil.which("nvcc"))
        self._run([cmake, "-S", str(source), "-B", str(self.cache / "build"),
                   "-DCMAKE_BUILD_TYPE=Release", "-DGGML_CUDA=" + ("ON" if cuda else "OFF"),
                   "-DLLAMA_CURL=OFF"])
        self._run([cmake, "--build", str(self.cache / "build"), "--target", "llama-server", "-j", "4"], timeout=3600)
        manifest.write_text(json.dumps({"commit": RUNTIME_COMMIT, "cuda": cuda}))
        return binary.resolve(), cuda

    def _setup(self, requested: str):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.root / "manager.lock").open("a") as lock:
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if self._stop.wait(.5) or self._cancelled():
                        return
            if self._cancelled():
                return
            if self._owned_process(self._read()):
                if self._healthy() and not self._device_changed(self._read(), requested):
                    self._write(status="ready", detail="Flint is ready.")
                    return
                # Never overwrite the key/PID while an older owned server is
                # still occupying memory. Identity checks prevent killing a
                # process belonging to a different user/model/run.
                self._stop_process()
            self._setup_locked(requested)

    def _setup_locked(self, requested: str):
        try:
            self.cache.mkdir(parents=True, exist_ok=True)
            # One installation across concurrent UI requests or backend workers.
            with (self.cache / "install.lock").open("a") as lock:
                while True:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if self._stop.wait(.5) or self._cancelled():
                            return
                model = self._checkpoint()
                binary, supports_cuda = self._runtime()
            while not self._cancelled():
                requested = self._read().get("requested_device", requested)
                resources = hardware()
                device = choose_device(resources, requested if supports_cuda else "cpu")
                if device:
                    break
                self._write(status="waiting_for_resources", detail="Waiting for an idle GPU with 32 GB free, or 48 GB available RAM. Running experiments stay in place.")
                if self._stop.wait(15):
                    return
            if self._cancelled():
                return
            # Recheck immediately before allocation; never claim a GPU selected
            # before a long download or build.
            if device.startswith("gpu:") and device != choose_device(hardware(), requested):
                raise RuntimeError("GPU availability changed before Flint started. Retry setup; no experiment was stopped.")
            self._write(launch_requested_device=requested)
            self._start(binary, model, device)
        except Exception as exc:
            failures = int(self._read().get("failures", 0)) + 1
            self._write(status="stopped" if self._cancelled() else "error",
                        failures=failures, next_retry_at=time.time() + 30 * (2 ** min(failures, 3)),
                        detail=str(exc) if isinstance(exc, RuntimeError) else
                        "Flint setup could not finish. Check available storage, network and the local setup log, then retry.")

    def _start(self, binary: Path, model: Path, device: str):
        with self._lock:
            if self._cancelled():
                raise RuntimeError("Flint setup stopped.")
            # A running coding CLI keeps this URL/key in its provider configuration.
            # Recovery must bring the same connection back, not invalidate clients.
            saved_port = int(self._read().get("port") or 0)
            with socket.socket() as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    sock.bind(("127.0.0.1", saved_port))
                except OSError:
                    raise RuntimeError("Flint's saved local port is in use by another process. Free that port and retry; existing client credentials were kept.") from None
                port = sock.getsockname()[1]
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            key = self.root / "api-key"
            if not key.exists() or not key.read_text().strip():
                key.write_text(secrets.token_urlsafe(32))
            key.chmod(0o600)
            command = [str(binary), "--model", str(model), "--alias", "flint", "--ctx-size", "16384",
                       "--n-gpu-layers", "0" if device == "cpu" else "99", "--parallel", "1",
                       "--threads", "4", "--threads-batch", "4", "--cache-ram", "0", "--jinja",
                       "--temp", "1.0", "--top-p", "0.95", "--top-k", "20", "--min-p", "0",
                       "--host", "127.0.0.1", "--port", str(port), "--api-key-file", str(key)]
            env = {**os.environ, "CUDA_VISIBLE_DEVICES": "" if device == "cpu" else device.split(":")[1],
                   "LD_LIBRARY_PATH": str(binary.parent) + os.pathsep + os.environ.get("LD_LIBRARY_PATH", "")}
            with (self.root / "server.log").open("ab") as log:
                process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                           env=env, start_new_session=True)
            self._write(status="starting", detail="Loading Flint into " + ("CPU memory. CPU responses take longer." if device == "cpu" else "the idle GPU."),
                        device=device, port=port, pid=process.pid, process_created_at=psutil.Process(process.pid).create_time(),
                        model_path=str(model), runtime_path=str(binary), progress=None)
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline and not self._stop.wait(.5):
            if self._cancelled():
                break
            if process.poll() is not None:
                raise RuntimeError("The local Flint server exited while starting. Check the local server log and retry.")
            if self._healthy():
                with self._lock:
                    if self._cancelled():
                        break
                    self._write(status="ready", failures=0, health_failures=0,
                                active_requested_device=self._read().get("launch_requested_device", "auto"),
                                detail="Flint is ready on " + ("CPU (4 threads). Responses are slower; training GPUs remain available." if device == "cpu" else device.replace(":", " ").upper() + "."))
                    return
        self._stop_process()
        if self._cancelled():
            raise RuntimeError("Flint setup stopped.")
        raise RuntimeError("Flint did not become ready within 10 minutes. Retry setup after checking available memory.")

    def _stop_process(self):
        process = self._owned_process(self._read())
        if process:
            with contextlib.suppress(psutil.NoSuchProcess):
                process.terminate()
                try:
                    process.wait(timeout=10)
                except psutil.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)

    def stop(self) -> dict:
        with self._lock:
            self._stop.set()
            self._write(status="stopping", manually_stopped=True,
                        detail="Stopping managed Flint; its downloaded model will be kept.")
            self._stop_process()
            self._write(status="stopped", manually_stopped=True,
                        detail="Managed Flint is stopped. Its downloaded model is kept for the next start.")
            return self.status()

    def recover(self, device: str = "auto") -> dict:
        """Bounded service recovery; explicit Stop remains stopped."""
        state = self._read()
        current = self.status()
        if state.get("manually_stopped") or (self._worker and self._worker.is_alive()):
            return current
        if current["status"] == "ready":
            if self._healthy():
                if state.get("health_failures"):
                    self._write(health_failures=0)
                if self._device_changed(state, device):
                    return self.prepare(device, automatic=True)
                return current
            count = int(state.get("health_failures", 0)) + 1
            self._write(health_failures=count)
            if count < 3:
                return current
        if int(state.get("failures", 0)) >= 3 or time.time() < state.get("next_retry_at", 0):
            return current
        # A server that repeatedly becomes ready then crashes must not reset its
        # retry budget merely because startup succeeded briefly.
        if state.get("pid"):
            now = time.time()
            history = [float(stamp) for stamp in state.get("recovery_history", []) if now - float(stamp) < 600]
            if len(history) >= 3:
                self._write(status="error", failures=3, detail="Flint stopped repeatedly. Automatic recovery is paused; check the local server log and choose Retry setup.")
                return self.status()
            history.append(now)
            self._write(recovery_history=history, next_retry_at=now + 30 * (2 ** (len(history) - 1)))
        return self.prepare(device, automatic=True)


_MANAGER = None


def manager() -> ManagedFlint:
    global _MANAGER
    if _MANAGER is None:
        from .config import DATA_DIR
        cache = Path(os.environ.get("ARUI_MODEL_CACHE_DIR") or
                     ("/var/lib/autoresearcherui/models" if os.geteuid() == 0 else
                      str(Path.home() / ".cache" / "autoresearcherui" / "models"))) / "flint"
        _MANAGER = ManagedFlint(DATA_DIR / "flint", cache)
    return _MANAGER


def start_if_selected():
    if os.environ.get("ARUI_DISABLE_BG") == "1":
        return
    from .db import SessionLocal
    from .models import Setting
    from .provider_config import flint_managed
    with SessionLocal() as db:
        row = db.query(Setting).filter(Setting.key == "onboarding").first()
        cfg = row.value if row and isinstance(row.value, dict) else {}
    if flint_managed(cfg) and any(cfg.get(field) == "flint/local" for field in (
            "research_agent_model", "author_agent_model", "scoping_model", "council_gemini_model",
            "council_openai_model", "council_claude_model", "pi_agent_model")):
        manager().recover(str(cfg.get("flint_device") or "auto"))


_LAST_TICK = 0.0


def tick():
    """Cheap supervisor hook; no model calls, at most once per 30 seconds."""
    global _LAST_TICK
    if time.monotonic() - _LAST_TICK < 30:
        return
    _LAST_TICK = time.monotonic()
    start_if_selected()
