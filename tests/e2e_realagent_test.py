#!/usr/bin/env python3
"""autoresearcherUI — RealAgent end-to-end test.

Exercises the real-mode path with no LLM and no GPU:

  1. boots the real backend (onboarding mode);
  2. completes onboarding — which launches RealAgent, which starts an
     autonomous agent in a tmux session (here the deterministic mock agent
     instead of the real `claude` CLI, via ARUI_CLAUDE_BIN);
  3. the agent runs a real experiment sequence, each logging through the arui
     SDK;
  4. asserts the dashboard filled with real runs + metrics via the public API.

Pure standard library. Exit 0 = pass, 1 = fail.
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = []


def check(ok, label, detail=""):
    RESULTS.append((bool(ok), label))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  {detail}" if detail else ""))
    return bool(ok)


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]; s.close(); return p


def get(u):
    with urllib.request.urlopen(u, timeout=15) as r:
        return json.loads(r.read())


def post(u, b):
    rq = urllib.request.Request(u, data=json.dumps(b).encode(),
                                headers={"Content-Type": "application/json"},
                                method="POST")
    with urllib.request.urlopen(rq, timeout=15) as r:
        return json.loads(r.read())


def main():
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    data_dir = tempfile.mkdtemp(prefix="arui-real-")
    logp = os.path.join(data_dir, "backend.log")
    env = dict(os.environ)
    env.update(ARUI_PORT=str(port), ARUI_DATA_DIR=data_dir,
               ARUI_CLAUDE_BIN=f"{sys.executable} {ROOT}/tests/mock_claude.py")
    env.pop("ARUI_AUTORUN", None)

    print(f"\n=== autoresearcherUI RealAgent e2e test ===\n    port={port}\n")
    logf = open(logp, "w")
    proc = subprocess.Popen([sys.executable, "-m", "backend.main"],
                            cwd=ROOT, env=env, stdout=logf, stderr=logf)
    try:
        up = False
        for _ in range(60):
            try:
                if get(base + "/healthz").get("ok"):
                    up = True; break
            except Exception:
                time.sleep(0.5)
        if not check(up, "backend boots and serves /healthz"):
            raise SystemExit

        check(get(base + "/api/project") == {},
              "fresh instance starts at onboarding (no project)")

        r = post(base + "/api/onboarding",
                 {"repo_name": "arc-trm-test", "metric": "val_mse",
                  "claude_token": "sk-ant-test", "purpose": "RealAgent e2e"})
        check(r.get("status") == "started",
              "onboarding launches the RealAgent", str(r))

        # Terminal statuses under the post-RESEARCH_IMPROVEMENT_PLAN #4
        # taxonomy: a run is terminal once /api/track/finish has fired,
        # which writes one of {kept_novel, kept_replicate, success_smoke,
        # crashed, discarded}. The legacy plain "kept" is kept here so
        # the FakeAgent orchestrator path (which still uses the old
        # labels) is also satisfied — both tests share this helper-ish
        # logic via copy/paste, so update it in both places when the
        # taxonomy grows again.
        TERMINAL = ("kept", "kept_novel", "kept_replicate", "success_smoke",
                    "crashed", "discarded")
        runs = []
        for _ in range(180):
            runs = get(base + "/api/runs")
            done = [x for x in runs if x["status"] in TERMINAL]
            if len(runs) >= 10 and len(done) == len(runs):
                break
            time.sleep(1)
        check(len(runs) >= 10,
              "the autonomous agent ran the full experiment sequence",
              f"{len(runs)} runs")
        non_terminal = [(x["id"], x["status"]) for x in runs
                        if x["status"] not in TERMINAL]
        check(runs and not non_terminal,
              "all runs reached a terminal state",
              ("non-terminal: " + ", ".join(f"{rid}={st}"
                                             for rid, st in non_terminal)
               if non_terminal else ""))

        by = {x["id"]: x for x in runs}
        brun = by.get("baseline")
        check(brun and brun["headline_metric"] is not None,
              "baseline run recorded",
              f"val_mse={brun and brun['headline_metric']}")

        proj = get(base + "/api/project")
        check(proj.get("name") == "arc-trm-test", "project registered",
              proj.get("name"))
        check(proj.get("baseline_metric") is not None,
              "baseline metric resolved on the project")

        bm = get(base + "/api/runs/baseline/metrics")
        check(len(bm.get("train_loss", [])) > 0,
              "metrics ingested via the arui SDK into DuckDB",
              f"{len(bm.get('train_loss', []))} points")

        finite = [x for x in runs if x["headline_metric"] is not None
                  and x["headline_metric"] < 5e4 and not x["is_baseline"]]
        if finite and brun:
            best = min(finite, key=lambda x: x["headline_metric"])
            check(best["headline_metric"] < brun["headline_metric"],
                  "the agent improved on the baseline",
                  f"{best['id']} {best['headline_metric']:.4f} "
                  f"< {brun['headline_metric']:.4f}")

        crashed = [x for x in runs if x["status"] == "crashed"]
        check(len(crashed) >= 1, "a diverged experiment was caught",
              f"{len(crashed)} crashed")

        events = get(base + "/api/events?limit=200")
        check(len(events) >= 2, "lifecycle events recorded", f"{len(events)}")

        term = get(base + "/api/agent/terminal")
        check(isinstance(term.get("text"), str) and bool(term["text"].strip()),
              "agent terminal endpoint returns the session output",
              f"{len(term.get('text', ''))} chars")
        snd = post(base + "/api/agent/send", {"text": "status?"})
        check("ok" in snd, "agent send endpoint responds", str(snd))

    except SystemExit:
        pass
    except Exception as e:
        check(False, "test harness ran without error", repr(e))
    finally:
        subprocess.run(["tmux", "kill-session", "-t", "agent"],
                       capture_output=True)
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        logf.close()

    ok = sum(1 for p, _ in RESULTS if p)
    print(f"\n=== {ok}/{len(RESULTS)} checks passed ===")
    if ok != len(RESULTS):
        print("\n--- backend log (tail) ---")
        try:
            print("".join(open(logp).readlines()[-45:]))
        except OSError:
            pass
        return 1
    print("RealAgent e2e test PASSED.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
