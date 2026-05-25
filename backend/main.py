"""autoresearcherUI backend - Uvicorn entrypoint.

Single process: serves the REST API, the SSE streams, the arui ingest
endpoints, and the static dashboard. In demo mode it also seeds a realistic
project and runs the live simulator so the dashboard is populated and animated.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .app.api import router
from .app.config import DEMO_MODE, HOST, PORT, STATIC_DIR
from .app.db import init_db
from .app.seed import seed_all
from .app.sim import simulator


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if DEMO_MODE:
        seed_all()
        asyncio.create_task(simulator())
    yield


app = FastAPI(title="autoresearcherUI", version="0.2.0", lifespan=lifespan)
app.include_router(router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


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
