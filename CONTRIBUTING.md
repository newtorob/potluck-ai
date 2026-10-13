# Contributing to Potluck

Thank you for your interest in contributing. This guide covers how to set up, build, test and submit changes.

## Table of contents

- [Getting started](#getting-started)
  - [Prerequisites](#prerequisites)
  - [Setting up the development environment](#setting-up-the-development-environment)
- [Contributing](#contributing)
  - [Submitting an issue](#submitting-an-issue)
  - [Development workflow](#development-workflow)
- [Coding guidelines](#coding-guidelines)
- [AI coding assistants](#ai-coding-assistants)
- [Testing](#testing)
- [Documentation](#documentation)
- [Reporting a vulnerability](#reporting-a-vulnerability)

## Getting started

### Prerequisites

- Python 3.11 or newer
- Node 20 or newer, with pnpm
- Go
- Docker, for the backend (Postgres, the cloud API and the coordinator)

The desktop app runs natively because the engine needs the GPU and Electron needs a real window. Only the backend runs in containers.

### Setting up the development environment

```bash
make setup          # Python venv for the engine, node installs for the UI and Electron
make install-hooks  # pre-commit hook that runs the privacy lint
make dev            # backend in Docker, desktop app natively
```

The CLI and the website keep their own dependencies:

```bash
cd apps/cli && pnpm install
cd web/landing && pnpm install
```

Run `make` with no arguments to list every target. The [README](README.md) has a map of the repository.

## Contributing

### Submitting an issue

Before you open an issue, search the tracker for an existing one. A good bug report says which version or commit you ran, your operating system and hardware (CPU, GPU, RAM), what you did, what you expected, and what happened, with logs if you have them.

Do not file security problems as issues. See [SECURITY.md](SECURITY.md).

### Development workflow

#### Commit messages

- Use [conventional commits](https://www.conventionalcommits.org/en/v1.0.0/) with a scope, for example `fix(gateway): return 400 when the prompt is too long`.
- Keep the subject short and in the imperative.
- Use the body to explain why the change was made when the subject does not say it.

#### Creating a pull request

For a large feature or a change in behavior, open an issue first so the approach can be discussed. Contributions are accepted under the project's [MIT License](LICENSE).

1. Fork the repository and create a branch with a descriptive name.
2. Make your change in focused commits.
3. Run the tests for the part you changed (see [Testing](#testing)).
4. Open a pull request against `main`.
5. In the description, say what the change does and why, how you tested it and on which platform, and any breaking change. Potluck runs on macOS, Linux and Windows, and GPU behavior differs between them.

## Coding guidelines

### General principles

- Keep a change to one topic. Leave out unrelated refactors and formatting.
- Add or update a test for any behavior you change.
- Match the code around your change: naming, structure and comment density.
- Comments explain why the code has to be the way it is. Leave out the history of how it got there; that belongs in the commit message.

### Python (engine, coordinator, API)

- Target Python 3.11. Both `ruff` (line length 100) and `mypy` in strict mode are configured in each `pyproject.toml`.
- Add new dependencies to the service's `requirements.txt`.

### TypeScript (UI, CLI, website)

- TypeScript runs in strict mode. The typecheck must pass: `make test-frontend`, `make test-cli` and `make test-landing` each run it.
- The UI has `eslint` and `prettier` set up in `apps/desktop/frontend`.

### Go (mesh sidecar, relay)

- Format with `gofmt` and run `go vet ./...`.

### The privacy lint

`make lint` runs `scripts/dev/lint_privacy.py`. It fails when coordinator code could put a per-peer identity (member id, peer id, node name, mesh address, hostname) into a message sent to a peer or into a public aggregate response, and when contributor or relay code could write someone else's prompt or response content to disk, logs or telemetry. The pre-commit hook runs the same check.

If the lint flags your change, fix the code. Extend the allow list in the script only when you can explain in the pull request why the flagged value is safe.

## AI coding assistants

You may use an AI coding assistant to work on Potluck. The rules are the same as for any other change:

- **You are responsible for the change.** Review, test and understand every line before you submit it. Check that every API, flag and file path the assistant refers to exists in the tree.
- **Do not add `Co-Authored-By` trailers for AI tools.** The person who submits the change owns it.
- Reviewers may ask about any part of a change regardless of how it was produced.

[AGENTS.md](AGENTS.md) is the entry point for assistants working in this repository.

## Testing

Run the suite for the part you changed, then the fast subset before you open a pull request.

```bash
make test-sidecar      # engine (pytest)
make test-go           # mesh sidecar and relay
make test-coordinator  # coordinator (pytest)
make test-api          # cloud API, in a throwaway container
make test-frontend     # UI tests and typecheck
make test-cli          # CLI tests and typecheck
make test-landing      # website tests, typecheck and build
make test-fast         # privacy lint and the three node suites
make test              # everything
```

## Documentation

When you change behavior a user can see (a CLI command or flag, an API route, a setting, a screen), update the matching doc in the same pull request: [docs/install.md](docs/install.md), [apps/cli/README.md](apps/cli/README.md), or the README of the component you changed.

Benchmark numbers in `docs/` must come from a run whose raw results are in `bench/results/`, and must say what hardware and conditions they were taken in.

## Reporting a vulnerability

Do not open a public issue. See [SECURITY.md](SECURITY.md).
