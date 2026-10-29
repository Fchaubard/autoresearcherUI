#!/usr/bin/env python3
"""Explicit live checks of every reasoning effort advertised by the catalog.

Only a tiny synthetic JSON arithmetic task is sent. No research data or GPU
jobs are used. Credentials may be loaded from the service DB read-only.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime as dt
import json
import os
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.probe_advisor_roles import _error_metadata, load_credentials


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--credentials-db")
    parser.add_argument("--model", action="append")
    parser.add_argument("--effort", action="append")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.credentials_db:
        load_credentials(args.credentials_db)
    with tempfile.TemporaryDirectory(prefix="arui-effort-matrix-") as folder:
        os.environ.update(ARUI_DATA_DIR=folder, ARUI_WORKSPACE_DIR=folder,
                          ARUI_DISABLE_BG="1", ARUI_TELEMETRY_DISABLED="1")
        from backend.app import council
        from backend.app.model_registry import MODELS
        council._TIMEOUT, council._RETRY_DELAYS = 45, ()
        original = council._post_json

        def bounded_post(url, body, headers):
            body = json.loads(json.dumps(body))
            if ":generateContent" in url:
                body.setdefault("generationConfig", {})["maxOutputTokens"] = 2048
            elif url.endswith("/responses"):
                body["max_output_tokens"] = 2048
            else:
                body["max_completion_tokens"] = 2048
            return original(url, body, headers)

        council._post_json = bounded_post
        def probe(model, effort):
            start = time.monotonic()
            result = {"model": model, "effort": effort, "inference": "real",
                      "tested_at": dt.datetime.now(dt.timezone.utc).isoformat()}
            try:
                answer = council._call_model(model, effort,
                    'Return only JSON with the answer, e.g. {"answer":4}.',
                    'What is 2+2? Return JSON {"answer":4} with the computed answer.')
                parsed = council._safe_parse(answer)
                result["status"] = "passed" if parsed and parsed.get("answer") == 4 else "unexpected_response"
            except Exception as exc:
                result.update(status="provider_error", **_error_metadata(exc))
            result["elapsed_seconds"] = round(time.monotonic() - start, 3)
            return result

        models = [m for m in MODELS if m.get("efforts") and (not args.model or m["id"] in args.model)]
        cells = [(m["id"], effort) for m in models for effort in m["efforts"]
                 if not args.effort or effort in args.effort]
        results = []
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(probe, model, effort) for model, effort in cells]
            for future in as_completed(futures):
                result = future.result()
                results.append(result)
                temp = args.output.with_suffix(".tmp")
                temp.write_text(json.dumps({"expected_cells": len(cells), "results": results}, indent=2) + "\n")
                temp.replace(args.output)
                print(json.dumps(result), flush=True)
        return 0 if all(r["status"] == "passed" for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
