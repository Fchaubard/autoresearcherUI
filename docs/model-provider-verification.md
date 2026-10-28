# Model providers: implementation and verification

Catalog verified on 2026-10-27; real role testing expanded on 2026-10-28.
The catalog has 41 models: all support scoping, both council reviewer slots,
tiebreaker and PI; 32 also support research and author coding agents.
The nine API-only compatibility models are intentionally absent from coding
role dropdowns. Runtime availability depends on provider capacity,
credentials and the configured local server. See the
[live role matrix](model-role-matrix-2026-10-28.md) for per-model results.

Model selection is strict. Missing credentials, quota, malformed responses,
or an unavailable model produce a named error. The app does not select another
model, accept a surviving reviewer's decision as consensus, or count a failed
review as approval. Underlying Gemini, Claude and OpenCode client routing is
also pinned to the selected model. Explicitly disabling both review slots
records a skipped review; it does not fabricate an approval.

## Catalog and configuration

New Gemini choices: `gemini-4-argon` and `gemini-3.1-flash-lite`.
Existing Gemini entries, including `gemini-3.8-flash`, remain available.
These support both the Gemini API and Gemini CLI. The launcher grants trust
only to its selected working directory for that subprocess using
`GEMINI_CLI_TRUST_WORKSPACE=true`; current CLI releases otherwise stop before
accepting an unattended research prompt.

OpenCode uses the installed **OpenCode 2** client. Set `ARUI_OPENCODE_BIN` if
it is outside PATH and `~/.opencode/bin`. Its free chat catalog is:

| Dropdown ID | Display name |
| --- | --- |
| `opencode/nemotron-3-ultra-free` | Nemotron 3 Ultra |
| `opencode/nemotron-3.5-lightning-free` | Nemotron 3.5 Lightning |
| `opencode/nemotron-3.5-super-vl-free` | Nemotron 3.5 Super VL |
| `opencode/big-pickle` | Big Pickle |
| `opencode/glm-5.3-flash-free` | GLM 5.3 Flash |
| `opencode/deepseek-v4.1-flash-free` | DeepSeek V4.1 Flash |
| `opencode/beam-free` | Beam |
| `opencode/flint-40b-free` | Hosted Flint 40B |
| `opencode/mimo-v2.6-flash-free` | MiMo V2.6 Flash |

These are limited-time free offers. OpenCode requires its client for free
chat models; calling their public completion endpoint directly is unsupported.
The optional `opencode_token` setting maps to `OPENCODE_API_KEY`. Jev's free
endpoint evaluates typed decisions instead of generating chat/tool responses,
so it cannot fill these research roles and is omitted.

Local Flint is a separate `flint/local` choice. Configure:

| Setting | Environment equivalent | Meaning |
| --- | --- | --- |
| `flint_base_url` | `ARUI_FLINT_BASE_URL` | Running OpenAI-compatible server, e.g. `http://127.0.0.1:30000/v1` |
| `flint_model` | `ARUI_FLINT_MODEL` | Exact served ID from `/v1/models`; default `flint` |
| `flint_token` | `ARUI_FLINT_API_KEY` | Optional server authentication |
| `flint_context_tokens` | `ARUI_FLINT_CONTEXT_TOKENS` | Server context size, default `16384` |
| `flint_max_output_tokens` | `ARUI_FLINT_MAX_OUTPUT_TOKENS` | Output budget including reasoning, default `8192`; must be below context size |

The app connects to a server the operator starts; selecting Flint does not
download weights or take GPUs from experiments. Recursive's reference SGLang
server uses `flint`; other runtimes may use `RecursiveSI/flint` or another
configured alias. Connection validation checks both the served ID and a
synthetic tool call, detecting servers missing the tool parser.

Research/author use OpenCode's interactive `mini` client. Free hosted advisors
run in private temporary directories and databases. They may read only the
staged `context.txt`; shell, writes, network, other files and subagents are
denied. A completed context read is required before accepting the answer.
The earlier deny-all-tools request was rejected by the free service and
produced misleading 429 responses; the documented read-only reviewer agent
works with the real client.
Each invocation uses a private server so current credentials/settings apply
without modifying the user's shared service. Local Flint advisors use the
OpenAI-compatible completion protocol directly. Credentials are environment
values and are never included in shell command text or diagnostics.
Gemini API authentication uses headers instead of query strings. Flint's
OpenCode adapter preserves `reasoning_content` across tool turns, sends the
server-compatible `max_tokens` field, and declares text-only input support.
Both idle and active OpenCode mini terminal states were inspected through a
PTY; the author feeder and supervisor recognize their distinct status text.

## Observed tests

All 269 supported model-role pairs were attempted. The 31 hosted models passed
all 155 advisor checks, 44 supported coding-role checks, and 31 scoping chat
checks. All 76 advertised API effort combinations passed. Local Flint passed
all seven roles plus chat with actual weights; its
[checkpoint, runtime and serving instructions](flint-live-verification.md) are recorded separately.

The nine free OpenCode models passed 35 of 63 core-role checks before final
configuration isolation. Nemotron Ultra was unavailable, and other remaining
core checks hit free-tier usage limits. Quota also blocked live revalidation
of the final isolated configuration. All nine free chat attempts failed;
their original probe wrapper did not retain the underlying HTTP status.
Those limitations are explicit in the [per-model matrix](model-role-matrix-2026-10-28.md).
No alternate paid or free model was substituted.

Final configuration isolation, exact model routing, read-only permissions and
blocked custom delegates passed real OpenCode CLI tests against a synthetic
server with hostile inherited settings. These protocol tests are recorded
separately from successful real inference.

Run the focused deterministic suite:

```sh
mkdir -p /tmp/arui-model-tests
TMUX= TMUX_TMPDIR=/tmp/arui-model-tests ARUI_DISABLE_BG=1 ARUI_TELEMETRY_DISABLED=1 \
  python -m pytest -q tests/unit/test_additional_providers.py tests/unit/test_role_model_permutations.py \
  tests/unit/test_agent_cli.py tests/unit/test_agent_watcher.py tests/unit/test_author_agent_repl.py \
  tests/unit/test_token_validation.py tests/unit/test_council.py tests/unit/test_pi.py tests/unit/test_scoping.py
```

Run the real OpenCode client against a local synthetic server (no GPU or hosted
inference; the test writes one temporary file through a real coding tool):

```sh
TMUX= TMUX_TMPDIR=/tmp/arui-model-tests ARUI_DISABLE_BG=1 ARUI_TELEMETRY_DISABLED=1 \
  ARUI_TEST_OPENCODE=1 python -m pytest -q tests/unit/test_opencode_protocol.py
```

For explicit live probes after loading provider environment variables, run
`python scripts/probe_model_support.py`, or restrict requests with repeated
`--model` options. Results contain status and the synthetic answer only.

## Official sources

- [Google model catalog](https://ai.google.dev/gemini-api/docs/models)
- [Gemini 4 Argon](https://ai.google.dev/gemini-api/docs/models/gemini-4-argon)
- [Gemini 3.1 Flash-Lite](https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-lite)
- [Gemini CLI automated workspace trust](https://geminicli.com/docs/cli/trusted-folders/#headless-and-automated-environments)
- [OpenCode Console models and free-model restrictions](https://opencode.ai/v2/docs/console/models/)
- [OpenCode 2 automation and standalone servers](https://opencode.ai/v2/docs/cli/)
- [OpenCode 2 provider schema](https://opencode.ai/v2/docs/providers/)
- [OpenCode reasoning and compatibility fields](https://opencode.ai/v2/docs/models/#reasoning)
- [OpenCode 2 permissions](https://opencode.ai/v2/docs/permissions/)
- [Recursive Flint release](https://recursive.com/updates/introducing-recursive-flint)
- [Recursive Flint model card and serving instructions](https://huggingface.co/RecursiveSI/flint)
- [Recursive runtime documentation](https://docs.recursive.com/)
