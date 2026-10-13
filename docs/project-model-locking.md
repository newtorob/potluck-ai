# Project model locks

A project can commit `potluck.lock` to select an exact GGUF artifact. The lock binds a model ID to the SHA-256 of its bytes, which also fixes its quantization. CLI/runtime upgrades, catalog refreshes, global model preferences, and a different model loaded in the desktop app cannot change that selection. A mismatch stops inference with an actionable error.

## Pin a project

Install the model and wait for its download to finish, then run from the project:

```sh
potluck models pin qwen3-1.7b-q4
potluck models lock
```

`pin` hashes the installed file through the authenticated local runtime. It creates `potluck.lock` at the Git root, or the current directory outside Git. From a subdirectory, the nearest existing lock applies. Discovery stops at a Git worktree root or the home boundary, so a nested repository does not inherit a parent repository's lock. Commit the file alongside your project.

The file contains no credentials, hostnames, local filesystem paths, or routing permissions:

```json
{
  "schemaVersion": 1,
  "model": {
    "id": "qwen3-1.7b-q4",
    "format": "gguf",
    "sha256": "b139949c5bd74937ad8ed8c8cf3d9ffb1e99c866c823204dc42c0d91fa181897",
    "sizeBytes": 1107409472,
    "source": {
      "url": "https://huggingface.co/unsloth/Qwen3-1.7B-GGUF/resolve/main/Qwen3-1.7B-Q4_K_M.gguf"
    }
  }
}
```

The checksum is authoritative. A source URL may contain a mutable revision such as `main`; if the publisher replaces those bytes, installation fails its checksum check instead of updating the lock. A full commit revision in the URL is also supported. The lock does not guarantee continued upstream availability.

For an imported file, or installed bytes that differ from the current catalog, pinning still works offline but omits the download source. Copy the same GGUF to the other machine's Potluck model directory, under the locked ID, and run `sync` to verify it. Pinning never trusts the checksum sidecar as proof of the actual file.

## Use it on another machine

```sh
potluck models sync
# If downloading, check progress, then run sync again to verify completion:
potluck models progress qwen3-1.7b-q4
potluck models sync
potluck models load qwen3-1.7b-q4
potluck --scope local -p "Explain this project"
```

`sync` downloads from the saved source and verifies the saved checksum, independently of the current catalog. Existing weights are hashed before being reported as verified. A conflicting file under the same model ID is preserved and reported; move it aside deliberately before syncing. Downloads use the existing resumable downloader and never promote a partial file before checksum validation. A `downloading` result is not a verification result.

Normal model management remains machine-wide. `load` deliberately loads a model on that machine; it does not rewrite the project lock. The agent selects the lock before the command line/global/app defaults, rejects a conflicting `--model` or `/model`, and retains that pin for the session. An explicit lock change applies when starting a new agent session.

## Change the pin intentionally

```sh
potluck models unpin
potluck models pin <installed-model-id>
```

Review and commit the lockfile diff. Repeating `pin` for the same ID leaves an existing lock unchanged, even if newer bytes are installed. Broken or unknown-version locks fail closed and can be explicitly removed with `unpin`.

## Routing and compatibility

- `local` requires the exact model ID and digest on this machine.
- `mine` may use a household machine advertising model-lock protocol version 1 and the same ID and digest. Every retry keeps the digest constraint. A same-ID peer with different weights or an older runtime is ineligible.
- Pinned requests require an explicit `local` or `mine` scope. Circle/open-pool, transformers/pipeline, and RPC-distributed backends currently cannot enforce this GGUF contract and are rejected.
- CPU/GPU and macOS/Linux can use the same GGUF lock when the backend supports that file. A lock fixes weights, not runtime versions, sampling settings, numerical precision, hardware capacity, or identical output across architectures.
- The CLI checks runtime support before sending project content, and checks the response acknowledgment before accepting output. Upgrade both CLI and serving runtimes for locking.

The loaded GGUF is hashed once at load, off the event loop, then its file identity is checked on locked requests. This adds one sequential file read to loading. The runtime rechecks the model ID/digest after acquiring inference access, preventing a queued request from silently using a model swapped in after route selection. Unsupported/unverifiable identities fail closed. This protects against accidental drift among trusted household machines; it is not remote attestation against a malicious host.

## API contract

`GET /v1/potluck/capabilities` advertises `model_lock_version: 1`. Locked chat and legacy completion requests include `model` and `potluck_model_sha256`, plus `X-Potluck-Scope: local|mine`. Responses acknowledge enforcement with `X-Potluck-Model-Lock: 1` and `X-Potluck-Model-SHA256`. An acknowledgment is not successful completion: clients must still handle errors within SSE streams.

Local ID mismatches return HTTP 409, even without a digest. A mismatch discovered after streaming starts is an SSE error before inference output. Model management exposes authenticated `GET /models/{id}/identity` and `POST /models/{id}/install-locked`; neither requires gateway access or changes contribution settings.

## Testing

Run the CLI tests and Python `test_project_model_lock.py` with an isolated Potluck data directory. To exercise an actual model and runtime restart without changing installed weights or services:

```sh
POTLUCK_RUNTIME_BIN=/absolute/path/to/potluck-local \
POTLUCK_VALIDATION_MODEL_FILE=/absolute/path/to/existing/model.gguf \
node apps/cli/scripts/validate-model-lock.cjs
```

The driver uses private temporary state, a random loopback port, CPU inference, and read-only use of existing weights. It reports whether its isolated runtime shut down cleanly.
