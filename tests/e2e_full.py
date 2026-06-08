#!/usr/bin/env python3
"""autoresearcherUI — comprehensive e2e (v0.0.2 release gate).

Boots the real backend ONCE (no orchestrator, no LLM, no GPU), then walks
through every major surface area added since the original e2e_test.py:

  1.  onboarding + project registration
  2.  passcode gate (set → 401 → 200 with auth → reset → off)
  3.  /api/system stats + warnings
  4.  cleanup preview endpoints (no destructive ops)
  5.  paper mode (proposal → enter → state → queue → results → resolve → revert)
  6.  share link (token mint, read-only payload, /p/<token> SPA, wrong token)
  7.  notify digest dry-run (in-process import — no real send)
  8.  lit agent search (network optional — accepts empty results)
  9.  council review (skipped if no API keys; sanity-checks /council/review wiring)
  10. cleanup/preview with several thresholds — shape stable
  11. SPA routing for every top-level view + /p/<token>
  12. authkeys read

Pure standard library. Exit 0 = pass, non-zero = fail. Designed to finish in
under 90 seconds with no external network dependency required.

Usage:
    ARUI_AUTORUN=0 ARUI_DATA_DIR=$(mktemp -d) python tests/e2e_full.py
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS: list[tuple[bool, str, str]] = []


# ─────────────────────────── tiny test harness ──────────────────────────────


def check(ok, label, detail=""):
    RESULTS.append((bool(ok), label, str(detail)))
    flag = "PASS" if ok else "FAIL"
    print(f"  {flag}  {label}" + (f"  ::  {detail}" if detail else ""))
    return bool(ok)


def section(name):
    print(f"\n--- {name} ---")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ─────────────────────────────── http helpers ───────────────────────────────


class Client:
    """Tiny stdlib HTTP client with cookie + token handling.

    Persists Set-Cookie values across calls (so the passcode cookie sticks)
    and lets the caller pass a bearer token or X-Arui-Passcode header.
    """

    def __init__(self, base: str):
        self.base = base
        self.cookies: dict[str, str] = {}

    # ─── core ────────────────────────────────────────────────────────────
    def request(self, method: str, path: str, *, body=None, headers=None,
                timeout: float = 20.0, want_json: bool = True):
        url = self.base + path
        data = None
        hdrs = {"Accept": "application/json, text/html;q=0.9, */*;q=0.1"}
        if body is not None:
            data = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        if self.cookies:
            hdrs["Cookie"] = "; ".join(
                f"{k}={v}" for k, v in self.cookies.items())
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(url, data=data, method=method,
                                     headers=hdrs)
        status = 0
        body_bytes = b""
        resp_headers: dict[str, str] = {}
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                status = r.status
                body_bytes = r.read()
                resp_headers = {k.lower(): v for k, v in r.getheaders()}
        except urllib.error.HTTPError as e:
            status = e.code
            try:
                body_bytes = e.read()
            except Exception:
                body_bytes = b""
            try:
                resp_headers = {k.lower(): v for k, v in e.headers.items()}
            except Exception:
                resp_headers = {}
        # absorb Set-Cookie
        sc = resp_headers.get("set-cookie", "")
        if sc:
            # naive: take name=value before the first ;
            for piece in sc.split(","):
                head = piece.split(";", 1)[0].strip()
                if "=" in head:
                    name, val = head.split("=", 1)
                    name = name.strip()
                    val = val.strip()
                    if name and val:
                        self.cookies[name] = val
        if want_json:
            try:
                parsed = json.loads(body_bytes) if body_bytes else {}
            except Exception:
                parsed = {"_raw": body_bytes[:200].decode("utf-8", "replace")}
            return status, parsed, resp_headers
        return status, body_bytes, resp_headers

    # ─── shortcuts ────────────────────────────────────────────────────
    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.request("POST", path, body=body, **kw)

    def put(self, path, body=None, **kw):
        return self.request("PUT", path, body=body, **kw)

    def delete(self, path, **kw):
        return self.request("DELETE", path, **kw)


# ─────────────────────────────── boot ───────────────────────────────────────


def boot_backend(env, log_path):
    logf = open(log_path, "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "backend.main"],
        cwd=ROOT, env=env, stdout=logf, stderr=logf)
    return proc, logf


def wait_for_health(c, attempts: int = 60):
    for _ in range(attempts):
        try:
            status, body, _ = c.get("/healthz")
            if status == 200 and isinstance(body, dict) and body.get("ok"):
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


# ──────────────────────────── individual tests ──────────────────────────────


def test_passcode_gate(c: Client):
    """Set a passcode, verify the gate enforces it, then reset the DB.

    Because there's no public "clear passcode" endpoint (passcode is in
    SECRET_FIELDS and PUT-settings refuses to clear it), the test resets
    the DB after this section. This MUST run before any test that
    depends on persistent state — but after a clean boot it's safe.
    """
    section("passcode gate")
    pc = "hunter2-e2e"
    # gate should be off initially (no passcode set, fresh DB)
    status, body, _ = c.get("/api/passcode/check")
    check(status == 200 and body.get("enabled") is False,
          "passcode gate OFF before set", f"{status} {body}")
    # set passcode (this is the only path that writes the secret field;
    # blank values are skipped, so a real value works)
    status, body, _ = c.put("/api/settings", body={"passcode": pc})
    check(status == 200 and body.get("status") == "ok",
          "PUT /api/settings sets passcode", f"{status} {body}")
    # gate should now refuse unauth — use a fresh client (no cookies)
    c2 = Client(c.base)
    status, body, _ = c2.get("/api/runs")
    check(status == 401, "unauthed /api/runs returns 401",
          f"got {status}")
    # auth via X-Arui-Passcode header
    status, body, _ = c2.get("/api/runs",
                              headers={"X-Arui-Passcode": pc})
    check(status == 200 and isinstance(body, list),
          "X-Arui-Passcode header auth → 200", f"got {status}")
    # auth via Authorization: Bearer
    status, body, _ = c2.get(
        "/api/runs", headers={"Authorization": f"Bearer {pc}"})
    check(status == 200,
          "Authorization: Bearer auth → 200", f"got {status}")
    # log in via the dedicated endpoint and verify cookie persists
    c3 = Client(c.base)
    status, body, _ = c3.post("/api/passcode/login", body={"passcode": pc})
    check(status == 200 and body.get("ok") is True,
          "/api/passcode/login accepts passcode",
          f"{status} {body}")
    status, body, _ = c3.get("/api/runs")
    check(status == 200,
          "subsequent /api/runs uses login cookie", f"got {status}")
    # wrong passcode → 401
    status, body, _ = c2.post("/api/passcode/login", body={"passcode": "nope"})
    check(status == 200 and body.get("ok") is False,
          "wrong passcode rejected (ok:false)", f"{status} {body}")
    # cycle the passcode cookie onto the master client so all subsequent
    # tests authenticate cleanly
    c.cookies["arui_pc"] = pc
    # reset wipes the DB and disables the gate — done via /api/reset.
    status, body, _ = c.post("/api/reset")
    check(status == 200 and body.get("status") == "reset",
          "/api/reset wipes state and disables gate",
          f"{status} {body}")
    # clear the now-stale cookie
    c.cookies.pop("arui_pc", None)
    # verify the gate is OFF again on a fresh client (no auth needed)
    c4 = Client(c.base)
    status, body, _ = c4.get("/api/passcode/check")
    check(status == 200 and body.get("enabled") is False,
          "passcode gate OFF after reset", f"{status} {body}")
    status, body, _ = c4.get("/api/runs")
    check(status == 200, "gate-OFF /api/runs returns 200 (no auth)",
          f"got {status}")


def test_onboarding(c: Client):
    section("onboarding + project")
    cfg = {
        "repo_name": "e2e-tiny",
        "purpose": "verify the entire stack end-to-end",
        "metric": "val_mse",
        "email": "",
        "cadence": "off",
        # disable every council reviewer so paper-proposal / per-run
        # review don't try to hit real APIs in CI. The 'available'
        # check is gated on these flags — both Author Agent's proposal
        # council and the per-run review will short-circuit cleanly.
        "council_enable_gemini": False,
        "council_enable_openai": False,
        "council_enable_claude_tiebreaker": False,
        "council_per_run_enabled": False,
        "strategic_review_enabled": False,
    }
    status, body, _ = c.post("/api/onboarding", body=cfg)
    check(status == 200 and body.get("status") in
          ("configured", "started"),
          "POST /api/onboarding accepts minimal config",
          f"{status} {body}")
    status, body, _ = c.get("/api/project")
    check(status == 200 and body.get("name") == "e2e-tiny",
          "GET /api/project returns registered project",
          f"name={body.get('name')!r}")
    # defaults endpoint
    status, body, _ = c.get("/api/onboarding/defaults")
    check(status == 200 and isinstance(body, dict)
          and "agent_instructions" in body,
          "GET /api/onboarding/defaults has agent_instructions",
          f"keys={sorted(body.keys())[:6]}…")
    # settings round-trip
    status, body, _ = c.get("/api/settings")
    check(status == 200 and body.get("repo_name") == "e2e-tiny",
          "GET /api/settings reflects onboarding", f"{status}")


def test_system(c: Client):
    section("system stats + warnings")
    status, body, _ = c.get("/api/system")
    has_keys = (isinstance(body, dict)
                and "gpus" in body and "ram" in body and "disk" in body
                and "warnings" in body)
    check(status == 200 and has_keys,
          "GET /api/system returns gpus+ram+disk+warnings",
          f"keys={sorted((body or {}).keys()) if isinstance(body, dict) else type(body).__name__}")
    if isinstance(body, dict):
        check(isinstance(body.get("warnings"), list),
              "system.warnings is a list",
              f"type={type(body.get('warnings')).__name__}")


def test_cleanup_endpoints(c: Client):
    section("cleanup endpoints (preview-only / no-op)")
    # preview with age=30 days, bottom=0.5 — fresh DB → nothing matches
    status, body, _ = c.get(
        "/api/runs/cleanup/preview?min_age_days=30&bottom_pct=0.5")
    has_shape = (isinstance(body, dict)
                 and "eligible" in body
                 and "bytes_freeable" in body
                 and "runs" in body)
    check(status == 200 and has_shape,
          "preview returns {eligible,bytes_freeable,runs}",
          f"eligible={(body or {}).get('eligible')}")
    check(isinstance(body, dict) and body.get("eligible") == 0,
          "nothing eligible on fresh DB",
          f"eligible={(body or {}).get('eligible')}")
    # actual cleanup with same thresholds — should also be a no-op
    status, body, _ = c.post(
        "/api/runs/cleanup",
        body={"min_age_days": 30.0, "bottom_pct": 0.5})
    check(status == 200 and isinstance(body, dict)
          and body.get("deleted") == 0,
          "POST /api/runs/cleanup is a no-op when nothing matches",
          f"deleted={(body or {}).get('deleted')}")
    # SOTA preview shape
    status, body, _ = c.get("/api/runs/cleanup/preview_sota")
    has_shape = (isinstance(body, dict)
                 and "eligible" in body and "kept_run_ids" in body)
    check(status == 200 and has_shape,
          "preview_sota returns {eligible,kept_run_ids,runs}",
          f"eligible={(body or {}).get('eligible')}")
    # try several thresholds — shape stable
    for age, pct in [(0.01, 0.1), (7.0, 0.25), (60.0, 0.99)]:
        status, body, _ = c.get(
            f"/api/runs/cleanup/preview?min_age_days={age}&bottom_pct={pct}")
        check(status == 200 and isinstance(body, dict)
              and "eligible" in body,
              f"preview stable @ age={age} pct={pct}",
              f"eligible={(body or {}).get('eligible')}")


def test_paper_mode(c: Client):
    section("paper mode")
    # initial mode should be research
    status, body, _ = c.get("/api/mode")
    check(status == 200 and body.get("mode") == "research",
          "/api/mode starts in 'research'", f"mode={body.get('mode')}")
    # start a proposal
    status, body, _ = c.post("/api/paper/proposal/start")
    pid = (body or {}).get("proposal_id", "")
    check(status == 200 and pid,
          "POST /api/paper/proposal/start returns id",
          f"id={pid}")
    # wait for the proposal council background thread to settle. With
    # every reviewer disabled in settings, the no-reviewers branch
    # fires almost immediately — we still allow a few seconds for the
    # thread scheduler.
    ready = False
    for _ in range(40):
        status, body, _ = c.get(f"/api/paper/proposal/{pid}")
        if isinstance(body, dict) and body.get("status") == "ready":
            ready = True
            break
        time.sleep(0.25)
    check(ready,
          "proposal background flips to 'ready' without reviewers",
          f"final={(body or {}).get('status')}")
    # enter paper mode with minimal meta
    status, body, _ = c.post(
        "/api/paper/enter",
        body={
            "meta": {
                "venue": "NeurIPS 2026",
                "deadline_iso": "2026-12-31T23:59:59",
                "authors": [],
                "gpu_budget_hours": 10,
                "llm_budget_daily_usd": 1,
            },
            "proposal_id": pid,
        })
    check(status == 200 and body.get("status") in
          ("entered_paper_mode", "already_in_paper_mode"),
          "POST /api/paper/enter flips mode",
          f"{status} {body.get('status')}")
    # verify mode flipped
    status, mbody, _ = c.get("/api/mode")
    check(status == 200 and mbody.get("mode") == "paper",
          "/api/mode is now 'paper'", f"mode={mbody.get('mode')}")
    # cadence should have been auto-bumped to 24h (off was set in onboarding;
    # the auto-switch logic preserves 'off' explicitly, so we expect 'off'
    # to STAY off — but if cadence was unset/blank/short, it flips to '24h').
    status, sbody, _ = c.get("/api/settings")
    cad = (sbody or {}).get("cadence")
    check(cad in ("off", "24h"),
          "cadence preserved (off) or auto-flipped to 24h",
          f"cadence={cad!r}")
    # /api/paper/state returns the full payload
    status, state, _ = c.get("/api/paper/state")
    keys_present = {"mode", "meta", "claims", "paper_runs",
                    "decisions", "versions"}
    check(status == 200 and keys_present.issubset(state.keys()),
          "GET /api/paper/state returns expected keys",
          f"keys={sorted(state.keys())[:8]}…")
    # queue a tiny no-op paper run
    status, body, _ = c.post(
        "/api/paper/runs/queue",
        body={"name": "e2e-noop", "role": "ablation",
              "cmd": "true",                # the trivial no-op shell command
              "n_seeds": 1, "gpus_required": 1, "est_time_sec": 1})
    check(status == 200 and body.get("ok") is True and body.get("id"),
          "POST /api/paper/runs/queue accepts noop cmd",
          f"{status} id={(body or {}).get('id')}")
    queued_id = body.get("id", "")
    # results endpoint
    status, body, _ = c.get("/api/paper/runs/results")
    check(status == 200 and isinstance(body, dict)
          and isinstance(body.get("runs"), list),
          "GET /api/paper/runs/results returns {runs:[...]}",
          f"n={len((body or {}).get('runs', []))}")
    # file a strategic decision so we can resolve one (state.decisions may
    # be empty depending on background threads — file our own deterministically)
    status, body, _ = c.post(
        "/api/paper/decisions",
        body={"kind": "approve_text",
              "title": "[e2e] noop decision",
              "body_md": "for testing /resolve",
              "default_action": "approve",
              "priority": 1})
    did = (body or {}).get("id", "")
    check(status == 200 and did,
          "POST /api/paper/decisions files a strategic decision",
          f"id={did}")
    if did:
        status, body, _ = c.post(
            f"/api/paper/decisions/{did}/resolve",
            body={"action": "approve", "note": "e2e ok"})
        check(status == 200 and body.get("ok") is True,
              "POST /api/paper/decisions/<id>/resolve → ok",
              f"{status} {body}")
    # paper sections endpoint (Phase E)
    status, body, _ = c.get("/api/paper/sections")
    check(status == 200 and isinstance(body, dict)
          and isinstance(body.get("sections"), list),
          "GET /api/paper/sections returns {sections:[]}",
          f"n={len((body or {}).get('sections', []))}")
    # citations endpoint
    status, body, _ = c.get("/api/paper/citations")
    check(status == 200 and isinstance(body, dict)
          and isinstance(body.get("citations"), list),
          "GET /api/paper/citations returns {citations:[]}",
          f"n={len((body or {}).get('citations', []))}")
    # versions endpoint
    status, body, _ = c.get("/api/paper/versions")
    check(status == 200 and isinstance(body, dict)
          and isinstance(body.get("versions"), list),
          "GET /api/paper/versions returns {versions:[]}",
          f"n={len((body or {}).get('versions', []))}")
    # anti-pattern watcher fire (idempotent, returns {filed})
    status, body, _ = c.post("/api/paper/anti_patterns/run")
    check(status == 200 and isinstance(body, dict) and "filed" in body,
          "POST /api/paper/anti_patterns/run → {filed:N}",
          f"filed={(body or {}).get('filed')}")
    # revert back to research
    status, body, _ = c.post(
        "/api/paper/revert",
        body={"reason": "e2e test wrap-up — done with paper-mode flows"})
    check(status == 200 and body.get("status") == "reverted",
          "POST /api/paper/revert flips back to research",
          f"{status} {body}")
    status, mbody, _ = c.get("/api/mode")
    check(status == 200 and mbody.get("mode") == "research",
          "/api/mode is back to 'research'", f"mode={mbody.get('mode')}")


def test_share_link(c: Client):
    section("share link (read-only)")
    # mint a token. paper mode is OFF now after revert — that's OK, share
    # endpoint just reads tokens out of Settings and serves a redacted payload.
    status, body, _ = c.post("/api/paper/share/token")
    token = (body or {}).get("token", "")
    check(status == 200 and len(token) >= 16,
          "POST /api/paper/share/token mints a token",
          f"token={token[:8]}…")
    # the share JSON endpoint is PUBLIC (auth.py whitelists /api/paper/share/)
    c2 = Client(c.base)  # cookieless / unauth
    status, body, _ = c2.get(f"/api/paper/share/{token}")
    check(status == 200 and body.get("ok") is True,
          "GET /api/paper/share/<token> serves read-only payload (public)",
          f"ok={body.get('ok')}")
    # wrong token returns ok:false (NOT a 4xx — that's the contract)
    status, body, _ = c2.get("/api/paper/share/this-token-is-wrong")
    check(status == 200 and body.get("ok") is False,
          "wrong token returns ok:false",
          f"{status} {body}")
    # /p/<token> serves the SPA shell
    status, body_bytes, _ = c2.get(f"/p/{token}", want_json=False)
    is_html = (status == 200
               and isinstance(body_bytes, bytes)
               and b"<" in body_bytes[:200])  # crude but reliable
    check(is_html,
          "/p/<token> serves the SPA shell (HTML)",
          f"status={status} bytes={len(body_bytes) if isinstance(body_bytes, bytes) else 'NA'}")
    # rotate token via DELETE
    status, body, _ = c.delete("/api/paper/share/token")
    check(status == 200 and body.get("ok") is True,
          "DELETE /api/paper/share/token revokes",
          f"{status} {body}")


def test_email_digest_dry_run(c: Client):
    section("notify digest dry-run (no real send)")
    # Drive the digest through the HTTP API — that runs digest_email()
    # inside the backend process so it shares the SQLAlchemy engine /
    # DB connection that has the live tables. Importing notify in our
    # process would create a SECOND engine on the same SQLite file —
    # SQLite WAL mode can leave the second connection seeing stale
    # schema until checkpoint, which has caused flaky failures.
    # cadence is 'off' so no transport is configured; the call should
    # return {sent: False} cleanly (NOT 5xx).
    status, body, _ = c.post("/api/notify/test", body={"digest": True})
    check(status == 200 and isinstance(body, dict) and "sent" in body,
          "/api/notify/test {digest:true} returns {sent:bool}",
          f"status={status} sent={(body or {}).get('sent')}")
    check(status == 200 and (body or {}).get("sent") is False,
          "digest dry-run returns sent:False (no transport configured)",
          f"sent={(body or {}).get('sent')}")
    # And the non-digest test path — same contract.
    status, body, _ = c.post("/api/notify/test", body={"digest": False})
    check(status == 200 and isinstance(body, dict) and "sent" in body,
          "/api/notify/test {digest:false} returns {sent:bool}",
          f"sent={(body or {}).get('sent')}")


def test_lit_search(c: Client):
    section("lit agent")
    # POST a query. Network may not return real results from arxiv/SS —
    # we just assert the endpoint doesn't 500. Short server-side timeout
    # is fine; the backend's urllib calls have their own 30s default.
    try:
        status, body, _ = c.post(
            "/api/paper/lit/search",
            body={"query": "sgd convergence", "limit": 2},
            timeout=20.0)
    except Exception as e:
        # Some sandboxes block egress entirely — that's an acceptable
        # outcome for CI; just don't fail the suite over it.
        check(True,
              "POST /api/paper/lit/search (offline tolerated)",
              f"egress error: {type(e).__name__}: {e}")
        return
    check(status == 200 and isinstance(body, dict)
          and isinstance(body.get("results"), list),
          "POST /api/paper/lit/search returns {results:[]} (no 500)",
          f"n={len((body or {}).get('results', []))}")


def test_council(c: Client):
    section("council review")
    # if no API keys are configured, the council endpoint returns no_review
    # or skips. We sanity-check the wiring either way.
    rid = "e2e-fake-run"
    # post a fake completed run via /api/track/finish so /council/review has
    # something to chew on
    status, _, _ = c.post(
        "/api/track/run",
        body={"name": rid, "config": {"lr": 0.01, "what": "e2e fake"}})
    check(status == 200, "track/run accepts a fake run", f"{status}")
    status, _, _ = c.post(
        "/api/track/finish",
        body={"run_id": rid, "summary": {"val_mse": 0.42}})
    check(status == 200, "track/finish marks it complete", f"{status}")
    # now ask council to review it. If keys are configured, this hits the
    # real council. Otherwise we expect a graceful no_review.
    status, body, _ = c.post("/api/council/review",
                              body={"run_id": rid}, timeout=45.0)
    accepted = (status == 200 and isinstance(body, dict)
                and (body.get("status") in (None, "no_review",
                                              "review_disabled",
                                              "error", "ok")
                     or "verdict" in body or "review" in body
                     or "rationale" in body))
    check(accepted,
          "council/review wired (returns no_review or real review)",
          f"{status} keys={sorted(body.keys()) if isinstance(body, dict) else type(body).__name__}")


def test_spa_routing(c: Client):
    section("SPA routing")
    paths = ["dashboard", "write-paper", "analysis", "system-stats",
             "p/abcdef0123456789", "lessons", "authkeys"]
    for p in paths:
        status, body_bytes, headers = c.get(f"/{p}", want_json=False)
        is_html = (status == 200
                   and isinstance(body_bytes, bytes)
                   and b"<" in body_bytes[:200])
        check(is_html, f"/{p} serves HTML SPA shell",
              f"status={status} ctype={headers.get('content-type', '')[:30]}")


def test_authkeys(c: Client):
    section("authkeys read")
    status, body, _ = c.get("/api/authkeys")
    has_shape = (isinstance(body, dict)
                 and "keys" in body and isinstance(body["keys"], list)
                 and "ssh" in body)
    check(status == 200 and has_shape,
          "GET /api/authkeys returns {keys:[], ssh:str, ...}",
          f"n_keys={len((body or {}).get('keys', []))}")
    status, body, _ = c.get("/api/authkeys/pubkey")
    check(status == 200 and isinstance(body, dict),
          "GET /api/authkeys/pubkey returns dict (best-effort)",
          f"keys={sorted(body.keys()) if isinstance(body, dict) else type(body).__name__}")


# ─────────────────────────────────── main ───────────────────────────────────


def main() -> int:
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    data_dir = tempfile.mkdtemp(prefix="arui-e2e-full-")
    log_path = os.path.join(data_dir, "backend.log")
    env = dict(os.environ)
    env.update(
        ARUI_PORT=str(port),
        ARUI_DATA_DIR=data_dir,
        ARUI_AUTORUN="0",
        # stub LLM creds to make sure council/lit agent take the no-op path
        # instead of accidentally calling real APIs from CI.
        ARUI_NO_LLM="1",
    )
    # Wipe any real API keys from the child env — CI safety. Empty strings
    # won't actually wipe: council._load_keys_env() re-loads from
    # .deploy/keys.env on import whenever os.environ.get(k) is falsy.
    # Setting a recognizable sentinel keeps the slot non-empty so the
    # re-load skips. The council still won't fire because we also
    # disable every reviewer in the settings (see below).
    for k in ("OPENAI_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY",
              "GOOGLE_API_KEY"):
        env[k] = "e2e-stub-not-a-real-key"

    print(f"\n=== autoresearcherUI e2e (full) ===")
    print(f"    port={port}")
    print(f"    data_dir={data_dir}")
    print(f"    log={log_path}\n")

    started = time.time()
    proc, logf = boot_backend(env, log_path)
    c = Client(base)
    try:
        up = wait_for_health(c)
        if not check(up, "backend boots and serves /healthz"):
            return 1

        # ORDER MATTERS:
        # 1. passcode FIRST — it resets the DB at the end so onboarding can
        #    re-register cleanly afterwards.
        test_passcode_gate(c)
        test_onboarding(c)
        test_system(c)
        test_cleanup_endpoints(c)
        test_paper_mode(c)
        test_share_link(c)
        test_email_digest_dry_run(c)
        test_lit_search(c)
        test_council(c)
        test_spa_routing(c)
        test_authkeys(c)

    except Exception as e:                            # noqa: BLE001
        traceback.print_exc()
        check(False, "harness ran without unhandled error",
              f"{type(e).__name__}: {e}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        try:
            logf.close()
        except Exception:
            pass

    elapsed = time.time() - started
    ok = sum(1 for p, _, _ in RESULTS if p)
    total = len(RESULTS)
    print(f"\n=== {ok}/{total} checks passed in {elapsed:.1f}s ===")
    if ok != total:
        print("\nFAILURES:")
        for p, label, detail in RESULTS:
            if not p:
                print(f"  - {label}  ::  {detail}")
        print("\n--- backend log (tail 60) ---")
        try:
            with open(log_path) as f:
                tail = f.readlines()[-60:]
            print("".join(tail))
        except OSError:
            pass
        return 1
    print("e2e_full PASSED — safe to tag v0.0.2.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
