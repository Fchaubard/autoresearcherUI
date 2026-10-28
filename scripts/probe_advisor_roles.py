#!/usr/bin/env python3
"""Live, isolated advisor-role matrix using the application's exact adapters.

Explicit opt-in only. Reads optional credentials DB in SQLite read-only mode;
credentials stay in process environment, and only synthetic research is sent.
Each cell has a fresh temporary DB/workspace and a hard child-process deadline.
No research loop, GPU job, agent send, notification, or production write runs.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import contextlib
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
ROLES = ("scoping", "reviewer1", "reviewer2", "tiebreaker", "pi")
EXTRA_ROLES = ("scoping_chat",)


def _error_metadata(exc: BaseException) -> dict:
    """Never emit raw provider errors: they can contain headers or requests."""
    out = {"error_type": type(exc).__name__}
    category = getattr(exc, "error_category", None)
    if category in {"model_unavailable", "quota_exhausted", "authentication"}:
        out["error_category"] = category
    code = getattr(exc, "code", None) or getattr(exc, "status", None)
    if isinstance(code, int):
        out["http_status"] = code
    if isinstance(exc, urllib.error.HTTPError):
        try:
            payload = json.loads(exc.read())
            err = payload.get("error", {})
            if isinstance(err, dict):
                for field in ("code", "type", "param", "status"):
                    value = err.get(field)
                    if isinstance(value, str) and re.fullmatch(r"[\w.\[\]-]{1,100}", value):
                        out["provider_" + field] = value
                message = str(err.get("message", "")).lower()
                for needle, label in (
                    ("not found", "model_not_found"), ("does not exist", "model_not_found"),
                    ("not supported", "unsupported_parameter_or_model"),
                    ("unsupported", "unsupported_parameter_or_model"),
                    ("quota", "quota_exhausted"), ("credit", "billing_or_credits"),
                    ("rate limit", "rate_limited"), ("usage limit", "usage_limit"),
                    ("thinking", "thinking_configuration")):
                    if needle in message:
                        out["error_category"] = label
                        break
        except Exception:
            pass
    return out


def load_credentials(path: str) -> None:
    from backend.app.provider_config import environment_from_config
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        row = conn.execute("SELECT value FROM setting WHERE key = ?", ("onboarding",)).fetchone()
    cfg = json.loads(row[0]) if row else {}
    os.environ.update(environment_from_config(cfg))


def _validate(role: str, raw: dict, parsed: dict) -> list[str]:
    errors = []
    if role == "scoping_chat":
        if not isinstance(raw.get("reply"), str) or not raw["reply"].strip():
            errors.append("missing_reply")
    elif role == "scoping":
        for key in ("problem_restated", "sota_summary", "recommended_direction"):
            if not isinstance(raw.get(key), str) or not raw[key].strip():
                errors.append("missing_" + key)
        for key in ("user_ideas_assessment", "new_ideas", "open_questions"):
            if not isinstance(raw.get(key), list):
                errors.append("invalid_" + key)
        for key in ("user_ideas_assessment", "new_ideas"):
            for idea in parsed.get(key, []):
                if not isinstance(idea, dict) or not idea.get("cheap_kill_test"):
                    errors.append("missing_kill_test")
                elif any(p != "fixture_ridge" for p in idea.get("closest_prior_work", [])):
                    errors.append("invalid_citation")
    elif role == "pi":
        if not isinstance(raw.get("concerns"), str) or not raw["concerns"].strip():
            errors.append("invalid_concerns")
        if not isinstance(raw.get("messages"), list) or any(not isinstance(x, str) for x in raw.get("messages", [])):
            errors.append("invalid_messages")
    else:
        if raw.get("verdict") not in {"kept", "discarded", "crashed", "inconclusive"}:
            errors.append("invalid_verdict")
        if not isinstance(raw.get("learning"), str):
            errors.append("invalid_learning")
        for key in ("rerank_pending", "new_ideas", "veto"):
            if not isinstance(raw.get(key), list):
                errors.append("invalid_" + key)
    return sorted(set(errors))


def probe_role(model: str, role: str, *, timeout: int = 90,
               max_output_tokens: int = 4096) -> dict:
    """One role cell. Call in a fresh process; backend imports bind DB paths."""
    started = time.monotonic()
    result = {"model": model, "role": role,
              "tested_at": dt.datetime.now(dt.timezone.utc).isoformat(),
              "timeout_seconds": timeout, "max_output_tokens": max_output_tokens,
              "inference": "real", "fallback_allowed": False}
    with tempfile.TemporaryDirectory(prefix="arui-advisor-") as temp:
        base = Path(temp)
        os.environ.update(ARUI_DATA_DIR=str(base / "data"),
                          ARUI_WORKSPACE_DIR=str(base / "workspace"),
                          ARUI_DISABLE_BG="1", ARUI_TELEMETRY_DISABLED="1",
                          TMUX="", TMUX_TMPDIR=str(base / "tmux"), PWD=str(base))
        (base / "tmux").mkdir(mode=0o700)
        # Imports happen only AFTER isolation. This function does not invoke
        # cycle/deliberate workers that persist or send model suggestions.
        from backend.app import council, db, pi
        from backend.app.model_registry import provider_for
        from backend.app.models import Setting
        from backend.app.provider_config import requirement_error
        result["provider"] = provider_for(model)
        db.init_db()
        cfg = {"scoping_model": model, "pi_agent_model": model,
               "pi_agent_effort": "low", "run_debate": False,
               "council_enable_gemini": False, "council_enable_openai": False,
               "council_enable_claude_tiebreaker": False}
        for slot in ("gemini", "openai", "claude"):
            cfg[f"council_{slot}_model"] = model
            cfg[f"council_{slot}_effort"] = "low"
        with db.SessionLocal() as session:
            session.add(Setting(key="onboarding", value=cfg))
            session.commit()
        requirement = requirement_error(model)
        if requirement:
            return dict(result, status="not_configured", error_category="missing_provider_configuration",
                        elapsed_seconds=round(time.monotonic() - started, 3))
        requests, outputs, dispatches = [], [], []
        original_post, original_call = council._post_json, council._call_model
        original_text_call = council._call_text_model
        original_timeout, original_retry = council._TIMEOUT, council._RETRY_DELAYS
        council._TIMEOUT, council._RETRY_DELAYS = timeout, ()

        def observed_post(url, body, headers):
            body = json.loads(json.dumps(body))
            endpoint = urlsplit(url).path
            request_model = body.get("model") or endpoint.split("/models/")[-1].split(":")[0]
            entry = {"endpoint": endpoint, "requested_model": request_model}
            if ":generateContent" in endpoint:
                body.setdefault("generationConfig", {})["maxOutputTokens"] = max_output_tokens
            elif endpoint.endswith("/responses"):
                body["max_output_tokens"] = max_output_tokens
            elif "api.openai.com" in url:
                body["max_completion_tokens"] = max_output_tokens
            else:
                body["max_tokens"] = min(body.get("max_tokens", max_output_tokens), max_output_tokens)
            requests.append(entry)
            try:
                data = original_post(url, body, headers)
                entry["http_status"] = 200
                if isinstance(data.get("model"), str):
                    entry["response_model"] = data["model"]
                if isinstance(data.get("modelVersion"), str):
                    entry["response_model"] = data["modelVersion"]
                return data
            except Exception as exc:
                entry.update(_error_metadata(exc))
                raise

        def observed_call(chosen, effort, system, user):
            dispatches.append(chosen)
            if chosen != model:
                raise RuntimeError("Forbidden model fallback detected")
            try:
                answer = original_call(chosen, effort, system, user)
                outputs.append(answer)
                return answer
            except Exception as exc:
                if not requests or requests[-1].get("http_status") == 200:
                    requests.append({"requested_model": chosen, **_error_metadata(exc)})
                raise

        def observed_text_call(chosen, system, user):
            dispatches.append(chosen)
            if chosen != model:
                raise RuntimeError("Forbidden model fallback detected")
            try:
                answer = original_text_call(chosen, system, user)
                outputs.append(answer)
                return answer
            except Exception as exc:
                # scope_chat replaces provider exceptions with a safe public
                # RuntimeError. Capture status/category at this inner boundary.
                if not requests or requests[-1].get("http_status") == 200:
                    requests.append({"requested_model": chosen, **_error_metadata(exc)})
                raise

        council._post_json, council._call_model = observed_post, observed_call
        council._call_text_model = observed_text_call
        context = {"project": {"purpose": "Synthetic ridge regression comparison", "metric": "val_mse", "metric_direction": "minimize"},
                   "this_run": {"id": "fixture_ridge_l2", "run_name": "fixture_ridge_l2", "status": "kept", "headline_metric": 0.08, "config": {"l2": 0.1}},
                   "all_prior_runs_count": 1, "all_prior_runs_oneliners": "kept 0.10 fixture_baseline baseline ridge l2=0",
                   "pending_ideas": [{"id": "fixture_seed_repeat", "what": "Repeat l2=0.1 with seed 2"}], "pending_total_count": 1}
        try:
            # Suppress production adapter diagnostics. Evidence below contains
            # only approved metadata, never provider error bodies or secrets.
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                if role == "scoping_chat":
                    text = council.scope_chat(
                        [{"role": "user", "text": "Give one short cheap kill test for L2=0.1, without running anything."}],
                        {"recommended_direction": "Compare ridge L2=0.1 with baseline L2=0 on two fixed seeds."},
                        [], "Synthetic ridge regression comparison", "val_mse (lower is better)", "L2=0.1")
                    parsed = {"reply": text}
                elif role == "scoping":
                    parsed = council.scope_review(
                        "Synthetic test: improve ridge-regression validation MSE. Keep your answer compact with one proposed idea.",
                        "val_mse (lower is better)", "Test ridge L2=0.1 against L2=0.",
                        [{"key": "fixture_ridge", "title": "Synthetic ridge baseline", "year": "2026",
                          "authors": "Test fixture", "abstract": "A synthetic baseline has MSE 0.10; ridge L2=0.1 has MSE 0.08 on one held-out seed."}])
                elif role == "pi":
                    user = "Here is the current state of the research project. Decide if the agent needs a nudge. Return JSON per the schema.\n" + json.dumps({
                        "project": context["project"], "gpus": [{"index": 0, "util_pct": 98}],
                        "running": [{"run_id": "fixture_seed_repeat", "gpu_index": 0, "elapsed_seconds": 10, "metric": 0.08}],
                        "agent_tail": "Running the planned seed replication; no blockers.", "pending": []})
                    text = pi._call(model, pi.SYSTEM, user)
                    parsed = council._safe_parse(text)
                else:
                    slot = {"reviewer1": "gemini", "reviewer2": "openai", "tiebreaker": "claude"}[role]
                    user = council._ctx_text(context)
                    system = council.SYSTEM
                    if role == "tiebreaker":
                        system = council.TIEBREAKER_SYSTEM
                        user += "\nFINAL POSITIONS AFTER 3 ROUNDS OF DEBATE:\n" + json.dumps({
                            "gemini": {"verdict": "kept", "learning": "One seed improved MSE 0.10 to 0.08.", "rerank_pending": ["fixture_seed_repeat"], "new_ideas": [], "veto": []},
                            "openai": {"verdict": "inconclusive", "learning": "A second seed is needed.", "rerank_pending": ["fixture_seed_repeat"], "new_ideas": [], "veto": []}})
                    parsed = council._call_reviewer(slot, system, user, cfg)
            raw_text = outputs[-1] if outputs else ""
            raw = {"reply": raw_text} if role == "scoping_chat" else council._strip_fences(raw_text)
            if role != "scoping_chat":
                try:
                    raw = json.loads(raw)
                except Exception:
                    match = re.search(r"\{.*\}", raw, re.DOTALL)
                    raw = json.loads(match.group()) if match else {}
            schema_errors = _validate(role, raw, parsed) if isinstance(parsed, dict) and isinstance(raw, dict) else ["no_parsed_object"]
            failed_request = next((r for r in requests if r.get("http_status", 200) != 200 or r.get("error_type")), None)
            result.update(status="provider_error" if failed_request else "passed" if not schema_errors and dispatches == [model] else "invalid_role_response",
                          schema_errors=schema_errors, dispatched_models=dispatches, requests=requests,
                          output_characters=len(raw_text), output_sha256=hashlib.sha256(raw_text.encode()).hexdigest(),
                          output_fields=sorted(raw) if isinstance(raw, dict) else [])
            if failed_request:
                result.update({k: v for k, v in failed_request.items() if k in {"http_status", "error_type", "error_category", "provider_code", "provider_param", "provider_type", "provider_status"}})
        except Exception as exc:
            result.update(status="provider_error" if requests else "failed", requests=requests,
                          dispatched_models=dispatches, **_error_metadata(exc))
            failed = next((r for r in requests if r.get("error_type")), {})
            result.update({k: v for k, v in failed.items() if k in {
                "http_status", "error_type", "error_category", "provider_code", "provider_param", "provider_type", "provider_status"}})
        finally:
            council._post_json, council._call_model = original_post, original_call
            council._call_text_model = original_text_call
            council._TIMEOUT, council._RETRY_DELAYS = original_timeout, original_retry
            db.engine.dispose()
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        return result


def _kill_worker_tree(proc):
    """Kill this isolated worker and descendants even if a CLI called setsid."""
    import psutil
    groups = {proc.pid}  # Every worker starts its own session.
    try:
        descendants = psutil.Process(proc.pid).children(recursive=True)
    except psutil.NoSuchProcess:
        descendants = []
    for child in descendants:
        try:
            groups.add(os.getpgid(child.pid))
        except ProcessLookupError:
            pass
    # Never target the harness's own group. Child groups are collected before
    # killing the worker, while ancestry can still be established reliably.
    groups.discard(os.getpgrp())
    for group in groups:
        try:
            os.killpg(group, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _subprocess_cell(model, role, timeout, max_output_tokens):
    started = time.monotonic()
    command = [sys.executable, str(Path(__file__).resolve()), "--worker", "--model", model,
               "--role", role, "--timeout", str(timeout), "--max-output-tokens", str(max_output_tokens)]
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            start_new_session=True)
    try:
        stdout, _ = proc.communicate(timeout=timeout + 15)
        return json.loads(stdout)
    except subprocess.TimeoutExpired:
        _kill_worker_tree(proc)
        proc.communicate()
        return {"model": model, "role": role, "status": "timeout", "inference": "real",
                "elapsed_seconds": round(time.monotonic() - started, 3), "fallback_allowed": False}
    except Exception as exc:
        return {"model": model, "role": role, "status": "worker_failed", "error_type": type(exc).__name__}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", action="append")
    parser.add_argument("--role", action="append", choices=ROLES + EXTRA_ROLES)
    parser.add_argument("--provider", action="append")
    parser.add_argument("--credentials-db")
    parser.add_argument("--output", default="/tmp/arui-model-matrix/advisor-results.json")
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    parser.add_argument("--concurrency", type=int, choices=(1, 2), default=2)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--resume", action="store_true", help="Keep completed cells in the output file")
    args = parser.parse_args()
    if args.worker:
        result = probe_role(args.model[0], args.role[0], timeout=args.timeout,
                            max_output_tokens=args.max_output_tokens)
        print(json.dumps(result), flush=True)
        return 0
    if args.credentials_db:
        load_credentials(args.credentials_db)
    from backend.app.model_registry import MODELS
    selected = args.model or [m["id"] for m in MODELS if m["provider"] in (args.provider or ["openai", "claude", "gemini"])]
    roles = args.role or list(ROLES)
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    results = json.loads(target.read_text()).get("results", []) if args.resume and target.exists() else []
    done = {(r["model"], r["role"]) for r in results}
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        cells = [pool.submit(_subprocess_cell, model, role, args.timeout, args.max_output_tokens)
                 for model in selected for role in roles if (model, role) not in done]
        for cell in as_completed(cells):
            result = cell.result()
            results.append(result)
            tmp = target.with_suffix(".tmp")
            tmp.write_text(json.dumps({"tested_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                                      "selected_models": selected, "roles": roles, "results": results}, indent=2))
            tmp.replace(target)
            print(json.dumps({k: result[k] for k in ("model", "role", "status", "http_status", "elapsed_seconds") if k in result}), flush=True)
    return 0 if all(r["status"] == "passed" for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
