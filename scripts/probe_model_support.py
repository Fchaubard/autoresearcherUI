#!/usr/bin/env python3
"""Explicit, bounded synthetic provider probes; never launches a GPU server.

Run from the repository with the service's provider environment loaded.
Default probes: new Gemini models and all registered OpenCode free models.
Use --model flint/local to test an already-running local endpoint as well.
No research data, credentials, raw HTTP bodies or stderr are printed.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.model_registry import OPENCODE_FREE_MODELS, provider_for
from backend.app.opencode_runtime import OpenCodeRequestError, complete
from backend.app.provider_config import flint_config


def probe(model: str) -> dict:
    provider = provider_for(model)
    result = {"model": model, "provider": provider}
    try:
        if provider == "opencode":
            result["response"] = complete(model, "Answer the user's arithmetic question. Do not use tools.",
                                          "What is 2+2? Reply with only the numeral.", timeout=30).strip()
        elif provider == "gemini":
            key = os.environ.get("GEMINI_API_KEY")
            if not key:
                return dict(result, status="not_configured")
            request = urllib.request.Request(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"Content-Type": "application/json", "x-goog-api-key": key},
                data=json.dumps({"contents": [{"role": "user", "parts": [{"text": "What is 2+2? Reply only with the numeral."}]}],
                                 "generationConfig": {"maxOutputTokens": 128, "thinkingConfig": {"thinkingLevel": "low"}}}).encode())
            with urllib.request.urlopen(request, timeout=30) as response:
                data = json.load(response)
            result["response"] = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"]
                                          if not p.get("thought")).strip()
        elif provider == "flint":
            url, served_model, token = flint_config()
            request = urllib.request.Request(url + "/chat/completions",
                headers={"Content-Type": "application/json", **({"Authorization": "Bearer " + token} if token else {})},
                data=json.dumps({"model": served_model, "max_tokens": 1024,
                                 "messages": [{"role": "user", "content": "What is 2+2? Reply only with the numeral."}]}).encode())
            with urllib.request.urlopen(request, timeout=30) as response:
                data = json.load(response)
            result["response"] = data["choices"][0]["message"]["content"].strip()
        else:
            return dict(result, status="unsupported_probe_provider")
        return dict(result, status="passed" if result["response"] == "4" else "unexpected_response")
    except OpenCodeRequestError as exc:
        return dict(result, status="provider_error", http_status=exc.status)
    except urllib.error.HTTPError as exc:
        return dict(result, status="provider_error", http_status=exc.code)
    except Exception as exc:
        return dict(result, status="failed", error_type=type(exc).__name__)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", action="append", help="Exact registry ID; repeat to probe several")
    args = parser.parse_args()
    models = args.model or ["gemini-4-argon", "gemini-3.1-flash-lite"] + ["opencode/" + m for m, _ in OPENCODE_FREE_MODELS]
    success = True
    for model in models:
        result = probe(model)
        print(json.dumps(result), flush=True)
        success = success and result["status"] == "passed"
    raise SystemExit(0 if success else 1)
