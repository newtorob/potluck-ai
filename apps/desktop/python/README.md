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

### Internal fixed-part investigation corrections

The experimental `fixed-part-selection-v1` service path accepts explicitly supplied
question parts and preserves exact selected source ranges. It is internal and
opt-in; automatic question coverage and semantic correctness remain unverified.

A saved correction is separate from the immutable original answer and provenance.
Its validator uses the fixed-part limits of 8,000 total text characters and up to
100 paragraph citations. Exact source ranges can include whitespace-only paragraph
snippets. Corrections verify every supplied citation and excerpt before removing
those blank entries from the separate correction. Supported and conflicting
corrections still require nonblank source support. Reopen validates the normalized
correction again; export and outcome recording preserve the original. Existing
selectors and their correction contracts are unchanged.

### Internal per-part evidence roles

The separately opted-in `fixed-part-selection-v2` mode uses the same explicit
question-part plan and current reviewed source-unit authority. Its independent
second call distinguishes direct answers, incompatible alternatives and background
context before choosing each part's verdict. Known parts remain visible beside
scoped unknown details; unresolved conflicts retain both alternatives without an
authority winner. The displayed answer preserves these same per-part leads.

Exact source-unit ranges and governing dependencies are retained. The existing
16 closed-unit, 8,000-character and measured generation limits still reject
overflow without clipping or retries. Corrections remain separate from original
raw replies, and DB mode/plan bindings are rechecked through read, review, export
and outcome. Older modes remain replayable. This internal mode adds no HTTP/MCP
route or new default; role shape, source binding and fixture-supplied plans do not
verify semantic correctness or complete question coverage.

The separately opted-in `fixed-part-selection-v3` contract represents each part
as zero, one or two nonempty answer groups, plus necessary background context.
The server derives unknown, supported or conflicting status from that shape;
there is no separately generated verdict that can contradict the groups. IDs,
dependencies, visible limits, current authority and stored plan/mode bindings
remain checked. Shape does not establish relevance, absence or a real conflict.

Adjacent identical validated source blocks may be displayed once with every
question/status lead retained in order. Full per-part roles, exact passages,
ranges and citations remain in the original trace. The displayed answer uses
that same text through correction and export. V1/V2 records are not rewritten.
This is an internal experimental opt-in with no native qualification or default
promotion implied; automatic coverage remains unverified.

The separately versioned internal `fixed-part-selection-v4` keeps the grouped
contract and asks both stages to check the full question alongside its shorter
part labels. A label cannot narrow the original request. For safeguard questions,
selection must include applicable source-stated scope, isolation and privacy
protections without inventing rules or adding unrelated implementations. This
guidance does not verify semantic coverage; older V1/V2/V3 records and current
defaults remain unchanged.

The opt-in internal `fixed-part-selection-v5` assigns each selected catalog unit
one role using a unique object key. Duplicate JSON keys reject; an ID cannot
occupy answer and context roles together. Zero/one/two nonempty answer groups
still derive the status, with exact source closure and original raw/replay
bindings. Structural exclusivity does not prove support, conflict, absence or
question coverage. Native qualification remains required; V1-V4 and defaults
are preserved.

The internal `fixed-part-selection-v6` keeps the unique-unit-key contract and
separately versions instructions to distinguish additive safeguards from competing
values, scan applicable global constraints and follow explicit source references.
These are model instructions, not verified source dependency edges or semantic
proof. Prior V1-V5 contracts and defaults remain unchanged; native development
and full separately frozen qualification are required.

The experimental `fixed-part-selection-v14` mode uses an order-bound compact applicability inventory and explicit verdicts constrained to agree with the selected evidence groups. Comparison and missing-property instructions are reviewed independently against frozen cases; structural validity alone is not answer correctness. Defaults remain unchanged.

The experimental `fixed-part-selection-v15` mode keeps V14 semantics and strict decoder verdict/group matching while using the existing runtime-admitted typed object/array/enum schema subset. Native grammar alone does not establish cross-field consistency. V14's native union-schema admission failure remains archived; defaults and ordinary runtime admission are unchanged.

The experimental `fixed-part-selection-v16` mode removes the redundant model verdict field. The server derives unknown/supported/conflicting from zero/one/two selected answer groups; checks remain untrusted metadata, and alternative-only groups are rejected. Exact source selection remains fallible and needs semantic evaluation. V15 failures and all older contracts remain immutable; current defaults, profile and caps are unchanged.

The experimental `fixed-part-selection-v17` mode retains exact selected units, part groups, dependencies and request context while displaying each unit identity once with part references. Generic selection instructions emphasize qualification coverage and necessary context; selection remains fallible. Older contracts and scores, model profile, budgets and defaults remain unchanged. This combines prompt and presentation changes; full qualification is still required.

The experimental `fixed-part-selection-v18` mode uses an explicit relation after an untrusted short check and one source-evidence set per part, without redundant alternative partitioning. A typed first inventory distinguishes answer facts and qualifiers; review may override it. Exact selected units, context and dependencies remain visible, and literally adjacent single-range units from the same citation share a display block to preserve source formatting. Unknown requires no answer evidence; supported/conflicting require evidence, but structural validity does not establish semantic correctness. All prior contracts, defaults, generation profile and caps remain unchanged.

The experimental `fixed-part-selection-v19` distinguishes selected source evidence, including a scoped absence basis, from an assertion of a requested property. A fixed catalog-unit/part-order role matrix and context booleans replace repeated applicability tags. Review states an explicit verdict after an untrusted check of at most 40 characters and requests compact JSON under unchanged output caps. Unknown may retain evidence marked Scope basis; supported/conflicting require evidence. Source identities, exact text, roles, dependencies and contiguous display remain bound. Structural acceptance does not prove semantic correctness. All older contracts, defaults, profile, sampler and caps remain unchanged.

The experimental `fixed-part-selection-v20` encodes each unit's part roles as fixed-width `n/e/q/b` characters and a final request-context bit. Strict decoding preserves V19 evidence/scoped-basis semantics and rejects malformed or obsolete wire formats. The schema uses supported length primitives; alphabet validation remains explicit. Source identities, review checks, presentation, caps, profile, sampler and defaults remain unchanged. Native token and latency benefit and answer quality still require measurement.

The experimental `fixed-part-selection-v21` uses a fixed unit/part-order integer role matrix: 0 for none, 1 for evidence, 2 for qualifier, and 3 for both, with a separate request-context boolean vector. Native enum schemas enforce the available roles; strict decoding rejects booleans, nonintegers, invalid values and obsolete wires. V19 independent review, evidence/scoped-basis semantics, exact source/context/dependency presentation, caps, profile, sampler and defaults remain unchanged. Native speed and answer quality remain unmeasured until evaluation.

The experimental `fixed-part-selection-v22` adds subject-boundary guidance to V21; its schema, evidence rendering, caps, profile and defaults remain unchanged. Its semantic quality remains unqualified.

The experimental `fixed-part-selection-v23` makes one measured independent-review request, with a 512-token output limit, for supplied question parts. It begins with an explicitly application-authored empty inventory that makes no relevance assertion and is not a model reply. Its strict trace records only the actual review reply and measurement; replay reconstructs the authored state. Selection favors directly responsive source rules while retaining governing prerequisites, exceptions and necessary context. Exact source rendering, profile, resources, older modes and defaults are preserved. Native answer quality and timing require separate qualification.

The experimental `fixed-part-selection-v24` omits the empty application inventory from the model prompt and requests evidence/context before checks/verdicts. It retains a recomputed application-owned inventory in its trace, with one actual 512-token reply and measured input. Selection guidance favors reusable standalone rules and explicit field mappings while retaining mandatory qualifications. Exact source rendering, older contracts, profile/resources and defaults are preserved. Native answer qualification remains separate from local checks.

Experimental `fixed-part-selection-v28` uses two planned source-only calls: a proposal and a necessity audit, each capped at 256 output tokens. Both use the full reviewed catalog and supplied question parts. The final answer quotes exact source evidence with declared dependencies; optional background stays available for inspection. Both native replies and measured input receipts are preserved and checked on reopen/export. The audit can still omit or retain incorrect material, so human factual and presentation review remains necessary. This mode is opt-in and unqualified; current defaults are unchanged.

Experimental `fixed-part-selection-v29` retains the two planned 256-token source-selection calls and measured receipts. A final answer where every requested detail is unknown must retain visible relevant source context or fail closed; the first proposal may be repaired by the planned review. The prompts ask the model to follow explicit references for required qualifications. These checks do not establish relevance, factual completeness or correctness, which still require review. The mode is opt-in and unqualified; defaults are unchanged.

Experimental `fixed-part-selection-v30` gives the second planned source-selection pass the exact unverified visible proposal beside the full reviewed catalog, so it can audit both necessary facts and presentation. The review instructions are compacted; both256-token replies and measured inputs remain mandatory. Source scope and dependency guards remain structural, not proof of relevance or correctness. This opt-in mode is unqualified; defaults are unchanged.

Experimental `fixed-part-selection-v31` uses a source proposal followed by explicit source-root drop/add edits and a separate final verdict. Both planned native replies and their distinct measured schemas are replay-bound. The applied selection is application-derived, not a third model reply. Declared source dependencies and wholly unknown scope anchors remain enforced. Edits and source citations do not prove relevance, factual completeness or presentation quality; review remains required. Two 256-token calls, opt-in and unqualified; defaults are unchanged.

Experimental `fixed-part-selection-v32` uses two actual source-selection replies with the V28 model-input wire. Application contracts and replay remain V32. Necessity guidance is focused on requested precise values, wholly unknown selected-source scope, and explicit source references; full source catalogs and dependencies remain available. Both 256-token inputs, model-wire identity and review focuses are measured and replay-bound. Strict final validation remains; factual correctness and presentation need review. Opt-in and unqualified; defaults unchanged.
