## Potluck Local: Python sidecar

This directory contains the FastAPI sidecar that runs inside the Potluck AI desktop app. It binds to `127.0.0.1:8321` and handles local LLM inference via `llama-cpp-python`, model downloads, hardware detection, and user settings. The sidecar is packaged into a single frozen binary by PyInstaller and spawned by the Electron main process; end users never interact with Python directly.

### Dev setup

```
cd apps/desktop/python
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m potluck_local.app --port 8321
```

Set `POTLUCK_DEV=1` to enable Swagger UI at `http://127.0.0.1:8321/docs` and verbose logging.

### Coding-agent context discovery

`GET /v1/potluck/context?model=<id>&scope=local|mine` reports the loaded
`context_window` for the same owned-machine route a chat request would use.
It uses gateway authentication, accepts `potluck_model_sha256` to honor an exact
project pin, sends no prompt and runs no inference. The response includes only
model, scope, context window and the route category (`local` or `household`).
Older serving engines may return a null window. This is a capacity snapshot,
not a reservation: reloading the model or failing over can change it.

### Native local inference (experimental, opt-in)

macOS and Linux builds containing Potluck's pinned native engine can use
`llama-server` for a single local GGUF. The default remains `llama-cpp-python`.
Set `runtime.backend` to `llama-server` through `PATCH /settings`, then reload
the model through the usual `/models/{slug}/load` route. For example, against
an isolated development sidecar:

```sh
curl -X PATCH http://127.0.0.1:8399/settings -H 'Content-Type: application/json' \
  -d '{"runtime":{"backend":"llama-server","native_cache_mib":256}}'
```

The child uses loopback, a per-process key, one inference slot, automatic Flash
Attention and a bounded RAM prompt cache (256 MiB by default, 0 disables it;
maximum 4096 MiB). This cache limit is additional to model weights, active KV
cache and compute buffers. The requested context must match the native engine's
effective context; a mismatch fails the load. Context shifting is disabled.
The engine uses the GGUF's native chat template, and Potluck advertises tool
support only after the engine's renderer includes a probe tool.

Potluck still enforces the model file's SHA-256, request routing and cancellation.
If cancellation cannot confirm the native slot is idle, the child is stopped and
the model must be reloaded. A child failure clears loaded/identity availability.
Control-token text inside request content stays plain text, as it does for managed split mode (`prompt_guard`). No prompt cache is saved to disk by this integration. Native process logs are
filtered by the existing supervisor rather than copied into Potluck's logs.

This is an engine foundation, not model or hardware qualification. Answer checks
and execution-placement evidence, scoring and the built-in speed benchmark are
not yet available for this backend:
`answers_verified` is false, and lending to other people is paused. Local use
and requests from your own machines remain available. It does not enable raw
RPC endpoints, install new models, or establish support for every architecture.
Use managed split mode separately for multiple-machine GGUF inference.

For source builds, build the pinned bundle with `scripts/build-split-engine.py`
and set `POTLUCK_SPLIT_ENGINE_DIR` to its output. The existing manifest pin is
required; an arbitrary external server is not accepted. Missing bundles fail
explicitly. To revert, set `runtime.backend` to `llama-cpp-python` and reload.
Windows native-local support is not enabled because the supervisor uses POSIX
process groups; Windows keeps the default backend.
