"""autoresearcherUI backend - Uvicorn entrypoint.

Single process: serves the REST API, the SSE streams, the arui ingest
endpoints, and the static dashboard. When AUTORUN is set and no project
exists yet, it boots the bundled example project through the real
orchestrator so a fresh install has something live to look at.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .app import auth, monitor, notify, orchestrator, pi, paper_runner, paper_watcher
from .app.api import router
from .app.config import AUTORUN, HOST, PORT, ROOT, STATIC_DIR
from .app.db import SessionLocal, init_db
from .app.models import Project


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Push onboarding-saved API tokens into os.environ BEFORE any of the
    # background services start, so the very first PI / council cycle on
    # boot can find them. Idempotent — env vars set externally win.
    try:
        from .app import api as _api
        _api._apply_tokens_to_env()
    except Exception as e:                              # noqa: BLE001
        print(f"[main] apply_tokens_to_env failed: {e}", flush=True)
    # Pre-write Claude Code's settings.json with apiKeyHelper so Claude
    # uses ANTHROPIC_API_KEY directly and never falls into OAuth. Doing
    # this at startup (not just at agent spawn) means even a manual
    # `claude` invocation from SSH picks up the API-key auth path.
    try:
        from .app.agent import RealAgent
        # _apply_tokens_to_env() above will have populated ANTHROPIC_API_KEY
        # from the onboarding row if it exists — pass it so the
        # "Use this API key?" dialog is pre-approved.
        import os as _os
        RealAgent._ensure_claude_settings(
            _os.environ.get("ANTHROPIC_API_KEY", ""))
    except Exception as e:                              # noqa: BLE001
        print(f"[main] ensure_claude_settings failed: {e}", flush=True)
    # RESEARCH_IMPROVEMENT_PLAN #3: rebuild the duplicate-killer
    # registry from every kept run before any /api/track/run can
    # arrive. We don't persist the registry; the DB is the source of
    # truth and re-hashing is cheap.
    try:
        from .app import novelty as _novelty
        loaded = _novelty.populate_registry_from_db()
        print(f"[main] novelty registry seeded with {loaded} hashes",
              flush=True)
    except Exception as e:                              # noqa: BLE001
        print(f"[main] novelty registry seed failed: {e}", flush=True)
    notify.start_scheduler()          # periodic email digests (cadence-driven)
    monitor.start()                   # gpu telemetry + run reconciliation
    pi.start()                        # hourly PI oversight cycle
    paper_runner.start()              # paper-mode ablation scheduler
    paper_watcher.start()             # hourly anti-pattern nudges
    try:                              # surface agent phases in activity feed
        from .app import agent_watcher
        agent_watcher.start()
    except Exception as e:                              # noqa: BLE001
        print(f"[main] agent_watcher start failed: {e}", flush=True)
    try:                              # anonymous, opt-out usage telemetry
        from .app import telemetry
        telemetry.capture("app_started")
    except Exception:                                   # noqa: BLE001
        pass
    if AUTORUN:
        db = SessionLocal()
        has_project = db.query(Project).first() is not None
        db.close()
        if not has_project:
            # auto-run the example research project through the REAL
            # orchestrator — genuine experiments, no fake seed/simulator.
            orchestrator.start(str(ROOT / "tests" / "example_project"),
                               name="tiny-sgd", n_slots=10,
                               metric_key="val_mse", direction="minimize")
    yield


app = FastAPI(title="autoresearcherUI", version="0.1.0", lifespan=lifespan)
# Passcode gate runs BEFORE the router so a missing/wrong passcode 401s
# immediately. The gate is a no-op when no passcode is configured, so
# fresh installs and pre-onboarding flows work exactly as before.
app.middleware("http")(auth.passcode_middleware)
app.include_router(router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# Browsers were aggressively disk-caching app.js / style.css despite our
# query-string cache-busting (Chrome heuristic when no Cache-Control header
# is sent). Force them to revalidate every request with no-cache on static
# assets and on the index. ETag/Last-Modified make this cheap (304 replies).
@app.middleware("http")
async def _no_cache_static(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/static/"):
        response.headers["Cache-Control"] = (
            "no-cache, no-store, must-revalidate, max-age=0")
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


import time as _time

_INDEX_TEMPLATE: str | None = None


def _index_html() -> str:
    """Serve index.html with the static-asset cache-bust REWRITTEN to the
    current wall-clock millisecond. This guarantees Chrome (which had been
    serving aggressively-cached app.js/style.css regardless of Cache-Control
    headers and hard-refresh) is forced to fetch fresh bytes because every
    page load references URLs it has never seen before. The actual asset
    bytes are still served from disk — the version is just a query param.
    """
    global _INDEX_TEMPLATE
    if _INDEX_TEMPLATE is None:
        _INDEX_TEMPLATE = (STATIC_DIR / "index.html").read_text()
    import re as _re
    nonce = str(int(_time.time() * 1000))
    return _re.sub(r"\?v=[^\"']+", f"?v={nonce}", _INDEX_TEMPLATE)


@app.get("/")
def index():
    from fastapi.responses import HTMLResponse
    return HTMLResponse(_index_html(), headers={
        "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    })


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


# SPA client-side routing: any URL that isn't an API or static asset
# should serve index.html so the frontend can read window.location.pathname
# and pick the right view (Dashboard / Write the paper / Analysis / …).
_SPA_PATHS = {
    "dashboard", "analysis", "files", "lessons",
    "write-paper", "writepaper", "write_paper", "paper", "latex",
    "system", "system-stats", "systemstats",
    "authkeys", "authorized-keys", "authorized_keys",
    "p",                         # /p/<token> → read-only share viewer
}


@app.get("/{path:path}")
def spa_catchall(path: str):
    """Serve index.html for SPA paths so /write-paper et al. work on reload.
    The /p/<token> share-viewer URL is also served as the SPA so the
    frontend can render the read-only view by calling /api/paper/share/."""
    from fastapi.responses import HTMLResponse, JSONResponse
    head = path.split("/", 1)[0]
    if head in _SPA_PATHS:
        return HTMLResponse(_index_html(), headers={
            "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        })
    # otherwise 404 (preserve existing behavior for unknown paths)
    return JSONResponse({"detail": "Not Found"}, status_code=404)


def _check_port_or_die(port: int) -> None:
    """Refuse to start if the configured port is already taken.

    Why: cloudflared, the email digest links, and the agents' ARUI_INGEST_URL
    all assume 8000 (or whatever ARUI_PORT is set to). If a previous
    backend.main is still bound when this one starts, uvicorn used to
    silently re-bind to the *next* free port and the operator would
    spend an hour wondering why https://…trycloudflare.com returns 502
    and `curl localhost:8000` works. Fail fast with a loud message
    instead — the supervisor loop in setup.sh will then retry in 2s,
    which is enough time for the old process to release the socket.
    """
    import socket as _s
    s = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
    try:
        s.setsockopt(_s.SOL_SOCKET, _s.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", port))
    except OSError as e:
        # Print a giant, hard-to-miss banner. setup.sh's supervisor will
        # respawn us in 2 seconds; usually the old process has released
        # the port by then.
        msg = (f"[backend] FATAL: port {port} is already in use ({e}). "
               f"Refusing to silently rebind to a different port — "
               f"cloudflared and the agents are configured for {port}. "
               f"Will exit so the supervisor can retry.")
        print("\n" + "=" * 72, flush=True)
        print(msg, flush=True)
        print("=" * 72 + "\n", flush=True)
        raise SystemExit(2) from e
    finally:
        try:
            s.close()
        except Exception:
            pass


def run() -> None:
    _check_port_or_die(PORT)
    print(f"\n  autoresearcherUI  ->  http://localhost:{PORT}\n")
    uvicorn.run("backend.main:app", host=HOST, port=PORT, reload=False)


if __name__ == "__main__":
    run()
