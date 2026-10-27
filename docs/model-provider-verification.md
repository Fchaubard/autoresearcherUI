# Model providers: implementation and verification

Verified against official catalogs on 2026-10-27. All entries below are
available to research, author, scoping, both council reviewer slots, the
tiebreaker and PI. Runtime availability still depends on provider capacity,
credentials and the configured local server.

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

The app connects to a server the operator starts; selecting Flint does not
download weights or take GPUs from experiments. Recursive's reference SGLang
server uses `flint`; other runtimes may use `RecursiveSI/flint` or another
configured alias. Connection validation checks both the served ID and a
synthetic tool call, detecting servers missing the tool parser.

Research/author use OpenCode's interactive `mini` client. Advisors run with
all tools denied, in temporary directories, with context passed through stdin.
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

These results distinguish actual inference from a protocol fixture.

| Target | Result on this host |
| --- | --- |
| Gemini 4 Argon API | HTTP 200; synthetic arithmetic returned `4` |
| Gemini 3.1 Flash-Lite API | HTTP 200; synthetic arithmetic returned `4` |
| Gemini 3.8 Flash API regression | HTTP 200; synthetic arithmetic returned `4` |
| Gemini 4 Argon CLI | Exit 0; returned `4` after workspace trust fix |
| Gemini 3.1 Flash-Lite CLI | Exit 0; returned `4` after workspace trust fix |
| OpenCode 2.0.29 catalog | All nine free IDs listed by the real client |
| Nemotron 3 Ultra inference | HTTP 400: upstream model unavailable |
| Other eight free models | HTTP 429: free usage limit reached |
| Local Flint weights | No running local server supplied; no weights launched on occupied GPUs |
| Local Flint protocol | Real OpenCode CLI against synthetic HTTP/SSE server; model-ID routing, tool-disabled advisor, actual file-writing tool and reasoning-content replay passed |

Free-provider inference could not be certified successful while those upstream
responses persisted. No paid fallback was used. Catalog validation reports
model visibility separately from inference/quota. Runtime errors retain useful
HTTP status classifications without exposing nested provider request details.

Unit coverage exercises each free model and new Gemini model through all
roles, local endpoint validation, tool parser checks, shell quoting, safe
errors, and restart workspace preservation. Existing agent-launch,
author-feeder, token-validation, council, PI and scoping regressions also pass.

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
