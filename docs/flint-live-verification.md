# Local Flint verification

For current installation and lifecycle behavior, see [Managed local Flint](managed-flint.md).
Selecting Flint now downloads, verifies and starts the local model automatically.
The manual commands below document the original October 28 verification.

On October 28, actual Recursive Flint weights passed research and author
coding tasks, scoping, both reviewer slots, tiebreaker, PI, and scoping chat.
The tests used a temporary localhost server on an idle GPU. They did not
change the production provider settings. See the [role matrix](model-role-matrix-2026-10-28.md)
for the final results and configuration revision of each attempt.

## Reproducible runtime

- Checkpoint: [`RecursiveSI/flint-int4-gguf`](https://huggingface.co/RecursiveSI/flint-int4-gguf),
  revision `9c47151036bc2c34331142dda04488bceab2bc9f`, `flint-int4-q4_1.gguf`.
- File size: 25,992,966,976 bytes. Verified SHA256:
  `43c07baccdba42eb7528a9630ee15699b1942c297291dc1b39a061f508ee1a4b`.
- CUDA llama.cpp commit: `cf24317bea431b807dc93319d74c61bed6e20aff`.
- Context: 16,384 tokens, one server slot. App output budget: 8,192 tokens,
  including reasoning. Served ID: `flint`; UI selection: `flint/local`.
- The original test used `/tmp/arui-flint-live`. On October 29 its verified
  checkpoint and pinned runtime moved into the persistent managed cache,
  `/var/lib/autoresearcherui/models/flint`; the old path is a compatibility link.

The test used idle GPU 9. To reproduce on that host when the device is available:

```sh
CUDA_VISIBLE_DEVICES=9 /tmp/arui-flint-live/build/bin/llama-server \
  --model /tmp/arui-flint-live/flint-int4-q4_1.gguf --alias flint \
  --ctx-size 16384 --n-gpu-layers 99 --parallel 1 --threads 8 \
  --cache-ram 0 --jinja --flash-attn on \
  --temp 1.0 --top-p 0.95 --top-k 20 --min-p 0 \
  --host 127.0.0.1 --port 18342
```

That test configured the external server URL as `http://127.0.0.1:18342/v1`
and served model as `flint`. Advanced external-server mode still supports
custom endpoints and `ARUI_FLINT_CONTEXT_TOKENS` / `ARUI_FLINT_MAX_OUTPUT_TOKENS`
budgets. Normal managed setup needs none of these connection fields.
See [Recursive's NVIDIA setup](https://docs.recursive.com/local/nvidia) for
installation options.

## Findings and limits

The previous 32k completion reservation could not fit a 16k server. A 4k
budget then exhausted reasoning before a visible answer. The adapter now
uses the configured local budget and reports truncation explicitly. The
tiebreaker prompt also now allows an inconclusive verdict when evidence is
insufficient; its concise schema completed within the same 8k budget.

Research and author tests used real read/write/shell tools on small synthetic
files. Advisory tests checked the required JSON fields and exact served ID.
These tests validate connectivity, role prompts and tools; they do not certify
research quality or long-project context capacity. Larger projects may require
a larger server context. A rejected or truncated request remains an explicit
failure of the selected local model.
