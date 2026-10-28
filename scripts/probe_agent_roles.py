#!/usr/bin/env python3
"""Opt-in real coding CLI probes, isolated from the running research service.

Reads credentials from the specified SQLite database in read-only mode and
passes them in child environments only. Each case gets a private tmux socket,
synthetic workspace, and two bounded file-editing turns. No production session
is inspected or changed. Reports never include credentials or raw exceptions.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.app.agent_cli import command, prepare_environment
from backend.app.model_registry import AGENT_CLI_MODELS, efforts_for, provider_for
from backend.app.provider_config import environment_from_config


def read_credentials(path: Path) -> dict:
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        row = db.execute("SELECT value FROM setting WHERE key='onboarding'").fetchone()
    return environment_from_config(json.loads(row[0])) if row else {}


def sanitize(text: str, credentials: dict) -> str:
    for value in sorted(credentials.values(), key=len, reverse=True):
        if value:
            text = text.replace(value, "[REDACTED]")
            if len(value) >= 30:
                text = text.replace(value[:7] + "..." + value[-20:], "[REDACTED]")
    text = re.sub(r"\b(?:sk|ghp)[-_][A-Za-z0-9_\-]{12,}", "[REDACTED]", text)
    return text


def classify_failure(screen: str) -> str:
    low = screen.lower()
    for phrases, category in [
        (("model_not_found", "model is not found", "unknown model", "does not exist", "model not found", "invalid model", "not a valid model", "not supported when using"), "model_unavailable"),
        (("authentication failed", "invalid api key", "api key not valid", "401 unauthorized", "not logged in"), "authentication_failed"),
        (("quota exceeded", "resource_exhausted", "insufficient_quota", "rate limit", "rate_limit"), "provider_limited"),
        (("error: unknown option", "unknown argument", "unrecognized subcommand"), "cli_arguments_invalid"),
        (("requires an interactive terminal", "stdin is not a terminal", "panic", "segmentation fault"), "cli_runtime_error"),
    ]:
        if any(phrase in low for phrase in phrases):
            return category
    return ""


def read_effective_models(workspace: Path, provider: str, config_dir: Path, started: float) -> list[str]:
    found = set()
    roots = [config_dir] if provider in {"claude", "gemini"} else [
        config_dir / "sessions" / dt.datetime.now(dt.timezone.utc).strftime("%Y/%m/%d"),
        Path.home() / ".codex" / "sessions" / dt.datetime.now(dt.timezone.utc).strftime("%Y/%m/%d")]
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.json*"):
            # Settings merely describe intent. Only conversation runtime
            # records count as evidence of the model actually executing.
            if provider == "claude" and (path.suffix != ".jsonl" or "projects" not in path.parts):
                continue
            if provider == "gemini" and ("chats" not in path.parts or not path.name.startswith("session-")):
                continue
            try:
                if path.stat().st_mtime < started - 2 or path.stat().st_size > 8_000_000:
                    continue
                raw = path.read_text(errors="replace")
                if provider == "openai" and str(workspace) not in raw:
                    continue
                records = []
                if path.suffix == ".jsonl":
                    for line in raw.splitlines():
                        try:
                            records.append(json.loads(line))
                        except ValueError:
                            pass
                else:
                    try:
                        records = [json.loads(raw)]
                    except ValueError:
                        pass
                def walk(node):
                    if isinstance(node, dict):
                        model = node.get("model")
                        if isinstance(model, str) and model.startswith(("gpt-", "claude-", "gemini-")):
                            found.add(model)
                        for value in node.values():
                            if isinstance(value, (dict, list)):
                                walk(value)
                    elif isinstance(node, list):
                        for value in node:
                            walk(value)
                for record in records:
                    walk(record)
            except OSError:
                continue
    return sorted(found)


def run_case(model: str, role: str, credentials: dict, base: Path, timeout: float) -> dict:
    provider = provider_for(model)
    label = re.sub(r"[^a-zA-Z0-9_.-]", "_", model) + "-" + role
    workspace = Path(tempfile.mkdtemp(prefix=label + "-", dir=base / "fixtures"))
    config_dir = workspace / ".probe-config"
    config_dir.mkdir(mode=0o700)
    data_dir = workspace / ".probe-data"
    data_dir.mkdir(mode=0o700)
    socket = "/tmp/arui-probe-" + uuid.uuid4().hex[:16] + ".sock"
    tmux = ["tmux", "-S", socket]
    env = {**os.environ, **credentials, "TMUX": "", "IS_SANDBOX": "1", "PWD": str(workspace),
           "ARUI_DISABLE_BG": "1", "ARUI_TELEMETRY_DISABLED": "1",
           "ARUI_DATA_DIR": str(data_dir), "ARUI_WORKSPACE_DIR": str(workspace),
           "GEMINI_CLI_TRUST_WORKSPACE": "true", "GEMINI_CLI_HOME": str(config_dir),
           "CODEX_HOME": str(config_dir),
           "CLAUDE_CONFIG_DIR": str(config_dir), "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
    env.update(prepare_environment(model, state_dir=config_dir))
    if provider == "openai" and credentials.get("OPENAI_API_KEY"):
        env["CODEX_API_KEY"] = credentials["OPENAI_API_KEY"]
    if provider == "claude":
        # Same onboarding flags production RealAgent prepares, without
        # persisting any credential or changing the user's global config.
        (config_dir / ".claude.json").write_text(json.dumps({
            "hasCompletedOnboarding": True, "bypassPermissionsModeAccepted": True,
            "dangerouslySkipPermissionsModeAccepted": True,
            "hasAcceptedDangerouslySkipPermissions": True,
            "hasTrustDialogAccepted": True, "trustDialogAccepted": True}))
    # No fallback flag, provider override, model alias, or testing replacement.
    efforts = efforts_for(model)
    output = workspace / "role_result.json"
    payload = {"model": model, "role": role, "turn": 1, "sum": 10}
    (workspace / "input.json").write_text(json.dumps({"numbers": [2, 3, 5]}))
    restrictions = ("This is a bounded synthetic CLI integration test. Only read/write files inside the CURRENT directory. "
                    "Never inspect environment variables, credentials or other directories. Do not train, install, spawn agents, "
                    "start background processes, or use network tools. Use your actual file/shell tools; do not only describe changes. ")
    prompt = restrictions + "Read input.json. Write role_result.json as exactly " + json.dumps(payload) + ". "
    if role == "author":
        prompt += "Also write draft.tex containing a minimal article with title Synthetic Test and text 'Evidence sum is 10'. "
    prompt += "Then stop and reply PROBE_TURN_ONE_DONE."
    second = restrictions + "Read role_result.json and change ONLY its turn field to 2. "
    if role == "author":
        second += "Append the LaTeX comment '% second turn verified' to draft.tex. "
    second += "Then stop and reply PROBE_TURN_TWO_DONE."
    initial_prompt = prompt if role == "research" and provider in {"openai", "gemini"} else ""
    provider, inner = command(model, "low" if "low" in efforts else "", initial_prompt, workspace=workspace)
    result = {"model": model, "role": role, "provider": provider,
              "status": "failed", "first_turn": False, "second_turn": False,
              "selected_model_arg": model, "effective_models": [],
              "initial_prompt_delivery": "command_argument" if initial_prompt else "interactive_feed",
              "strict_model_config": True,
              "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
              "workspace": str(workspace), "transport": "production agent_cli.command + private tmux"}
    started = time.time()
    screens = []
    handled = set()
    def run(args, **kwargs):
        return subprocess.run(tmux + args, capture_output=True, text=True, timeout=8, env=env, **kwargs)
    def send(text):
        run(["send-keys", "-t", "probe", "-l", text])
        time.sleep(0.8)
        run(["send-keys", "-t", "probe", "Enter"])
    def capture():
        return run(["capture-pane", "-t", "probe", "-p", "-S", "-250"]).stdout
    def consent(screen):
        low = screen.lower()
        if "do you want to use this api key" in low and "key" not in handled:
            handled.add("key")
            run(["send-keys", "-t", "probe", "Up", "Enter"])
            return True
        if "yes, i trust this folder" in low and "no, exit" in low and "trust" not in handled:
            handled.add("trust")
            run(["send-keys", "-t", "probe", "Home", "Down", "Enter"])
            return True
        patterns = [
            ("theme", "choose the text style", "Enter"),
            ("theme", "choose your preferred theme", "Enter"),
            ("key", "do you want to use this api key", "1"),
            ("trust", "do you trust", "1"),
            ("bypass", "yes, i accept", "2"),
            ("codextrust", "trust this directory", "Enter"),
        ]
        for key, phrase, answer in patterns:
            if phrase in low and key not in handled:
                handled.add(key)
                if answer == "Enter":
                    run(["send-keys", "-t", "probe", "Enter"])
                else:
                    send(answer)
                return True
        return False
    def valid(turn):
        try:
            value = json.loads(output.read_text())
            if value != {**payload, "turn": turn}:
                return False
            if role == "author":
                text = (workspace / "draft.tex").read_text()
                if "Synthetic Test" not in text or "Evidence sum is 10" not in text:
                    return False
                if turn == 2 and "% second turn verified" not in text:
                    return False
            return True
        except (OSError, ValueError):
            return False
    try:
        subprocess.run(["git", "init", "-q", str(workspace)], capture_output=True, check=True, timeout=10)
        run(["new-session", "-d", "-s", "probe", "-x", "160", "-y", "55", "-c", str(workspace)])
        run(["set-window-option", "-t", "probe", "remain-on-exit", "on"])
        run(["set-window-option", "-t", "probe", "alternate-screen", "off"])
        run(["respawn-pane", "-k", "-t", "probe", "-c", str(workspace), inner])
        # Do not inject a prompt until the provider's REPL has rendered.
        deadline = time.monotonic() + min(timeout, 45)
        ready = False
        while time.monotonic() < deadline:
            screen = capture()
            screens.append(screen)
            if "update available" in screen.lower() or "update now" in screen.lower():
                result["failure"] = "unexpected_update_prompt"
                return result
            if consent(screen):
                time.sleep(1)
                continue
            if any(mark in screen.lower() for mark in ("❯", "›", "/help", "type your message", "ask anything", "ask codex")):
                ready = True
                break
            failure = classify_failure(screen)
            if failure:
                result["failure"] = failure
                return result
            time.sleep(0.5)
        if not ready:
            result["failure"] = "cli_not_ready"
            return result
        for turn, instruction in [(1, prompt), (2, second)]:
            if turn != 1 or not initial_prompt:
                send(instruction)
            deadline = time.monotonic() + timeout
            submitted_at = time.monotonic()
            nudged = False
            while time.monotonic() < deadline:
                screen = capture()
                screens.append(screen)
                if valid(turn):
                    result["first_turn" if turn == 1 else "second_turn"] = True
                    break
                failure = classify_failure(screen)
                if failure:
                    result["failure"] = failure
                    return result
                if not nudged and time.monotonic() - submitted_at > 9 and not valid(turn):
                    # Match the production author feeder's one Enter retry:
                    # large pasted prompts can finish rendering after the
                    # initial submit key, particularly in Gemini's TUI.
                    if "yolo mode This is a bounded" in screen:
                        run(["send-keys", "-t", "probe", "Enter"])
                        nudged = True
                time.sleep(0.5)
            else:
                result["failure"] = f"turn_{turn}_timeout"
                return result
            time.sleep(1.0)
        result["effective_models"] = read_effective_models(workspace, provider, config_dir, started)
        if result["effective_models"] and any(m != model for m in result["effective_models"]):
            result["failure"] = "model_mismatch"
        elif model not in result["effective_models"]:
            # A header is selection evidence, not proof of server execution.
            result["failure"] = "effective_model_unverified"
        else:
            result["status"] = "passed"
        return result
    except Exception as exc:
        result["failure"] = type(exc).__name__
        return result
    finally:
        result["duration_seconds"] = round(time.time() - started, 2)
        if not result["effective_models"]:
            result["effective_models"] = read_effective_models(workspace, provider, config_dir, started)
        transcript = sanitize("\n\n--- screen ---\n\n".join(screens[-8:]), credentials)
        (workspace / "sanitized-transcript.txt").write_text(transcript)
        run(["kill-server"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials-db", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("/tmp/arui-model-matrix/agent-results.json"))
    parser.add_argument("--models", nargs="*")
    parser.add_argument("--roles", nargs="+", default=["research", "author"], choices=["research", "author"])
    parser.add_argument("--timeout", type=float, default=75)
    parser.add_argument("--workers", type=int, choices=[1, 2], default=2)
    args = parser.parse_args()
    credentials = read_credentials(args.credentials_db)
    base = args.output.parent
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    (base / "fixtures").mkdir(exist_ok=True, mode=0o700)
    models = args.models or sorted(m for m in AGENT_CLI_MODELS if provider_for(m) in {"openai", "claude", "gemini"})
    records = []
    if args.output.exists():
        records = json.loads(args.output.read_text()).get("results", [])
    def write(result):
        nonlocal records
        records = [r for r in records if (r["model"], r["role"]) != (result["model"], result["role"])] + [result]
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(json.dumps({"generated_at": dt.datetime.now(dt.timezone.utc).isoformat(), "results": records}, indent=2))
        temporary.replace(args.output)
        print(json.dumps({key: result.get(key) for key in ("model", "role", "status", "failure", "first_turn", "second_turn", "effective_models", "duration_seconds")}), flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(run_case, model, role, credentials, base, args.timeout) for model in models for role in args.roles]
        for future in concurrent.futures.as_completed(futures):
            write(future.result())


if __name__ == "__main__":
    main()
