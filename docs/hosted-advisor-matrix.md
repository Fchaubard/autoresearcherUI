# Live hosted advisor-role matrix

The explicit probe in `scripts/probe_advisor_roles.py` executes the selected
model through the production role adapters and prompts. It covers scoping,
reviewer 1, reviewer 2, tiebreaker, and PI; `--role scoping_chat` also covers
the free-text scoping conversation path.

Every cell runs in a fresh process with an isolated database, workspace and
tmux directory. The harness sends only small synthetic ridge-regression
fixtures. It does not run the research loop, launch training, mutate live
settings, send messages, or send email. Optional saved credentials are read
through SQLite read-only mode and remain in process environment variables.

The harness checks that exactly the selected model was dispatched. It checks
required fields in the raw JSON before the production parser can supply
missing defaults, validates the parsed role result, and records endpoint,
requested model, reported response model, HTTP status, elapsed time, and an
output hash. It never records credential values or raw provider error bodies.
Each API request has a 90-second deadline, output is capped at 4,096 tokens,
and at most two cells run concurrently. A timeout is a failed bounded probe,
not proof that the provider can never finish a longer request.

## Observed result: 2026-10-28

All **155 of 155 hosted model × advisor-role cells passed**, covering 31
models in all five roles. All **31 of 31 free-text scoping cells passed**.
These are successful real inference and parsed role outputs, not catalog
visibility or mocked responses. The complete sanitized records are checked
in as [advisor evidence](verification/hosted-advisor-results.json) and
[scoping conversation evidence](verification/hosted-scoping-chat-results.json).

The initial run exposed four o3-pro request-shape failures and one Haiku
non-JSON tiebreaker response. After correcting the adapter and adding the
explicit tiebreaker schema, all five o3-pro roles passed, and tiebreaker
regressions passed with Haiku, GPT-6 Astra, and Gemini 4 Argon. Their final
records retain previous attempts and identify the changed request or prompt.
Already passing unrelated cells were not repeatedly charged and rerun.
A subsequent local Flint tiebreaker exhausted its 8,192-token reasoning
budget. A shorter prompt retaining the same JSON fields and scientific
learning markers resolved it; it permits inconclusive evidence instead of
forcing certainty. The revised prompt passed again with Haiku, GPT-6 Astra,
and Gemini 4 Argon, and those final records retain their earlier versions.

| Model | Five advisor roles | Scoping conversation |
| --- | --- | --- |
| `gpt-6-astra` | 5/5 passed | Passed |
| `gpt-5.6-sol` | 5/5 passed | Passed |
| `gpt-5.6-terra` | 5/5 passed | Passed |
| `gpt-5.6-luna` | 5/5 passed | Passed |
| `gpt-5.5` | 5/5 passed | Passed |
| `gpt-5.2` | 5/5 passed | Passed |
| `gpt-5.1` | 5/5 passed | Passed |
| `gpt-5` | 5/5 passed | Passed |
| `gpt-5-mini` | 5/5 passed | Passed |
| `gpt-5-nano` | 5/5 passed | Passed |
| `o3` | 5/5 passed | Passed |
| `o3-pro` | 5/5 passed | Passed |
| `gemini-4-argon` | 5/5 passed | Passed |
| `gemini-3.1-flash-lite` | 5/5 passed | Passed |
| `gemini-3.8-flash` | 5/5 passed | Passed |
| `gemini-3.7-flash` | 5/5 passed | Passed |
| `gemini-3.6-flash` | 5/5 passed | Passed |
| `gemini-3.5-flash` | 5/5 passed | Passed |
| `gemini-3.5-flash-lite` | 5/5 passed | Passed |
| `gemini-3.1-pro-preview` | 5/5 passed | Passed |
| `gemini-2.5-pro` | 5/5 passed | Passed |
| `gemini-2.5-flash` | 5/5 passed | Passed |
| `claude-fable-5-1` | 5/5 passed | Passed |
| `claude-opus-5` | 5/5 passed | Passed |
| `claude-sonnet-5` | 5/5 passed | Passed |
| `claude-fable-5` | 5/5 passed | Passed |
| `claude-opus-4-8` | 5/5 passed | Passed |
| `claude-opus-4-7` | 5/5 passed | Passed |
| `claude-opus-4-6` | 5/5 passed | Passed |
| `claude-sonnet-4-6` | 5/5 passed | Passed |
| `claude-haiku-4-5-20251001` | 5/5 passed | Passed |

Focused deterministic tests additionally verify failed selected calls never
invoke another model, malformed approval booleans cannot approve code,
missing credentials remain visible, invalid PI output sends no messages,
and timed-out CLI probes terminate detached child processes. Isolated
end-to-end checks passed: fake research agent 13/13, real agent scaffold
14/14, completion supervisor 8/8. These do not certify arbitrary long-running
research outcomes or future provider availability.

## Reproduce

Use the project's Python environment. The credential database argument is
optional when provider environment variables are already set.

```sh
python scripts/probe_advisor_roles.py \
  --credentials-db /path/to/autoresearch.db \
  --output /tmp/arui-model-matrix/advisor-results.json
python scripts/probe_advisor_roles.py \
  --credentials-db /path/to/autoresearch.db \
  --role scoping_chat \
  --output /tmp/arui-model-matrix/advisor-scoping-chat-results.json
```

Repeat `--model` or `--provider` to select a subset. `--resume` retains completed
cells. Exit status is nonzero when any cell is unconfigured, fails, times out,
or has an invalid role response. The saved JSON identifies those cases
separately; catalog presence is never counted as successful inference.

## Compatibility changes verified by the suite

- Gemini and Claude responses may begin with thought content. The adapters
  join visible text blocks rather than reading only the first block.
- PI dispatch uses the same model-specific effort normalization and provider
  adapters as the council. A stale effort from the previous model cannot send
  an unsupported effort to the newly selected model.
- `o3-pro` uses the OpenAI Responses API. Structured output places the JSON
  instruction inside explicit developer/user input messages.
- OpenAI maximum reasoning also uses Responses; Chat Completions rejects the
  advertised `max` effort value for the current GPT-6/5.6 models.
- Scoping and strategic review retain the selected model when credentials or
  inference fail. A required failed reviewer or tiebreaker cannot become
  approval, consensus, or a decision from a surviving peer.
- Both reviewer toggles explicitly disabled means review was intentionally
  skipped. It never fabricates an approval or automatically enters paper mode.

Local Flint advisors use the configured server context and output limits.
Their separate live probe uses the same configured budget; reasoning tokens
can consume 4,096 tokens before a visible answer. Exhausting the budget
without an answer produces an actionable error. A small request must not
reserve 32k completion tokens on a 16k-context local server.

The separate agent-role, OpenCode/Flint, and reasoning-effort probes cover
other transports and settings; they must not be represented as this suite's
live hosted advisor results.

## Official OpenAI references

- [Reasoning models](https://developers.openai.com/api/docs/guides/reasoning)
- [o3-pro API compatibility](https://developers.openai.com/api/docs/models/o3-pro)
- [Structured output and JSON mode](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses)
