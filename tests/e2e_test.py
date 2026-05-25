#!/usr/bin/env python3
"""autoresearcherUI — end-to-end integration test (the merge-to-main gate).

Exercises the whole system, hardware-free and with no fake data:

  1. boots the real backend (FastAPI + DuckDB + SQLite);
  2. on startup the backend auto-runs the REAL orchestrator on the bundled
     tiny-sgd example project — a ~44-experiment hyperparameter search, each a
     real train.py subprocess that logs metrics through the arui SDK;
  3. asserts the whole pipeline via the public HTTP API — experiments ran,
     metrics ingested into DuckDB, the running-best beat the baseline, some
     ideas diverged, journal + events written.

Only the LLM agent and the GPUs are faked (FakeAgent + CPU training). Pure
standard library. Exit 0 = pass, 1 = fail. Run via tests/run_e2e.sh.
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


def get(url):
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.loads(r.read())


def main():
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    data_dir = tempfile.mkdtemp(prefix="arui-e2e-")
    log_path = os.path.join(data_dir, "backend.log")
    env = dict(os.environ)
    env.update(ARUI_PORT=str(port), ARUI_DATA_DIR=data_dir, ARUI_AUTORUN="1")

    print(f"\n=== autoresearcherUI e2e integration test ===")
    print(f"    port={port}  data_dir={data_dir}\n")

    logf = open(log_path, "w")
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

        # the orchestrator auto-runs on startup — poll until it reports done
        project, done = {}, False
        for _ in range(240):
            try:
                project = get(base + "/api/project")
                if project.get("status") == "done":
                    done = True; break
            except Exception:
                pass
            time.sleep(1)
        check(done, "autonomous research loop completes",
              f"status={project.get('status')}")

        runs = get(base + "/api/runs")
        ideas = get(base + "/api/ideas")
        events = get(base + "/api/events?limit=400")
        journal = get(base + "/api/journal")

        check(project.get("name") == "tiny-sgd", "project registered",
              project.get("name"))
        check(len(ideas) >= 40, "full idea search parsed from ideas.md",
              f"{len(ideas)} ideas")
        check(len(runs) >= 40, "every idea produced a real run",
              f"{len(runs)} runs")

        terminal = {"kept", "discarded", "crashed"}
        check(all(r["status"] in terminal for r in runs),
              "all runs reached a terminal state",
              ", ".join(sorted({r["status"] for r in runs})))

        by_id = {r["id"]: r for r in runs}
        base_run = by_id.get("baseline")
        check(base_run and base_run["status"] == "kept"
              and base_run["headline_metric"] is not None,
              "baseline run completed and recorded",
              f"val_mse={base_run and base_run['headline_metric']}")

        kept = [r for r in runs if r["status"] == "kept" and not r["is_baseline"]]
        crashed = [r for r in runs if r["status"] == "crashed"]
        check(len(kept) >= 5, "many ideas beat the baseline", f"{len(kept)} kept")
        check(len(crashed) >= 1, "unstable ideas diverged and were caught",
              f"{len(crashed)} crashed")

        finite = [r for r in runs if r["headline_metric"] is not None
                  and r["headline_metric"] < 5e4 and not r["is_baseline"]]
        if finite and base_run:
            best = min(finite, key=lambda r: r["headline_metric"])
            check(best["headline_metric"] < base_run["headline_metric"],
                  "running-best beats the baseline",
                  f"{best['id']} {best['headline_metric']:.4f} "
                  f"< {base_run['headline_metric']:.4f}")

        bm = get(base + "/api/runs/baseline/metrics")
        check(len(bm.get("train_loss", [])) > 0,
              "metrics ingested into DuckDB via the arui SDK",
              f"{len(bm.get('train_loss', []))} points")

        check(len(journal) >= 2, "research journal written", f"{len(journal)}")
        etypes = {e["type"] for e in events}
        check("run_started" in etypes and "run_finished" in etypes,
              "lifecycle events emitted", ", ".join(sorted(etypes)))

    except SystemExit:
        pass
    except Exception as e:
        check(False, "test harness ran without error", repr(e))
    finally:
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
            print("".join(open(log_path).readlines()[-45:]))
        except OSError:
            pass
        return 1
    print("e2e integration test PASSED — safe to merge.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
