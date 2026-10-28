# Real model-role verification — 2026-10-28

All **269 eligible model-role pairs** were attempted: real inference passed in **241 pairs**; **28 did not pass**. The catalog contains 41 models and seven roles; nine API-only compatibility models do not offer coding-agent roles.

The separate free-text scoping path passed **32/41**. All **76 advertised API reasoning-effort combinations** passed real inference. These are bounded smoke tests using synthetic work, not evidence that every future research task will succeed.

**Free OpenCode qualification:** 35 successful role checks preceded the final configuration-isolation hardening. Free-tier quota exhaustion blocked live revalidation of that final configuration. The final isolation and routing constraints passed real-CLI tests against a synthetic server. No free model is certified here as passing every role on the final configuration. Nemotron Ultra returned model-unavailable errors; 21 other core checks hit quota. All nine free scoping-chat attempts failed; their earlier probe wrapper did not preserve the underlying status code, and this metadata limitation is retained in the evidence.

## Per-model results

`Pass` means actual inference with role-specific output or real coding tools. `Pass†` is a real free-model success before final isolation; live revalidation was quota-blocked. `N/A` means the role is intentionally unsupported. Other values identify a failed attempt; catalog visibility and protocol fixtures are never counted as inference passes.

| Model | Research | Author | Scoping | Reviewer 1 | Reviewer 2 | Tiebreaker | PI | Chat |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| gpt-6-astra | Pass | Pass | Pass | Pass | Pass | Pass | Pass | Pass |
| gpt-5.6-sol | Pass | Pass | Pass | Pass | Pass | Pass | Pass | Pass |
| gpt-5.6-terra | Pass | Pass | Pass | Pass | Pass | Pass | Pass | Pass |
| gpt-5.6-luna | Pass | Pass | Pass | Pass | Pass | Pass | Pass | Pass |
| gpt-5.5 | Pass | Pass | Pass | Pass | Pass | Pass | Pass | Pass |
| gpt-5.2 | Pass | Pass | Pass | Pass | Pass | Pass | Pass | Pass |
| gpt-5.1 | N/A | N/A | Pass | Pass | Pass | Pass | Pass | Pass |
| gpt-5 | N/A | N/A | Pass | Pass | Pass | Pass | Pass | Pass |
| gpt-5-mini | N/A | N/A | Pass | Pass | Pass | Pass | Pass | Pass |
| gpt-5-nano | N/A | N/A | Pass | Pass | Pass | Pass | Pass | Pass |
| o3 | N/A | N/A | Pass | Pass | Pass | Pass | Pass | Pass |
| o3-pro | N/A | N/A | Pass | Pass | Pass | Pass | Pass | Pass |
| gemini-4-argon | Pass | Pass | Pass | Pass | Pass | Pass | Pass | Pass |
| gemini-3.1-flash-lite | Pass | Pass | Pass | Pass | Pass | Pass | Pass | Pass |
| gemini-3.8-flash | Pass | Pass | Pass | Pass | Pass | Pass | Pass | Pass |
| gemini-3.7-flash | Pass | Pass | Pass | Pass | Pass | Pass | Pass | Pass |
| gemini-3.6-flash | Pass | Pass | Pass | Pass | Pass | Pass | Pass | Pass |
| gemini-3.5-flash | Pass | Pass | Pass | Pass | Pass | Pass | Pass | Pass |
| gemini-3.5-flash-lite | Pass | Pass | Pass | Pass | Pass | Pass | Pass | Pass |
| gemini-3.1-pro-preview | Pass | Pass | Pass | Pass | Pass | Pass | Pass | Pass |
| gemini-2.5-pro | Pass | Pass | Pass | Pass | Pass | Pass | Pass | Pass |
| gemini-2.5-flash | Pass | Pass | Pass | Pass | Pass | Pass | Pass | Pass |
| claude-fable-5-1 | Pass | Pass | Pass | Pass | Pass | Pass | Pass | Pass |
| claude-opus-5 | Pass | Pass | Pass | Pass | Pass | Pass | Pass | Pass |
| claude-sonnet-5 | Pass | Pass | Pass | Pass | Pass | Pass | Pass | Pass |
| claude-fable-5 | Pass | Pass | Pass | Pass | Pass | Pass | Pass | Pass |
| claude-opus-4-8 | Pass | Pass | Pass | Pass | Pass | Pass | Pass | Pass |
| claude-opus-4-7 | N/A | N/A | Pass | Pass | Pass | Pass | Pass | Pass |
| claude-opus-4-6 | N/A | N/A | Pass | Pass | Pass | Pass | Pass | Pass |
| claude-sonnet-4-6 | N/A | N/A | Pass | Pass | Pass | Pass | Pass | Pass |
| claude-haiku-4-5-20251001 | Pass | Pass | Pass | Pass | Pass | Pass | Pass | Pass |
| opencode/nemotron-3-ultra-free | model unavailable | model unavailable | model unavailable | model unavailable | model unavailable | model unavailable | model unavailable | failed |
| opencode/nemotron-3.5-lightning-free | Pass† | Pass† | Pass† | Pass† | Pass† | quota exhausted | Pass† | failed |
| opencode/nemotron-3.5-super-vl-free | Pass† | Pass† | Pass† | Pass† | Pass† | quota exhausted | Pass† | failed |
| opencode/big-pickle | Pass† | Pass† | Pass† | Pass† | Pass† | quota exhausted | Pass† | failed |
| opencode/glm-5.3-flash-free | Pass† | Pass† | Pass† | Pass† | Pass† | quota exhausted | Pass† | failed |
| opencode/deepseek-v4.1-flash-free | Pass† | Pass† | Pass† | Pass† | Pass† | quota exhausted | quota exhausted | failed |
| opencode/beam-free | Pass† | Pass† | quota exhausted | quota exhausted | quota exhausted | quota exhausted | quota exhausted | failed |
| opencode/flint-40b-free | Pass† | Pass† | quota exhausted | quota exhausted | quota exhausted | quota exhausted | quota exhausted | failed |
| opencode/mimo-v2.6-flash-free | Pass† | Pass† | quota exhausted | quota exhausted | quota exhausted | quota exhausted | quota exhausted | failed |
| flint/local | Pass | Pass | Pass | Pass | Pass | Pass | Pass | Pass |

## Evidence and method

- [Machine-readable summary](verification/model-role-summary.json).
- [Hosted advisor calls](verification/hosted-advisor-results.json) and [free-text calls](verification/hosted-scoping-chat-results.json): production adapters and prompts, raw JSON fields, selected dispatch, endpoint/model metadata and output hashes.
- [Hosted coding roles](verification/hosted-coding-results.json): production CLI commands in private tmux sessions, two real file-editing turns and runtime model identity.
- [OpenCode advisors](verification/opencode-advisor-results.json), [OpenCode chat](verification/opencode-scoping-chat-results.json), [OpenCode coding](verification/opencode-coding-results.json), [Flint coding](verification/flint-coding-results.json), and [local Flint advisors](verification/flint-advisor-results.json).
- [Reasoning efforts](verification/reasoning-effort-results.json): every advertised effort, including maximum reasoning through Responses.
- [Full production launcher smoke tests](verification/hosted-startup-results.json):
  six real research/author starts across Claude, Codex and Gemini in fresh private state.
- [Installed Gemini routing policy tests](verification/gemini-fallback-policy-results.json):
  identity resolution and synthetic 404/429/500 responses retain the selected model.
- [Flint runtime provenance](verification/flint-runtime.json) and
  [local serving instructions](flint-live-verification.md).

Corrected failures retain earlier attempts in the evidence when available. Retests explicitly used the same model. No alternative model is counted as a pass. Unit tests separately verify unavailable credentials, malformed responses, quota errors, and incomplete reviews fail visibly without substitution.

## Strict selection changes

Backend verification passed **1,227 distinct tests**: the complete collected
suite passed 1,224/1,224, and the final 24-test startup regression run covered
three additional trust-dialog cases added during that run. No final collected
test was omitted. The three end-to-end suites passed **35/35 checks**.

The final WebKit/Chromium suite passed **146 tests**, including every eligible
dropdown model-role pair and failed-review, scoping-chat and plan-update states.
The [iPhone incomplete-review screen](screenshots/mobile-review-failure.png)
was visually inspected in WebKit; the error is readable and controls remain accessible.

Scoping uses its exact selected model. Required reviewers and tiebreakers must complete before a decision can be accepted. Missing keys never remove a reviewer. PI failures return a named error and send no nudges. Failed plan updates preserve the prior plan with an explicit error; paper assessment failures cannot be accepted as completed proposals.

Gemini CLI model promotion and availability chains are pinned to the exact selection. Claude fallback targets are restricted to that same selection. OpenCode pins main and maintenance requests, API model IDs and provider access. Startup prompts cannot accept another model on the user’s behalf.

## Reproduce

The scripts read optional saved credentials through a read-only SQLite connection. They use private workspaces, databases and sessions, never production training or email. Hosted probes incur normal provider usage. See [advisor methodology](hosted-advisor-matrix.md) and [provider setup](model-provider-verification.md).

```sh
python scripts/probe_advisor_roles.py --credentials-db /path/to/autoresearch.db --output /tmp/advisors.json
python scripts/probe_advisor_roles.py --credentials-db /path/to/autoresearch.db --role scoping_chat --output /tmp/chat.json
python scripts/probe_agent_roles.py --help
python scripts/probe_opencode_roles.py --help
python scripts/probe_model_efforts.py --help
```
