# Autoresearch API cost audit — 2026-10-29

The strongest measured savings opportunity is fewer large-context model calls,
especially mechanical monitoring. Lowering Astra's reasoning effort is not
required for the recommendations below. The operator independently changed
research and authoring from xhigh to medium at approximately 18:58 UTC during
this audit; the baseline below deliberately ends before that change.

## Measured research-agent baseline

Exact workspace: `continual-learning-experiments`. Window: 2026-10-28 22:51:27
through 2026-10-29 18:58:15 UTC, approximately 20.11 hours. This excludes the
repair assistant, isolated model-verification workspaces, advisory API calls,
GPU rental and all earlier research sessions.

| Recorded quantity | Value |
| --- | ---: |
| Astra xhigh responses, including compaction responses | 3,628 |
| Responses per hour | 180.4 |
| Input tokens | 532,649,001 |
| Cached input reads | 523,446,580 |
| Cache writes | 8,869,544 |
| Remaining uncached input | 332,877 |
| Output tokens, including reasoning | 3,705,399 |
| Reasoning output, a subset of output | 1,599,197 |
| Average request input | 146,816 tokens |
| Maximum request input | 249,039 tokens |
| Token cache hit rate | 98.27% |

The per-response records reconcile with the final cumulative
`thread_token_usage` record. Cumulative `token_count` events are not summed.
No duplicate response IDs were found. The model and effort follow each recorded
`turn_context`; this complete session contains only Astra xhigh.

| Estimated charge component | USD | Share |
| --- | ---: | ---: |
| Cached input reads | $523.45 | 63.6% |
| Cache writes, assumed default 30-minute lifetime | $110.87 | 13.5% |
| Other input | $3.33 | 0.4% |
| Output, including reasoning | $185.27 | 22.5% |
| **Total for this research session** | **$822.91** | **100%** |

This is approximately **$40.91/hour**, or **$981.93 per 24 hours if the same
activity continued**. The daily figure is a projection, not observed daily
billing. Reasoning output accounts for about $79.96 of the estimate (9.7% of
the total); that does not predict the quality or cost effect of changing effort.

These are estimates, not an invoice reconciliation. The calculation uses
published standard Astra rates of $10/M uncached input, $1/M cached input,
$12.50/M 30-minute cache writes, and $50/M output. No request in this baseline
crossed the 272K long-context threshold. The CLI logs expose total cache writes
but not their lifetime; the default 30-minute lifetime is assumed. Account
discounts, a different service tier or cache lifetime would change the result.
See [Astra pricing](https://developers.openai.com/api/docs/models/gpt-6-astra)
and [cache accounting](https://developers.openai.com/api/docs/guides/prompt-caching).

A 98% cache hit rate is already good. Replaying a 147K-token cached context
still costs roughly $0.147 per call before new tokens or output. Improving cache
hits alone therefore will not address the largest component.

## Where unnecessary work appears

### 1. Move mechanical monitoring outside the model

The baseline contains 381 explicit `clock.sleep` responses and 558 responses
invoking the named `core_compact_poll_command`: **939 responses, $196.60** of
estimated charges, or 23.9% of the session total. Some named polls share a call
with additional useful work; this is an opportunity bucket, not a promise that
every dollar is removable. It also excludes other status checks that were not
classified confidently.

Use a deterministic monitor to wait for run completion, failure, a new metric
milestone, blocked dispatch, or an operator message. Send Astra a compact event
delta when a decision is needed. Keep GPU dispatch and health checks running
independently so idle model time does not mean idle GPUs. Batch independent reads
in one tool round and avoid repeated full status-document rewrites.

At the audit baseline, the setup prompt asked for polling every ten seconds
(`backend/app/realrun.py`, initial research prompt). The observed agent often
slept 35–45 seconds, then made several status calls. A recommendation to the
model alone is less reliable than implementing an event-driven wait tool.

### 2. Keep a small working context without discarding research history

Retain experiments, plans, evidence and full transcripts on disk; feed the
model a concise current-state summary and retrieve specific evidence as needed.
The baseline resume prompt asked it to read the transcript and run history, and
supervisor instructions asked for broad rereads. At the audit snapshot, the
transcript was 116 MB, lessons 1.35 MB, directives 423 KB and ideas 220 KB.
These file sizes do not establish how much was sent to the model.

As a sensitivity calculation, halving **billed cached-input tokens** alone at
the same request count would reduce this baseline by $261.72 (31.8%). That is
not an achieved saving: summary generation, retrieval, changed cache writes
and decision quality must be measured. This overlaps with the polling savings;
the two percentages must not simply be added.

### 3. Avoid repeated debates about equivalent crashes

In the separate trailing-24-hour app snapshot, 84 per-run reviews were stored,
all for crashed runs. Their saved debate histories contain 174 Astra turns,
174 Gemini turns and one Fable tiebreaker. Ninety Astra turns were follow-up
debates beyond one initial review per run. Strategic review was also enabled,
producing 36 events, and there were 67 code-review outcomes.

Group repeated infrastructure failures by a stable failure signature; preserve
selected-model review for new failures and substantive research decisions.
Review a crash family once, and attach the diagnosis to subsequent matching
runs. Keep strategic batch reviews and code correctness gates. This can remove
repeated review calls without lowering their reasoning effort. The current
eligibility list also omits `kept_novel` and `kept_replicate`; review policy
should be corrected deliberately, not blindly disabled.

No defensible dollar amount is available for these historical calls: the app
discarded provider usage fields, and historical reviewer effort was not recorded.

### 4. Remove old reviews from new review inputs

Strategic review serializes full run configuration, including previous review
metadata. Among the 84 currently stored reviewed configurations, the mean JSON
size is 24,923 characters; omitting `review`, `reviews`, `strategic_review` and
`strategic_review_id` reduces that component to 2,059 characters, or 91.7% less.
This is a current payload-size measurement, not a historical token saving or a
91.7% reduction in the whole prompt.

Send experiment configuration and evidence once, with a short prior-review
summary when needed. Cache code approval by source hash, reviewer/model/effort
and prompt version; coalesce concurrent identical requests. Code review can
currently collect up to 320K characters for each of two reviewers. Identical
duplicate submissions were not proven in this audit, so their savings remain
unmeasured.

### 5. Add complete usage accounting before claiming total application spend

Record role, exact model and effort, trigger/run ID, attempt, latency, outcome,
input/cache-write/cache-read/output/reasoning tokens and provider request ID.
Record failures and retries too. Keep synthetic verification traffic separate.
The app's existing paper budget table has no rows and is not an enforced
inference spending limit; it cannot currently validate total API spend.

PI is lower priority: it ran approximately hourly (median 60.2 minutes), with
no close duplicate cycles in the inspected window. Seventeen recorded nudges
had no exact duplicate text. Routine dashboard, GPU, email and watchdog health
polling is deterministic and does not itself call a model.

## Call inventory and limits of attribution

| Path | Trigger / fan-out | Measured trailing 24h |
| --- | --- | ---: |
| Research coding agent | Ongoing CLI reasoning/tool loop | Separate exact token baseline above |
| Author coding agent | Active author session in paper workspace | No active author process at snapshot |
| Per-run council | Two initial reviewers, up to three debate rounds, optional tiebreak | 84 reviews; 349 saved turns |
| Strategic council | Selected reviewer after each configured batch (currently 10 finished runs) | 36 events |
| Code blessing | Two configured reviewers per request | 59 approved, 8 rejected outcomes |
| PI | One call per hourly cycle; optional nudges | 25 intervention events |
| Scoping | Initial synthesis, each chat message, explicit finalization | User-driven; no recurring loop |
| Paper readiness/review/import | Explicit actions or gated paper workflow | No paper activity records at snapshot |
| Watchdog rule generation | First setup or explicit regeneration | One all-time event |
| Health checks, notifications, GPU status | Deterministic checks | No LLM call |

Provider retry wrappers can make up to four attempts for certain transient
failures. Read timeouts are deliberately not retried. Actual historical retry
usage cannot be reconstructed from the app's current telemetry.

## Reproduction

Run the read-only, credential-free parser against the original session logs:

```bash
python scripts/audit_codex_usage.py \
  --cwd /workspace/zoo_cartridge/autoresearcherUI/continual-learning-experiments \
  --start 2026-10-28T22:50:00Z \
  --end 2026-10-29T18:58:16Z \
  --output /tmp/research-cost-audit.json
```

The parser emits counts and estimates, not prompts or tool output. It ignores
other workspaces, excludes cumulative usage events, deduplicates response IDs,
and charges reasoning only as part of output. Other models are counted but
remain unpriced. A sanitized result is in
[`verification/research-cost-audit-2026-10-29.json`](verification/research-cost-audit-2026-10-29.json).
Regression tests cover token partitioning, reasoning double-count prevention,
the long-context boundary, duplicate events and workspace scoping.

Recommendation order: implement event-driven monitoring, reduce routine context
replay, deduplicate crash reviews and review payloads, then evaluate lower-price
service tiers for latency-tolerant reviews. Keep exact selected models and
reasoning levels; never introduce silent substitution.

## Implemented controls

The research agent can now register a durable wait at `POST /api/agent/wait`
with a concise handoff and then finish its turn. The deterministic supervisor
checks experiment and code-review state, delivers a bounded change message to
the existing conversation, and clears the wait only after delivery succeeds.
Metric updates alone do not wake it. A default 15-minute safety reassessment
is bounded to 5–30 minutes. Generic idle nudges respect this intentional wait;
dead-process recovery remains active. An unchanged cropped terminal emits a
bounded warning instead of cancelling potentially healthy work. Worker processes
and GPU dispatch continue independently. Operator messages and substantive PI
interventions can wake the conversation immediately.

`GET /api/agent/context` returns a bounded current-state summary and the last
handoff. Setup, resume and supervisor prompts ask for a small `AGENT_STATE.md`
and targeted evidence retrieval. Complete research history stays on disk.
Existing conversations need the new policy delivered once after deployment;
new launches and native settings resumptions receive it automatically.

Council calls now omit recursive review metadata, reuse conservatively matched
crash diagnoses, cache identical code reviews and coalesce concurrent duplicate
requests. New or changed evidence still goes through the selected reviewers.
The new private usage ledger records individual HTTP attempts and available
token counts. See [council cost controls](council-cost-controls.md) for cache
invalidation, accounting coverage and limitations.

These changes do not refund past charges, impose a spending cap or guarantee a
particular percentage reduction. The $196.60 polling bucket and $261.72 context
sensitivity overlap. Post-deployment usage must be measured; the operator's
separate change to medium also prevents a direct controlled comparison with
the xhigh baseline.
