# 09 — Notifications & Security

## Part A — Notifications

The brief: *"send you emails to your email … will produce plots and files and
email them all to you while the research is being conducted"* and *"how often to
proactively email about alerts."*

### 9.1 Email channels

The `notify/` worker sends two kinds of email, both to the researcher's
onboarding email address:

1. **Digests** — a periodic summary on the chosen **cadence**
   (`off` / `immediate` / `1h` / `4h` / `12h` / `24h`).
2. **Alerts** — event-driven, sent as things happen, independent of the digest
   cadence (unless cadence is `off`).

### 9.2 Cadence semantics

| Cadence | Digest behavior | Alert behavior |
|---------|-----------------|----------------|
| `off` | No digests. | No alerts. (Total email silence — in-app only.) |
| `immediate` | No batched digest; every notable event emails as it occurs. | Every alert sent immediately. |
| `1h`/`4h`/`12h`/`24h` | One rolled-up digest per interval. | Critical alerts (crash, agent-down, idle GPU, breakthrough) still sent immediately; non-critical events wait for the digest. |

APScheduler drives the digest timer; alerts are pushed by the event system.

### 9.3 What a digest contains

A digest covers everything since the last one:

- **Headline** — current best metric vs. baseline, total experiments done,
  success rate, GPU utilization over the window.
- **Completed experiments** — a table: idea name, result vs. baseline,
  keep/discard, duration.
- **Plots** — the key metric charts rendered server-side to PNG and **attached**
  (the brief's "produce plots and files and email them"), plus inline thumbnails.
- **Files** — notable artifacts (e.g. a new best checkpoint) attached or linked
  if large.
- **Upcoming** — the top few EV-ranked ideas.
- **Anything wrong** — crashes, idle GPUs, agent issues in the window.
- A deep link back to the dashboard (the tailnet URL).

### 9.4 Alert triggers

| Alert | Severity | Trigger |
|-------|----------|---------|
| **Run crashed** | warning | A run exits non-zero / OOMs / times out. Includes the stack-trace tail. |
| **GPU idle** | warning | A GPU under the idle threshold past the grace period ([doc 05](./05-autoresearch-engine.md) §5.4). |
| **Agent down** | critical | The Principal Researcher's session died. |
| **Breakthrough** | info | A run beats the best metric by ≥ a configurable margin. Plots attached. |
| **Loop stalled** | warning | No run started for longer than a configured window despite free GPUs. |
| **Bootstrap failed** | critical | A Phase-B step failed and could not auto-retry. |
| **Disk pressure** | warning | `data/` free space below a threshold. |

Alerts are de-duplicated (the same idle GPU does not email every 5 minutes — one
alert, then a resolution notice).

### 9.5 Sending mechanism

Sending email requires a relay; the recipient address alone is not enough. Two
configured options ([doc 04](./04-onboarding-and-agent-bootstrap.md) §4.2):

- **SMTP** via `aiosmtplib` — host/port/user/password. Works with a Gmail
  account + app password, or any SMTP relay.
- **Resend HTTP API** — an API key, for those who prefer it.

The worker retries with backoff, logs every send to `email_log`, and surfaces
send failures as an in-app warning so a misconfigured relay is obvious. A
**"send test email"** button in onboarding and Settings verifies the config end
to end.

### 9.6 In-app parity

Every email also appears in the dashboard's alerts panel and timeline
([doc 07](./07-dashboard-ui.md) §7.12). Email and in-app never diverge — email is
the *push* channel, the dashboard is the *pull* channel, same events.

---

## Part B — Security

> **v0.2 note:** the author has explicitly **deprioritized security** — this
> runs open-source research on disposable, rented, single-user GPU nodes behind
> a private tailnet. Part B is therefore **informational, not a v1 work item**.
> Build the convenient default (tailnet-only, optional passcode, `.env` on
> disk) and do not spend implementation time hardening beyond it. The content
> below is retained as reference for anyone who later wants it.

This is a single-researcher, self-hosted tool. The threat model is modest, but
the system holds real secrets and runs an agent with broad permissions, so the
posture must be deliberate.

### 9.7 Access control

- **Network layer (primary).** The dashboard is exposed only on the
  researcher's **Tailscale tailnet**. Devices not on the tailnet cannot reach it
  at all. This is the real perimeter.
- **Passcode (secondary).** A passcode ([doc 04](./04-onboarding-and-agent-bootstrap.md) §4.5)
  gates the dashboard for anyone who *is* on the tailnet. Default = the repo
  name; **blank = no passcode** (URL-only access), exactly per the brief.
- **Sessions.** A correct passcode mints a signed (HS256) session cookie —
  `HttpOnly`, `Secure` when HTTPS is on, `SameSite=Lax`, with an expiry. The
  signing key is `ARUI_SESSION_SECRET` in `.env`. The passcode is stored as a
  **bcrypt hash**, never plaintext.
- **Funnel is opt-in.** Public exposure via Tailscale Funnel is **off by
  default**; turning it on shows an explicit warning, and a passcode becomes
  strongly recommended (the UI nudges this).
- **Rate limiting.** Passcode attempts are rate-limited to blunt brute force.

### 9.8 Secret handling

Secrets live in **`data/secrets/.env`**, mode `0600`, owned by the backend user.

- Secrets are **never** written to the SQLite DB, **never** sent to the frontend
  (the API returns masked placeholders like `ghp_••••••1a2b`), and **never**
  logged.
- The frontend only ever *sends* a secret (on entry/rotation), never *receives*
  one back.
- The four large research text fields are stored as readable files under
  `data/config/` — they are not secret.
- **Token rotation** via Settings rewrites `.env` and restarts only the affected
  component.
- The bulk-paste config block contains secrets in plaintext; the UI warns that
  it is sensitive and should be treated like a password file. It is the
  researcher's own clipboard on their own machine — an accepted trade for the
  bulk-setup ergonomics the brief demands.

### 9.9 The `--dangerously-skip-permissions` agent

The biggest security consideration. The Principal Researcher runs Claude Code,
optionally with `--dangerously-skip-permissions`, which lets it execute shell
commands without per-action confirmation. Mitigations
([doc 02](./02-architecture.md) §2.4, [doc 03](./03-installation-and-node-setup.md) §3.4):

- It runs as a **dedicated, unprivileged unix user `claude`** — not root, not
  the backend user, **no `sudo`**.
- It can read/write **only** the experiment repo workspace and its own home. It
  **cannot read `data/secrets/.env`**; the backend injects only the specific env
  vars a given step needs into that step's process environment.
- It cannot reach the backend's privileged internals — the backend drives the
  agent purely by attaching to its tmux session.
- The checkbox controls *only* whether the flag is passed; the user-isolation
  above is **always** applied, checkbox or not.
- This is a **rented, ephemeral GPU node** dedicated to one research project —
  the blast radius is intentionally a throwaway box, not a machine with other
  important data.
- The dashboard's activity timeline records agent actions, so behavior is
  auditable after the fact.

The honest framing: this product *embraces* an autonomous agent with real
permissions — that is the point. Security here is about **bounding the blast
radius** (unprivileged user, no secret access, disposable node), not about
preventing the agent from acting.

### 9.10 Network & transport

- **HTTPS** via Tailscale Serve (a tailnet-valid cert) is recommended and
  offered by `setup.sh`; with it, session cookies are `Secure`.
- The `arui` ingest endpoint binds to **`127.0.0.1`** — training jobs reach it
  locally; it is not exposed on the tailnet. It additionally requires the
  `ARUI_TRACKING_INGEST_TOKEN` bearer token.
- Terminal WebSockets are gated by the same session auth as the rest of the API;
  an unauthenticated socket is rejected before a PTY is attached.

### 9.11 Data & privacy

- All research data, metrics, and logs stay **on the node**. Nothing is sent to
  any autoresearcherUI-operated service — there is no such service. The only
  outbound traffic is to the model APIs the agent calls, GitHub, and the email
  relay.
- The experiment repo is created under the **researcher's own GitHub account**;
  they choose public or private.
- Tearing down the node (`setup.sh --uninstall --purge`) destroys all local
  data; the GitHub repo persists under the researcher's control.

### 9.12 Threat model summary

| Threat | Mitigation |
|--------|------------|
| Outsider reaches the dashboard | Tailnet-only by default; Funnel opt-in and warned. |
| Tailnet peer opens the dashboard | Passcode (default on, = repo name); researcher may set blank deliberately. |
| Secret theft from disk | `.env` `0600`; secrets never in DB, API responses, or logs. |
| Agent damages the host | Unprivileged `claude` user, no `sudo`, no secret-file access, disposable node. |
| Brute-forcing the passcode | Rate limiting + bcrypt hashing. |
| Training job exfiltrates metrics | Ingest endpoint is loopback-only + bearer-token gated. |
| Lost work on backend crash | tmux sessions and the SDK's write-ahead buffer survive restarts. |

The posture matches the product: a powerful autonomous tool on a disposable,
single-user, tailnet-private box — convenient by default, with the sharp edges
(`--dangerously-skip-permissions`, blank passcode, Funnel) clearly labelled so
the researcher chooses them knowingly.
