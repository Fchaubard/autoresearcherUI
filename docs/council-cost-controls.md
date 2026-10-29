# Council cost controls

These controls preserve selected models and reasoning settings. They avoid
duplicate work; they do not substitute models or disable selected reviewers.

Run, strategic and completion prompts omit the application-owned top-level
`review`, `reviews`, `strategic_review` and `strategic_review_id` fields from
experiment configuration. Stored evidence and experimental subkeys remain
intact. Existing concise lessons and strategic summaries remain available.

Code-review results are cached for 24 hours, with at most 128 entries. The key
includes the workspace, full eligible source contents, review prompt, seed-task
metadata and exact reviewer/model/effort selection. Local Flint configuration
is included because its alias can point to another checkpoint or endpoint.
Prompt collection and hashing share one eligibility function that prunes data,
checkpoint and cache directories before descending. Named root-level generated
progress records such as `AGENT_STATE.md`, `ARUI_HANDOFF.md` and `lessons.md` are
excluded from both; program/specification/configuration files remain included.
Changes beyond the prompt's display limit also invalidate the key. Pre-flight
and seed-task gates still run before lookup; failed/incomplete reviews are never
cached. Reused results expose `cached`, `reviewed_at`, `reused_at` and
`review_key`. Concurrent identical submissions share the running review;
different submissions receive an explicit busy response. Source, reviewer or
pre-flight changes during a review prevent its old approval from being applied.

Crash reuse requires a concrete exception from the captured log and a known
source SHA or git commit, matching experiment configuration, selected reviewers,
efforts, debate policy and prompts. It only applies to `crashed` or `failed`
runs. Unknown source versions, generic exits and changed failures receive fresh
reviews. The first crash review receives at most 6,000 characters of the final
traceback, with configured credentials redacted and unrelated console output
removed. A matching diagnosis is attached with `reused_from_run_id` and a
`council_review_reused` event; old vetoes, queue changes and lessons are not
reapplied. Existing per-run review eligibility is preserved exactly; the cost
controls do not create additional billed reviews. Strategic and completion
decisions are not cached.

Separate policy follow-up: the pre-existing per-run eligibility list omits
`kept_novel` and `kept_replicate`. Adding them would increase model calls and
requires a deliberate review-policy choice; it is not part of this cost fix.

Completion reviews allow one live worker. Identical pending claims with the same
selected reviewer policy share that worker; different claims receive an explicit
busy response. A 30-second lease heartbeat and process-local thread check keep a
slow local reviewer from being retried by the six-minute orphan watchdog. A
backend restart still allows recovery of an orphaned request. Review IDs prevent
a late verdict from overriding an operator's rejection or entering paper mode
after that rejection. Once the current review finishes, a new submission is
reviewed again; this guard does not reuse completed scientific decisions.

The private `advisory_usage.jsonl` file records each HTTP attempt through the
council adapters, including PI and scoping. It includes requested/response model,
actual request effort, role, operation, trigger ID where available, retry attempt,
latency, HTTP outcome, request ID and provider-reported token counts.
Requested/returned service tiers and cache-write lifetime counts are retained
when supplied, so later pricing can distinguish 30-minute and 12-hour writes.
Prompts, answers, credentials and raw provider errors are excluded. Unknown usage stays
null; reasoning is part of output, never an additional total. Google reports
visible candidate and thought counts separately; Anthropic reports cache reads
and writes separately from uncached input. [Gemini usage reference](https://ai.google.dev/api/generate-content#UsageMetadata)
and [Anthropic usage fields](https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/types/usage.py)
describe these fields.

The ledger records transport outcomes, not final scientific-review approval.
It is not an invoice, enforced spending cap or historical reconstruction.
Coding CLI sessions and OpenCode advisory subprocesses retain their separate
CLI evidence; their usage is not included in this HTTP ledger. Set
`ARUI_VERIFICATION=1` for isolated probes so recorded synthetic traffic is
explicitly labeled. The advisor probe does this automatically.

Tests cover prompt filtering, source/model/effort/prompt invalidation, rejected
and incomplete verdicts, concurrent requests, changed-source protection, crash
provenance, unchanged review eligibility, token normalization and per-attempt
accounting without sensitive content. Savings in production require a later
measured comparison; the earlier payload-size reduction is not a whole-session
cost-saving estimate.
