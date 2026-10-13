<h1 align="center">
  <img width="140" src="apps/desktop/electron/icons/icon-512.png" alt="Potluck"><br>
  Potluck
</h1>

<p align="center">
  <a href="https://trypotluck.ai">Website</a> ·
  <a href="docs/install.md">Install</a> ·
  <a href="apps/cli/README.md">CLI</a> ·
  <a href="docs/inference-benchmarks.md">Benchmarks</a> ·
  <a href="docs/security/threat-model.md">Security model</a>
</p>

**Potluck** is local-first AI that runs on the machines you already have. A desktop app and a CLI run open models on your own computer. When a request needs more than one machine can give, a mesh connects it to others, and you choose how far each request may travel.

- **Local by default**: models run on your computer with llama.cpp. A request leaves the machine only when you pick a wider scope.
- **Your machines work together**: a WireGuard mesh joins the computers you own, so a laptop can use the GPU in another room.
- **Wider when you want it**: share with a trusted circle of people you know, or use the open pool. The scope is set per request.
- **Works with your tools**: an OpenAI- and Anthropic-compatible API on localhost, an MCP server with persistent memory, and a terminal coding agent.

## Guided tour

Every chat shows where it will run, and you can change it before you send.

![The chat routing selector showing Local only, My machines, Trusted circle and Cooperative network](docs/images/getting-started/06-chat-scopes.png)

## Quickstart

### Desktop app

Download the app for macOS, Windows or Linux from [trypotluck.ai](https://trypotluck.ai). [docs/install.md](docs/install.md) walks through the first run: picking a model that fits your hardware, signing in, and adding a second machine.

### Command line

```bash
brew install newtorob/potluck/potluck

potluck setup            # set up the local engine, account and connections
potluck                  # start the interactive agent
potluck run "prompt"     # plain inference, with optional piped stdin
potluck status           # runtime, connection and models
```

The full command reference is in [apps/cli/README.md](apps/cli/README.md).

### API

The engine's API is at `http://127.0.0.1:8321`. It serves OpenAI-style routes (`/v1/chat/completions`, `/v1/embeddings`, `/v1/models`) and Anthropic Messages (`/v1/messages`), so existing clients work by pointing their base URL at it. Requests follow the same routing scopes as the app. See the gateway section of [apps/cli/README.md](apps/cli/README.md) for enabling the gateway and its key.

## Features

- **Local inference** through llama.cpp, with GPU support on Apple Silicon, NVIDIA and AMD cards.
- **Routing scopes**: this machine, your own machines, a trusted circle, or the open pool.
- **Mesh networking**: WireGuard tunnels between machines, direct on a LAN when possible, through a relay when not.
- **One model across several machines**: `potluck split` runs a model that is too large for any single computer you own. See [docs/managed-split-models.md](docs/managed-split-models.md).
- **Memory and MCP**: a local memory store with an MCP server, so coding agents keep context between sessions. Memory stays on your machine.
- **Terminal agent**: `potluck` edits code, runs batch jobs across your computers, and shows usage and model speed.
- **Measured, not assumed**: models are checked on your hardware before they serve requests, and [benchmarks](docs/inference-benchmarks.md) state the conditions each number was taken in.

## How it fits together

| Path | What it is |
| --- | --- |
| `apps/desktop/electron` | Electron shell: the window, updates, and starting the engine and mesh sidecar |
| `apps/desktop/python` | The engine: a FastAPI service that runs models, serves the API, and holds local memory and the MCP server |
| `apps/desktop/frontend` | The React UI rendered inside Electron |
| `apps/desktop/go/potluck-mesh` | The mesh sidecar: WireGuard tunnels between machines, with a relay fallback |
| `apps/cli` | `potluck`, the terminal agent |
| `services/coordinator` | Coordinator: peer registry, attestation, job routing, credit ledger, mesh control plane |
| `services/api` | Cloud API: member accounts and membership. It never handles prompts |
| `services/derp` | Relay used when two machines cannot reach each other directly |
| `web/landing` | The website |
| `packaging`, `scripts` | Installers, service definitions, setup and release scripts |
| `bench` | Benchmark runners and raw results |
| `docs` | Install guide, design notes, security model, benchmarks |

## Build from source

You need Python 3.11 or newer, Node 20 or newer, pnpm, Go, and Docker. Everything is driven from the root `Makefile`. Run `make` with no arguments to list every target.

```bash
make setup    # one time: Python venv and node installs
make dev      # backend in Docker, desktop app natively, in one terminal
```

`make dev` starts Postgres, migrations, the cloud API and the coordinator in Docker, then launches the desktop app (Vite, the engine, Electron) in the foreground. Ctrl-C stops the desktop app and leaves the backend running. `make down` stops the backend.

The desktop app runs natively because the engine needs the GPU and Electron needs a real window. Only the backend runs in containers.

```bash
make test        # every local suite
make test-fast   # privacy lint and the three node suites with typechecks
```

## Resources

- [docs/install.md](docs/install.md): installing and first run
- [apps/cli/README.md](apps/cli/README.md): the CLI
- [docs/phase-g/mesh-design.md](docs/phase-g/mesh-design.md): how the mesh works
- [docs/security/threat-model.md](docs/security/threat-model.md): what the system protects against and what it does not
- [docs/benchmarks.md](docs/benchmarks.md) and [docs/inference-benchmarks.md](docs/inference-benchmarks.md): methods and measured results
- [docs/architecture](docs/architecture): design notes for individual subsystems

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development loop and [AGENTS.md](AGENTS.md) if you work with an AI coding assistant. To report a vulnerability, follow [SECURITY.md](SECURITY.md) and do not open a public issue.

## License

MIT. See [LICENSE](LICENSE). Vendored third-party files keep their own licenses, which sit next to them.

## Acknowledgements

Potluck builds on work from the open-source community:

- [llama.cpp](https://github.com/ggerganov/llama.cpp) and [llama-cpp-python](https://github.com/abetlen/llama-cpp-python) for inference
- [WireGuard](https://www.wireguard.com/), [wireguard-go](https://git.zx2c4.com/wireguard-go/) and [Wintun](https://www.wintun.net/) for the mesh tunnels
- [Tailscale](https://tailscale.com/)'s DERP, which the relay is modeled on
- [LongMemEval](https://github.com/xiaowu0162/LongMemEval) for the retrieval benchmark data
- The [Model Context Protocol](https://modelcontextprotocol.io/) for agent integration
