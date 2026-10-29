#!/usr/bin/env python3
"""Opt-in startup integration checks using the actual research/author launchers.

Runs only bounded synthetic file tasks on private tmux servers. Provider keys
are read from the supplied read-only database into child environments. The
research pip-path installer is skipped;
everything from role configuration through tmux launch and prompt delivery is
the application implementation. No training or production session is touched.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

from probe_agent_roles import ROOT, classify_failure, read_credentials, read_effective_models, sanitize


def worker(args):
    root = Path(tempfile.mkdtemp(prefix="startup-" + args.role + "-", dir=args.output.parent / "fixtures"))
    config = root / ".probe-config"
    config.mkdir(mode=0o700)
    private_tmux = Path("/tmp/arui-start-" + uuid.uuid4().hex[:10])
    private_tmux.mkdir(mode=0o700)
    keys = read_credentials(args.credentials_db)
    os.environ.update(keys)
    if keys.get("OPENAI_API_KEY"):
        os.environ["CODEX_API_KEY"] = keys["OPENAI_API_KEY"]
    os.environ.update({"ARUI_DISABLE_BG": "1", "ARUI_TELEMETRY_DISABLED": "1",
                       "ARUI_DATA_DIR": str(root / ".data"), "ARUI_WORKSPACE_DIR": str(root),
                       "CLAUDE_CONFIG_DIR": str(config), "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                       "CODEX_HOME": str(config),
                       "TMUX_TMPDIR": str(private_tmux), "TMUX": "", "IS_SANDBOX": "1", "PWD": str(root)})
    from backend.app import db, models, agent, author_agent, agent_cli, model_registry
    db.init_db()
    with db.SessionLocal() as session:
        session.add(models.Project(id="probe", name="Synthetic startup", repo_path=str(root)))
        session.add(models.Setting(key="onboarding", value={
            "repo_name": "probe", "research_agent_model": args.model, "author_agent_model": args.model,
            "research_agent_effort": "low", "author_agent_effort": "low"}))
        session.commit()
    folder = root / "latex" if args.role == "author" else root
    folder.mkdir(exist_ok=True)
    (folder / "input.json").write_text('{"numbers":[2,3,5]}')
    payload = {"model": args.model, "role": args.role, "sum": 10, "turn": 1}
    restrictions = ("This is a bounded synthetic integration check. Only read/write files in the CURRENT directory. "
                    "Never inspect environment variables, credentials, parent directories or network. Do not train, "
                    "install, launch background work, or spawn agents. Use actual file tools. ")
    prompt = restrictions + "Read input.json and write role_result.json as exactly " + json.dumps(payload) + ". "
    if args.role == "author":
        prompt += "Also write draft.tex as a minimal article with title Synthetic Test and text 'Evidence sum is 10'. "
    prompt += "This fixture is complete after those changes: stop and reply PROBE_TURN_ONE_DONE."
    second = restrictions + "Read role_result.json and change ONLY turn to 2. "
    if args.role == "author":
        second += "Append '% second turn verified' to draft.tex. "
    second += "Stop and reply PROBE_TURN_TWO_DONE."
    result = {"model": args.model, "role": args.role, "workspace": str(folder),
              "transport": "RealAgent.start" if args.role == "research" else "author_agent.start + feed_brief",
              "status": "failed", "first_turn": False, "second_turn": False, "effective_models": [],
              "started_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    started = time.time()
    screens = []
    provider = model_registry.provider_for(args.model)
    cli_config = Path(agent_cli.prepare_environment(args.model).get("GEMINI_CLI_HOME", str(config)))
    def tmux(*argv):
        return subprocess.run(["tmux", *argv], capture_output=True, text=True, timeout=5)
    def valid(turn):
        try:
            if json.loads((folder / "role_result.json").read_text()) != {**payload, "turn": turn}:
                return False
            if args.role == "author":
                text = (folder / "draft.tex").read_text()
                if "Synthetic Test" not in text or "Evidence sum is 10" not in text:
                    return False
                if turn == 2 and "% second turn verified" not in text:
                    return False
            return True
        except (OSError, ValueError):
            return False
    try:
        subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
        # The pip installer persists outside the fixture and is unrelated
        # to tmux startup/prompt delivery. Keys remain environment-only here.
        agent._install_arui_pth = lambda *a, **kw: None
        if args.role == "research":
            instance = agent.RealAgent(str(root), "probe", "http://127.0.0.1:1", str(ROOT),
                                       setup_prompt=prompt, session="probe", model=args.model, effort="low",
                                       anthropic_key=keys.get("ANTHROPIC_API_KEY", ""),
                                       openai_key=keys.get("OPENAI_API_KEY", ""), gemini_key=keys.get("GEMINI_API_KEY", ""))
            instance.start()
        else:
            author_agent.SESSION = "probe"
            author_agent.SYSTEM = restrictions
            author_agent._AUTHOR_BRIEF = prompt
            startup = author_agent.start()
            if startup.get("status") != "started":
                result["failure"] = "author_start_rejected"
                return result
            result["feeder_observed_work"] = author_agent.feed_brief("probe", prompt, ready_timeout=45)
        for turn in (1, 2):
            if turn == 2:
                tmux("send-keys", "-t", "probe", "-l", second)
                time.sleep(0.8)
                tmux("send-keys", "-t", "probe", "Enter")
            deadline = time.monotonic() + args.timeout
            while time.monotonic() < deadline:
                screen = tmux("capture-pane", "-t", "probe", "-p", "-S", "-120").stdout
                screens.append(screen)
                if valid(turn):
                    result["first_turn" if turn == 1 else "second_turn"] = True
                    break
                failure = classify_failure(screen)
                if failure:
                    result["failure"] = failure
                    return result
                time.sleep(0.5)
            else:
                result["failure"] = f"turn_{turn}_timeout"
                return result
            time.sleep(1)
        result["effective_models"] = read_effective_models(folder, provider, cli_config, started)
        if result["effective_models"] == [args.model]:
            result["status"] = "passed"
        else:
            result["failure"] = "model_mismatch_or_unverified"
        return result
    except Exception as exc:
        result["failure"] = type(exc).__name__
        return result
    finally:
        result["duration_seconds"] = round(time.time() - started, 2)
        (root / "sanitized-transcript.txt").write_text(sanitize("\n--- screen ---\n".join(screens[-6:]), keys))
        tmux("kill-server")
        args.output.write_text(json.dumps(result, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials-db", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("/tmp/arui-model-matrix/startup-results.json"))
    parser.add_argument("--models", nargs="+", default=["claude-haiku-4-5-20251001", "gpt-5.6-sol", "gemini-3.1-flash-lite"])
    parser.add_argument("--timeout", type=float, default=75)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--model")
    parser.add_argument("--role", choices=["research", "author"])
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    (args.output.parent / "fixtures").mkdir(exist_ok=True)
    if args.worker:
        worker(args)
        return
    def run(model, role):
        output = args.output.parent / ("startup-" + model + "-" + role + ".json")
        output.unlink(missing_ok=True)
        completed = subprocess.run([sys.executable, __file__, "--worker", "--model", model, "--role", role,
                        "--credentials-db", str(args.credentials_db), "--output", str(output),
                        "--timeout", str(args.timeout)], capture_output=True, timeout=240)
        if completed.returncode or not output.exists():
            return {"model": model, "role": role, "status": "failed", "failure": "startup_probe_process_failed"}
        result = json.loads(output.read_text())
        print(json.dumps({k: result.get(k) for k in ("model", "role", "status", "failure", "effective_models")}), flush=True)
        return result
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run, model, role) for model in args.models for role in ("research", "author")]
        results = [future.result() for future in concurrent.futures.as_completed(futures)]
    args.output.write_text(json.dumps({"results": results}, indent=2))


if __name__ == "__main__":
    main()
