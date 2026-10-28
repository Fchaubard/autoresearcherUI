# Research controls, model providers, and mobile repair

This is the October 27 validation record. Subsequent model selection fixes
and real inference coverage are recorded in the
[October 28 role matrix](model-role-matrix-2026-10-28.md).

This change addresses the October 27 email storm, ineffective research halt,
live settings failures, blocked iPhone Send button, and post-submit mobile
research review. The subsequent button audit also repaired file creation/save
races, Analysis layout persistence and filters, paper claim/citation controls,
version history/diffs, submission assets and frozen PDFs, paper-mode return,
clipboard/share errors, and SSH key validation.

## Causes and behavior

- Email delivery could succeed before SQLite recorded the tunnel URL. Disk I/O
  failures then caused another email every minute. Delivery now requires a
  durable reservation, rate limits rotations, and pauses on storage failure.
  Production metadata was recovered to local disk with consistent backups;
  the notification tests cover failed commits and duplicate suppression.
- Halt only queried tracked Run rows. Actual training and queue supervisors
  launched with local tracking were absent from that table. Project-scoped
  process discovery now stops queues before workers and verifies termination.
  Durable pause/halt gates survive SQLite failure. Failed stops remain visible
  with Retry, and dismissing the report never resumes research.
- Settings stored values without reliably refreshing provider environment or
  live sessions. Form and watchdog edits now commit together, credentials
  refresh, and affected agent sessions restart against the original workspace.
  Changes wait while paused or while training runs inside the affected session;
  the watcher retries after that training completes. HTTP failures preserve
  drafts and produce readable messages.
- The phone conversation launcher overlaid its Send button. A dedicated mobile
  dock reserves space outside content and disappears when the conversation
  opens. Scoping has separate plan/review/discussion panels; pages, tables,
  dialogs, and action bars adapt to phone widths and viewport changes.

## Validation

- Full Python unit/integration suite with `ARUI_TEST_OPENCODE=1`: **1,100 passed**,
  zero skips, zero failures. One subsequent missing-CLI settings regression
  passed independently: **1,101 distinct backend tests**.
- Full WebKit + Chromium browser suite: **132 passed**. Eight subsequent targeted
  pin/diff, telemetry, and navigation checks passed, including four new cases:
  **136 distinct browser tests**.
- `bash tests/run_e2e.sh`: **34/34 checks**, across the CPU research orchestrator,
  mock autonomous agent using real tmux/API/tracking, and supervisor recovery.
- Real temporary worker/queue processes verified halt ordering, detached jobs,
  agent-owned training, permission failures, mixed project panes, interactive
  shell protection, and checkpoint preservation during deferred model changes.
- Real temporary Git and SSH-key fixtures verified paper version history,
  submission assets/frozen PDFs, existing-file preservation, and the last valid
  authorized SSH key guard.
- JavaScript syntax, Python compilation, whitespace, and changed-file credential
  scans passed. Browser regression execution is included in GitHub Actions.

Tests use private data directories and tmux servers. The browser harness mocks
API side effects and blocks external requests; it does not send email or touch
production training. Production process discovery was checked read-only and
identified the untracked queues/workers that the original halt missed.

Live deployment checks passed for all seven routes in a 390px WebKit viewport,
with no horizontal page overflow or JavaScript errors and an unobstructed Send
button. An authenticated unchanged-settings save succeeded. Database integrity
passed; the same research agent and ten GPU processes survived the backend
restart. Email remained paused, with no deliveries or storage errors logged.
The dashboard's GPU card now reads hardware telemetry separately from tracked
run counts, so experiments using local tracking do not falsely show idle GPUs.
This does not reconstruct missing historical Run rows from local experiment files.

## Model verification limits

See [provider verification](model-provider-verification.md) for exact model IDs,
official documentation, live probe results, and local Flint setup. Both new
Gemini models passed real API and CLI inference. The nine OpenCode free model
IDs were confirmed in its real client, but eight inference calls returned 429
and Nemotron Ultra returned 400 unavailable. No paid fallback was used.

Flint's OpenAI-compatible protocol, streaming, reasoning replay, and actual
coding tool execution passed against a local synthetic server through the real
OpenCode CLI. Actual Flint weights were not launched: no local server endpoint
was supplied and production GPUs were occupied by research.

See the [mobile audit](mobile-ui-audit.md) for the page/dialog inventory and
screenshots. WebKit browser emulation covers phone sizes and viewport changes;
it does not certify every physical iPhone keyboard or browser-toolbar behavior.
