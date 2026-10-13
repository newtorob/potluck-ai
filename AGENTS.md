# Potluck agent instructions

This file is the entry point for AI coding assistants (Claude Code, Cursor, Copilot, Codex, Aider and others) working on Potluck. Human contributors: see [CONTRIBUTING.md](CONTRIBUTING.md).

## Policy for AI-assisted contributions

- **The human submitter is responsible** for reviewing, testing and understanding every line of generated code.
- **No `Co-Authored-By` trailers for AI tools.** The person who submits the change owns it.
- **Check that what you reference exists.** APIs, flags, make targets and file paths must be in the current tree.

## Where to read

| Path | When to read |
| --- | --- |
| [README.md](README.md) | Map of the repository and how the parts fit together |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Set up, commit conventions, tests, coding guidelines |
| [apps/desktop/python/README.md](apps/desktop/python/README.md) | Working on the engine |
| [apps/desktop/python/potluck_local/mcp/README.md](apps/desktop/python/potluck_local/mcp/README.md) | The MCP server and its tools |
| [apps/desktop/go/potluck-mesh/README.md](apps/desktop/go/potluck-mesh/README.md) | Working on the mesh sidecar |
| [apps/cli/README.md](apps/cli/README.md) | Working on the CLI |
| [services/coordinator/README.md](services/coordinator/README.md) | Working on the coordinator |
| [docs/phase-g/mesh-design.md](docs/phase-g/mesh-design.md) | Mesh design: identity, pairing, connections, relay |
| [docs/security/threat-model.md](docs/security/threat-model.md) | What the system protects against, and what is planned |
| [packaging/README.md](packaging/README.md) | Installers and release builds |

## Quick reference

- **Build and run**: `make setup` once, then `make dev`. Ask before running long builds such as `make installer-*`.
- **Tests**: run the suite for the part you changed (`make test-sidecar`, `make test-go`, `make test-coordinator`, `make test-api`, `make test-frontend`, `make test-cli`), then `make test-fast`.
- **Privacy lint**: `make lint` must pass. Coordinator code must never put a per-peer identity into a message sent to a peer or into a public aggregate response. Contributor and relay code must never write someone else's prompt or response content to disk, logs or telemetry. If the lint flags a change, fix the code.
- **Local engine port**: the engine listens on port 8321. A test engine must use another port (`POTLUCK_PORT`) so it does not take traffic from a running app.
- **Comments**: explain why, in the present tense. No development history, dates, ticket ids or names in comments.
- **Docs with code**: when you change behavior a user can see, update the matching doc in the same change.
- **Benchmarks**: never state a number that is not backed by a run in `bench/results/`, and state the conditions it was taken in.
- **Platforms**: the app runs on macOS, Linux and Windows. Say which ones a change was tested on.
