"""Private per-attempt inference accounting without prompts or credentials."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import datetime as dt
import fcntl
import json
import logging
import os
import re
import time
from urllib.parse import urlsplit

_CONTEXT = ContextVar("advisory_usage", default={})
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:/+=-]{1,200}$")


@contextmanager
def context(**fields):
    token = _CONTEXT.set({**_CONTEXT.get(), **fields})
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def _integer(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def tokens(data: dict, provider: str) -> dict:
    """Provider totals retain their semantics; unknown counts remain null.

    Anthropic input totals include its separately reported cache reads/writes.
    Reasoning is a subset of output, never an extra billable total here.
    """
    usage = data.get("usageMetadata" if provider == "gemini" else "usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    write_30m, write_12h = None, None
    if provider == "gemini":
        inp = _integer(usage.get("promptTokenCount"))
        visible = _integer(usage.get("candidatesTokenCount"))
        reason = _integer(usage.get("thoughtsTokenCount"))
        output = visible + (reason or 0) if visible is not None else None
        cached = _integer(usage.get("cachedContentTokenCount"))
        writes = None
    elif provider == "claude":
        uncached = _integer(usage.get("input_tokens"))
        cached = _integer(usage.get("cache_read_input_tokens"))
        writes = _integer(usage.get("cache_creation_input_tokens"))
        inp = uncached + (cached or 0) + (writes or 0) if uncached is not None else None
        output = _integer(usage.get("output_tokens"))
        details = usage.get("output_tokens_details") or {}
        reason = _integer(details.get("thinking_tokens")) if isinstance(details, dict) else None
    else:
        inp = _integer(usage.get("input_tokens", usage.get("prompt_tokens")))
        output = _integer(usage.get("output_tokens", usage.get("completion_tokens")))
        details = usage.get("input_tokens_details", usage.get("prompt_tokens_details")) or {}
        out_details = usage.get("output_tokens_details", usage.get("completion_tokens_details")) or {}
        cached = _integer(details.get("cached_tokens")) if isinstance(details, dict) else None
        writes = _integer(details.get("cache_write_tokens", details.get("cache_creation_tokens"))) if isinstance(details, dict) else None
        ttl = details.get("cache_write_tokens_details", usage.get("cache_write_tokens_details")) if isinstance(details, dict) else None
        if isinstance(ttl, dict):
            write_30m = _integer(ttl.get("ttl_30m_tokens"))
            write_12h = _integer(ttl.get("ttl_12h_tokens"))
        reason = _integer(out_details.get("reasoning_tokens")) if isinstance(out_details, dict) else None
    return {"input_tokens": inp, "cache_read_tokens": cached,
            "cache_write_tokens": writes, "output_tokens": output,
            "cache_write_30m_tokens": write_30m, "cache_write_12h_tokens": write_12h,
            "reasoning_tokens": reason, "usage_reported": bool(usage)}


def _safe(value):
    return value if isinstance(value, str) and _SAFE_ID.fullmatch(value) else ""


def record(url: str, body: dict, *, started: float, data: dict | None = None,
           status: int | None = None, request_id: str = "", error_type: str = "") -> None:
    """Append a sanitized JSONL event. Accounting failures never repeat a call."""
    from .config import DATA_DIR
    host = urlsplit(url).hostname
    provider = {"api.openai.com": "openai", "api.anthropic.com": "claude",
                "generativelanguage.googleapis.com": "gemini"}.get(host, "flint")
    model = body.get("model", "")
    if provider == "gemini":
        model = urlsplit(url).path.rsplit("/", 1)[-1].split(":", 1)[0]
    effort = body.get("reasoning_effort") or (body.get("reasoning") or {}).get("effort")
    if provider == "gemini":
        effort = (body.get("generationConfig", {}).get("thinkingConfig") or {}).get("thinkingLevel")
    ctx = _CONTEXT.get()
    response = data if isinstance(data, dict) else {}
    returned_usage = response.get("usageMetadata" if provider == "gemini" else "usage") or {}
    returned_tier = (response.get("service_tier", response.get("serviceTier"))
                     or (returned_usage.get("serviceTier", returned_usage.get("service_tier"))
                         if isinstance(returned_usage, dict) else None))
    event = {"at": dt.datetime.now(dt.timezone.utc).isoformat(),
             "provider": provider, "model": _safe(model),
             "response_model": _safe(response.get("model", response.get("modelVersion", ""))),
             "effort": _safe(effort), "role": _safe(ctx.get("role", "advisor")),
             "requested_service_tier": _safe(body.get("service_tier", body.get("serviceTier"))) or None,
             "returned_service_tier": _safe(returned_tier) or None,
             "operation": _safe(ctx.get("operation", "inference")),
             "trigger_id": _safe(ctx.get("trigger_id", "")),
             "attempt": _integer(ctx.get("attempt", 1)) or 1,
             "traffic": "verification" if os.environ.get("ARUI_VERIFICATION") == "1" else "application",
             "latency_ms": round((time.monotonic() - started) * 1000),
             "http_status": _integer(status), "outcome": "error" if error_type else "response",
             "error_type": _safe(error_type),
             "request_id": _safe(request_id or response.get("id", response.get("responseId", ""))),
             **tokens(response, provider)}
    try:
        path = DATA_DIR / "advisory_usage.jsonl"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as output:
            fcntl.flock(output, fcntl.LOCK_EX)
            output.write(json.dumps(event, separators=(",", ":")) + "\n")
            output.flush()
            fcntl.flock(output, fcntl.LOCK_UN)
    except (OSError, TypeError, ValueError):
        logging.getLogger(__name__).warning("Could not persist advisory usage accounting")
