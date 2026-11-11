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

### Per-request thinking controls

Coding clients using `POST /v1/chat/completions` can send
`"chat_template_kwargs": {"enable_thinking": false}` or
`"reasoning_effort": "none"` to request a response without extra thinking.
For `POST /v1/responses`, use `"reasoning": {"effort": "none"}`.
Set `enable_thinking` to `true` to request thinking, or omit both controls to
keep the model's default. This is separate from `reasoning_format: "none"`,
which only keeps reasoning in the answer text and does **not** disable it.

These controls require the native `llama-server` backend or a managed split
head. Both whole and streamed gateway replies support them. Household routing
also preserves them when the serving machine advertises
`reasoning_controls_version: 1`; retries only use machines with that capability.
`GET /v1/potluck/capabilities` reports the same field for the local engine.
The internal peer route accepts them on unbatched streams (the gateway uses
this transport for both response modes). The in-process Python backend,
pipeline/batch mode, older household engines, and circle/pool routes return a
clear `reasoning_controls_unsupported` error instead of ignoring the setting.

Chat completions accept an explicit boolean `parallel_tool_calls` when offering
function tools. The native runtime forwards it to the engine and template;
`false` requests one call at a time. Gateway
`parallel_tool_calls_request_version: 1` identifies the request contract, and
native engine/peer `parallel_tool_calls_version: 1` identifies route support.
Unsupported backends and older peers refuse the control; retries preserve it.
Clients must still validate complete calls and enforce their execution policy.

Both APIs also accept the Potluck extension `reasoning_budget_tokens` (integer
0–65535), with an explicit larger output limit: `max_tokens` for chat, or
`max_output_tokens` for Responses. The native engine caps reasoning inside that
output limit; closing reasoning delimiters and the final answer also use tokens.
This is not a guarantee of a completed answer. The gateway advertises
`reasoning_budget_request_version: 1` for request validation. Native engines and
updated household peers advertise `reasoning_budget_version: 1`; retries preserve
the exact budget and skip older peers. Budget-only requests use the template's
default thinking mode.

`POST /v1/potluck/count-tokens` accepts the chat request shape and `local` or
`mine` scope. `prompt_count_version: 1` advertises this optional endpoint. For a
local native model it renders and tokenizes with the same tools, controls and
prompt protection as generation, returning `prompt_tokens` and the loaded
`context_window`. It checks model pins under the model lifecycle gate and never
starts inference or records usage. Unsupported backends and remote household
routes return null counts; they do not forward the prompt or invent an estimate.
Counts describe the current model snapshot; generation still enforces its own
context limit if the runtime changes between requests.

A prompt containing protected control-token text needs a raw-token rendering
path. Reasoning budgets currently fail explicitly on that path; prompt protection
stays enabled and the engine remains running. Retry without the budget, or use a
supported thinking toggle. Never interpret refusal as a successful generation.

Effort values `none`, `minimal`, `low`, `medium`, `high`, and `xhigh` are passed
to the native engine. Their effect depends on the loaded model's template;
transport support does not establish that a model honors every effort level or
can disable thinking. Only the boolean `enable_thinking` template variable is
accepted. Invalid types, other template variables, and contradictory controls
return 400 before inference. Turning thinking off can change answer quality;
it is an explicit client choice, not an automatic speed optimization.

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

### Pinned model recipes (experimental)

`GET /models/recipes` lists bundled setups with an immutable Hugging Face revision,
exact GGUF filename, size and SHA-256, native engine commit, context window and
resource settings. These recipes are independent of the refreshable model catalog.
They are candidate setups, not a claim of model quality or hardware qualification.

`GET /models/recipes/{recipe_id}/preflight` inspects the engine, platform, live
available memory, disk space and installed artifact. It does not download, load or
change preferences. RAM estimates include weights, context-dependent KV cache,
prompt cache and working overhead; they are not measured peak usage. Unified RAM
is counted once. Unknown free dedicated GPU memory blocks an accelerated load.
The memory held by an existing loaded model is not guessed back into the budget.

`POST /models/recipes/{recipe_id}/install` starts an explicitly requested download
from the pinned revision and verifies its size and hash. Use the returned `slug`
with the usual download-progress route. An existing different file is never
overwritten. `POST /models/recipes/{recipe_id}/load` repeats preflight and loads
exact recipe settings, with no smaller-context or backend fallback. File identity
is checked again before replacing the current model and after native loading.
Resource availability is a snapshot, not a reservation or a guarantee against OOM.

A successful recipe load is session-only: it clears the previous startup model
choice, so restarting cannot silently restore that model with different settings.
Other runtime preferences stay unchanged. A failed preflight preserves the
current model and startup choice. Ordinary model loads still use the existing
restart behavior. See [recipe provenance and commands](../../../docs/model-recipes.md).

### Source collections (local development foundation)

Source collections register explicitly selected Markdown folders in a workspace.
This developer command is the first Company Brain milestone, not the complete
investigation workflow or an enterprise release. It exposes no HTTP/MCP route.

```sh
# Use a disposable database while evaluating the feature.
export POTLUCK_MEMORY_DB_PATH=/absolute/path/to/experiment/memory.db
python -m potluck_local.services.source_collections --workspace example register runbooks /absolute/path/to/runbooks --exclude 'drafts/**'
# Use the collection_id printed by registration.
python -m potluck_local.services.source_collections --workspace example scan COLLECTION_ID
python -m potluck_local.services.source_collections --workspace example status
python -m potluck_local.services.source_collections --workspace example remove COLLECTION_ID
```

Registration binds a name and real directory to a workspace. Repeat registration
with the same name/root is idempotent. Changed include/exclude rules immediately
clear that collection's derived content and require a new scan. A root change
requires a new collection. The command adds `--exclude` patterns to defaults
excluding `.git`, `.obsidian`, and lowercase `archive` directories. Use explicit
patterns for other historical folders/files, including exported memory archives.
Patterns use case-sensitive POSIX paths and shell-style wildcards; `**/` also
matches at the root. The default include is `**/*.md`.

Scans reuse Markdown chunking and credential redaction, track stable document and
revision identifiers, and atomically replace only that collection's evidence.
Unchanged files retain their IDs. Removed or excluded files lose derived content;
empty files replace old chunks. The 2 MiB per-file limit, invalid UTF-8 or a failed
read aborts the scan and records failed status while preserving the last complete
snapshot. Check status/freshness before using evidence. Symlink files/directories
are excluded; registration requires a real path. This local-owner facility is
not an OS sandbox against a hostile process mutating parent directories.

Evidence is stored in `source_collections`, `source_documents`, and `source_chunks`
in the existing local database. These are raw-source records, separate from
approved knowledge and personal memory. No embedding or inference runs, and no
source is sent to another machine. Existing personal/global memory, legacy vault
imports and agent search are unchanged. `remove` cascades through the selected
collection's derived records without touching the original files. A personal
memory wipe does not remove source collections; remove them explicitly.

Workspace labels organize this local store, not authentication. Permission-aware
retrieval, review/supersession, investigation briefs, evaluations, and shared-user
authorization remain subsequent milestones.
