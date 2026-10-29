"""Local model setup controls, protected by the dashboard's normal auth gate."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .flint_manager import manager

router = APIRouter(prefix="/flint")


@router.get("/status")
async def status():
    return await asyncio.to_thread(manager().status)


@router.post("/prepare")
async def prepare(request: Request):
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("Expected an object")
        return await asyncio.to_thread(manager().prepare, str(body.get("device") or "auto"))
    except (ValueError, TypeError) as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)


@router.post("/stop")
async def stop():
    return await asyncio.to_thread(manager().stop)
