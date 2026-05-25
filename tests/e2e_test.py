#!/usr/bin/env python3
"""autoresearcherUI — end-to-end integration test (the merge-to-main gate).

Exercises the whole system, hardware-free:

  1. boots the real backend (FastAPI + DuckDB + SQLite, demo mode off);
  2. triggers the orchestrator on the bundled `tiny-sgd` example project;
  3. the orchestrator parses ideas.md, runs the baseline, then schedules the
     remaining ideas highest-EV first, launching each as a real train.py
     subprocess that logs metrics back through the arui SDK;
  4. asserts the full pipeline via the public HTTP API — runs completed,
     metrics ingested into DuckDB, baseline beaten, journal + events written.

Only the LLM agent and the GPUs are faked (FakeAgent + CPU training); every
other component is the real thing. Pure standard library — no test deps.

Exit code 0 = pass, 1 = fail.  Run via tests/run_e2e.sh.
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
RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    RESULTS.append((bool(ok), label + (f"  ({detail})" if detail else "")))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}"
          + (f"  {detail}" if detail else ""))
    return bool(ok)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def get(url: str):
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read())


def post(url: str, body: dict | None = None):
    req = urllib.request.Request(
        url, data=json.dumps(body or {}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def main() -> int:
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    data_dir = tempfile.mkdtemp(prefix="arui-e2e-")
    log_path = os.path.join(data_dir, "backend.log")
    env = dict(os.environ)
    env.update(ARUI_PORT=str(port), ARUI_DATA_DIR=data_dir, ARUI_DEMO="0")

    print(f"\n=== autoresearcherUI e2e integration test ===")
    print(f"    port={port}  data_dir={data_dir}\n")

    logf = open(log_path, "w")
    proc = subprocess.Popen([sys.executable, "-m", "backend.main"],
                            cwd=ROOT, env=env, stdout=logf, stderr=logf)
    try:
        # ── wait for the backend ────────────────────────────────────────
        up = False
        for _ in range(60):
            try:
                if get(base + "/healthz").get("ok"):
                    up = True
                    break
            except Exception:
                time.sleep(0.5)
        if not check(up, "backend boots and serves /healthz"):
            raise SystemExit  # nothing else can run

        # ── trigger the autonomous research loop ────────────────────────
        started = post(base + "/api/dev/run-example")
        check(started.get("status") in ("started", "already_running"),
              "orchestrator started on the example project",
              str(started))

        # ── poll until the loop reports done ────────────────────────────
        project, done = {}, False
        for _ in range(180):                       # up to 180 s
            try:
                project = get(base + "/api/project")
                if project.get("status") == "done":
                    done = True
                    break
            except Exception:
                pass
            time.sleep(1)
        check(done, "research loop completes within the time budget",
              f"status={project.get('status')}")

        # ── inspect the results via the public API ──────────────────────
        runs = get(base + "/api/runs")
        ideas = get(base + "/api/ideas")
        events = get(base + "/api/events?limit=200")
        journal = get(base + "/api/journal")

        check(project.get("name") == "tiny-sgd",
              "project registered", project.get("name"))
        check(len(ideas) == 5, "all 5 ideas parsed from ideas.md",
              f"{len(ideas)} ideas")
        check(len(runs) >= 5, "every idea produced a run",
              f"{len(runs)} runs")

        terminal = {"kept", "discarded", "crashed"}
        check(all(r["status"] in terminal for r in runs),
              "all runs reached a terminal state",
              ", ".join(sorted({r["status"] for r in runs})))

        by_id = {r["id"]: r for r in runs}
        base_run = by_id.get("baseline")
        check(base_run is not None and base_run["status"] == "kept"
              and base_run["headline_metric"] is not None,
              "baseline run completed and was recorded",
              f"val_mse={base_run and base_run['headline_metric']}")

        non_base = [r for r in runs if not r["is_baseline"]]
        kept = [r for r in non_base if r["status"] == "kept"]
        lost = [r for r in non_base if r["status"] != "kept"]
        check(len(kept) >= 2, "at least two ideas beat the baseline",
              f"{[r['id'] for r in kept]}")
        check(len(lost) >= 1, "at least one idea failed / was discarded",
              f"{[r['id'] for r in lost]}")

        agg = by_id.get("aggressive-lr")
        check(agg is not None and agg["status"] != "kept",
              "the deliberate failure case (aggressive-lr) did not survive",
              agg and agg["status"])

        if kept and base_run and base_run["headline_metric"] is not None:
            best = min(kept, key=lambda r: r["headline_metric"])
            check(best["headline_metric"] < base_run["headline_metric"],
                  "best kept run improves on the baseline (lower val_mse)",
                  f"{best['id']} {best['headline_metric']:.4f} "
                  f"< {base_run['headline_metric']:.4f}")

        # ── metrics actually flowed through arui -> DuckDB ──────────────
        bm = get(base + "/api/runs/baseline/metrics")
        check(len(bm.get("train_loss", [])) > 0,
              "baseline metrics ingested into DuckDB via the arui SDK",
              f"{len(bm.get('train_loss', []))} train_loss points")
        fm = get(base + "/api/runs/faster-lr/metrics")
        check("val_mse" in fm and len(fm.get("train_loss", [])) > 0,
              "faster-lr logged train_loss + val_mse")

        # ── journal + event timeline ───────────────────────────────────
        check(len(journal) >= 2, "research journal was written",
              f"{len(journal)} entries")
        etypes = {e["type"] for e in events}
        check("run_started" in etypes and "run_finished" in etypes,
              "lifecycle events were emitted",
              ", ".join(sorted(etypes)))

        # ── idea statuses updated ───────────────────────────────────────
        istat = {i["idea_id"]: i["status"] for i in ideas}
        check(all(istat.get(i) in ("success", "failed", "unclear")
                  for i in ["baseline", "faster-lr", "aggressive-lr"]),
              "idea statuses updated from run outcomes", str(istat))

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

    passed = sum(1 for ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\n=== {passed}/{total} checks passed ===")
    if passed != total:
        print("\n--- backend log (tail) ---")
        try:
            with open(log_path) as f:
                print("".join(f.readlines()[-40:]))
        except OSError:
            pass
        return 1
    print("e2e integration test PASSED — safe to merge.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
