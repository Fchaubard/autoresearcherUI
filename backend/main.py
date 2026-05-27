"""autoresearcherUI backend - Uvicorn entrypoint.

Single process: serves the REST API, the SSE streams, the arui ingest
endpoints, and the static dashboard. In demo mode it also seeds a realistic
project and runs the live simulator so the dashboard is populated and animated.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .app import monitor, notify, orchestrator, pi
from .app.api import router
from .app.config import AUTORUN, HOST, PORT, ROOT, STATIC_DIR
from .app.db import SessionLocal, init_db
from .app.models import Project


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    notify.start_scheduler()          # periodic email digests (cadence-driven)
    monitor.start()                   # gpu telemetry + run reconciliation
    pi.start()                        # hourly PI oversight cycle
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


app = FastAPI(title="autoresearcherUI", version="0.2.0", lifespan=lifespan)
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


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


def run() -> None:
    print(f"\n  autoresearcherUI  ->  http://localhost:{PORT}\n")
    uvicorn.run("backend.main:app", host=HOST, port=PORT, reload=False)


if __name__ == "__main__":
    run()
