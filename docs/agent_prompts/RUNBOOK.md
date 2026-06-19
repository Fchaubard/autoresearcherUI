# autoresearcherUI — Node Runbook

Exact commands to deploy and exercise autoresearcherUI on a GPU node (e.g. the
RunPod 10×A40 box). Everything here has been verified in a Linux environment;
the e2e integration test passes 17/17.

> **Why you run these, not me:** the assistant that built this cannot SSH into
> your pod (no access to your private key, no outbound SSH). These commands are
> copy-paste ready.

---

## 1. Get the repo onto the node

The simplest path — push to GitHub from your Mac, then clone on the pod:

```bash
# on your Mac, in the repo
cd /path/to/autoresearcherui
git remote add origin https://github.com/Fchaubard/autoresearcherui.git
git push -u origin main
```

```bash
# on the pod
ssh root@<your-node-ip> -p <ssh-port> -i ~/.ssh/id_ed25519
git clone https://github.com/Fchaubard/autoresearcherui.git
cd autoresearcherui
```

(Or, since the direct-TCP connection supports SCP:
`scp -P <ssh-port> -i ~/.ssh/id_ed25519 -r /path/to/autoresearcherui root@<your-node-ip>:~/autoresearcherui`)

---

## 2. Run the e2e integration test (the merge gate)

This proves the whole system works — backend, orchestrator, the autonomous
loop, the arui→DuckDB metric pipeline — hardware-free, in ~20 seconds:

```bash
bash tests/run_e2e.sh
```

Expect: `17/17 checks passed` and `e2e integration test PASSED — safe to merge.`
This is the gate — it must exit 0 before any merge to `main`. CI runs it
automatically (`.github/workflows/ci.yml`).

---

## 3. See the dashboard (demo mode)

```bash
./dev.sh
```

Open the dashboard. On the pod it is reachable via the RunPod-exposed port, or
join it to your tailnet first:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up --authkey=<your-tailscale-auth-key>
# dashboard is then at http://<pod>.<tailnet>.ts.net:8000
```

Demo mode boots a populated, live-updating dashboard (the `bs1learning` sample
project) so you can see the UI immediately.

---

## 4. Run the example research project live (real orchestrator, no GPU)

This runs the **real autonomous loop** — the orchestrator picking ideas by EV,
launching real `train.py` subprocesses, ingesting metrics — driving the
dashboard live. It uses the `FakeAgent` and a tiny CPU task, so it needs no GPU
and no Claude token:

```bash
ARUI_DEMO=0 ./dev.sh           # terminal 1 — clean backend
curl -X POST localhost:8000/api/dev/run-example   # terminal 2 — start the loop
```

Watch the Experiments table and Live Graphs fill in as runs complete.

---

## 5. A real GPU research run — what's built, what's next

**Built and tested:** the backend, the dashboard, the `arui` tracker, the
orchestrator + research loop, the GPU-slot scheduler, the e2e gate, and the
`FakeAgent` path (deterministic, hardware-free).

**The remaining milestone (M3 real-mode):** `backend/app/agent.py :: RealAgent`
is a documented stub. To run a *real* research project on the 10×A40s it needs:
its `bootstrap()` to launch `claude --dangerously-skip-permissions` in a tmux
session and feed it `setup_prompt.md.j2` (creating the GitHub repo and
writing `program.md`/`train.py`/`ideas.md`), and `implement()` to have the agent
edit `train.py` per idea. The orchestrator, scheduler, tracking, and UI it plugs
into are all done and tested — `RealAgent` is the one seam left.

Once `RealAgent` is implemented, the same `Orchestrator` loop drives real GPU
training jobs; swap `n_slots` to your GPU count and the scheduler keeps all
10 A40s fed.

---

## Tokens

The tokens shared in chat (GitHub PAT, Anthropic key, Tailscale keys) are **not**
stored anywhere in this repo. Supply them at runtime via the onboarding form or
environment. Rotate them — they were pasted into a chat log.
