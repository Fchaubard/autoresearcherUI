# Managed local Flint

Selecting **Flint local** in any model dropdown starts automatic setup. The app downloads the official checkpoint once, verifies its SHA-256, installs a pinned llama.cpp runtime, and starts a private localhost server. The Settings and onboarding forms display download progress, startup, resource waits, CPU availability, and retryable errors. An advanced external-server mode remains available, including compatibility with previously saved external URLs.

The selected model stays Flint throughout. Starting on CPU changes the hardware, not the model or provider. The app never substitutes a hosted model when Flint is unavailable. A pending research/author change keeps the existing agent alive until Flint is ready; Settings identifies that pending handoff explicitly.

## Resources and lifecycle

- Automatic hardware selection requires an idle GPU with at least 32 GiB free and no compute processes. The app checks again immediately before allocation. It never stops a training process to make room.
- CPU execution requires 48 GiB of available RAM and uses four inference threads, one slot, a 16,384-token context, and an 8,192-token output budget. CPU responses can take several minutes for reasoning-heavy prompts; the default managed CPU request timeout is 1,800 seconds.
- Installation requires the 26 GB checkpoint plus 8 GiB free working space. Interrupted downloads are retained and resumed. A checksum mismatch is reported, never silently ignored.
- The default persistent root cache is `/var/lib/autoresearcherui/models/flint`; non-root installations use `~/.cache/autoresearcherui/models/flint`. `ARUI_MODEL_CACHE_DIR` can relocate the cache. Private lifecycle state and a randomly generated API key are stored below the app data directory in `flint/`.
- Process ownership includes PID creation time, executable, model path, and private key-file path. Stop/recovery never signals an unrelated process, and an unhealthy owned server is stopped before replacement. Concurrent setup requests share startup and installation locks.
- The supervisor checks recovery at most every 30 seconds. Failed starts use backoff; repeated crashes stop automatic recovery after three attempts. **Stop Flint** suppresses automatic restart until the user explicitly starts it again. Changing hardware during setup updates the choice before allocation.
- `ARUI_DISABLE_BG=1` disables provisioning in automated tests. Explicit isolated live tests can opt in with `ARUI_FLINT_ALLOW_SETUP=1`.

## Pinned sources

| Component | Pin |
| --- | --- |
| Checkpoint | [RecursiveSI/flint-int4-gguf](https://huggingface.co/RecursiveSI/flint-int4-gguf/tree/9c47151036bc2c34331142dda04488bceab2bc9f), `flint-int4-q4_1.gguf` |
| Checkpoint revision | `9c47151036bc2c34331142dda04488bceab2bc9f` |
| Checkpoint bytes | `25992966976` |
| Checkpoint SHA-256 | `43c07baccdba42eb7528a9630ee15699b1942c297291dc1b39a061f508ee1a4b` |
| Runtime | [llama.cpp](https://github.com/ggml-org/llama.cpp/tree/cf24317bea431b807dc93319d74c61bed6e20aff), commit `cf24317bea431b807dc93319d74c61bed6e20aff` |

The managed server uses `--alias flint --ctx-size 16384 --parallel 1 --threads 4 --threads-batch 4 --cache-ram 0 --jinja --temp 1.0 --top-p 0.95 --top-k 20 --min-p 0`. It binds only `127.0.0.1`, selects a free port, authenticates with its private API key file, and hides all GPUs for CPU execution. Public status responses exclude the key and private filesystem paths.

## Verification

Lifecycle tests cover busy GPUs, idempotence, concurrent starts, owned-process replacement, reused PIDs, manual stop, bounded recovery, checksum-verified resumed downloads, storage failures, public status, and API validation. Browser tests exercise automatic setup, pending model handoff, errors/retry/stop, external-mode compatibility, accurate conversation-continuation messages, and a 320-pixel iPhone onboarding form in WebKit and Chromium.

On 2026-10-29 the real managed CPU server loaded the pinned checkpoint without allocating a GPU, served exact model `flint`, and completed a 44-token synthetic request in 2.64 seconds. Broader role probes use isolated fixtures and real production dispatch/parsers; their results are recorded separately. Small-request timing is not a claim about production-length CPU latency.
