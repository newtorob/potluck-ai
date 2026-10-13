# Potluck CLI 0.1.10

Set up your local AI runtime, manage models, inspect connections, and use a
terminal coding agent powered by Potluck.

## Install

Supported downloads: macOS 15 or newer on Apple Silicon, and Linux x86-64 with
glibc 2.35 or newer (such as Ubuntu 22.04+). Node.js 20 or newer is required.
The archive includes the Python runtime; no separate Python
installation or desktop app is required. Models are downloaded separately.

Homebrew installs the CLI and Node:

```sh
brew install newtorob/potluck/potluck
```

For a standalone archive, verify its SHA-256 using the adjacent `.sha256` file,
extract it, and run this from the extracted directory:

```sh
node install.mjs
export PATH="$HOME/.local/bin:$PATH"
potluck --version
```

Use `node install.mjs --prefix /absolute/path` for a custom installation.
Install as your own user, without sudo. Add the installation's `bin` directory
to your shell's PATH for future terminals.

## First local run

```sh
potluck setup
```

Setup recommends a model that fits your machine, asks before downloading,
and tests a local response. For unattended setup, choose the model explicitly
with `potluck setup --no-input --model <model-id>`.
`potluck setup --no-input` alone prepares the runtime without downloading weights.

Enable local API access and send a prompt:

```sh
potluck gateway enable
potluck run "Say hello" --scope local
potluck doctor --scope local
```

`potluck gateway enable` enables authenticated local API access for the coding
agent and other tools. Setup without explicit options does not enable sharing.
Use `potluck down` to stop an engine started by the CLI. `potluck --help` lists
commands, and `potluck status --json` reports runtime and model readiness.

## Project models and terminal workflows

From your project directory, pin an installed model's exact weights:

```sh
potluck models pin <model-id>
potluck models sync
potluck run "Explain the purpose of a model lock"
cat notes.txt | potluck run "Summarize in one line"
potluck status --watch
```

Commit `potluck.lock` with your project. It records the model ID, SHA-256 and
file size. Compatible runtimes verify those weights and reject mismatches.
An upgrade preserves the lock and downloaded models. Different CPU/GPU backends
can still produce different output. Pinned inference supports local and
own-machine routes; circle and open-pool routes do not yet support pins.

Use `potluck launch aider` with an independently installed Aider, or run the
built-in coding agent with `potluck -p "Explain this repository"`.
Agent sessions can be saved explicitly with `--save-session` and resumed with
`--resume <id>` in the same project. Saved sessions require a model lock and
contain local, unencrypted conversation history. Inspect `potluck --help` for
session management and contribution schedules/resource limits.

## Coding-agent repairs

File feedback includes the current path and bounded code context. Repository maps
include Python and JavaScript/TypeScript declaration hints, and repair feedback
keeps the original request nearby. The agent can write literal code without
JSON-escaping the whole file. Whole-file overwrites require a complete read;
truncated reads and unreadable existing files do not authorize replacement.

## Coding-agent recovery

The agent validates tool arguments, recovers narrow JSON/Markdown framing errors,
and stops repeated failed edits or unchanged reads. Test failures stay available
while it works across files. Interrupted responses do not execute pending tools.

Configure a fixed check with `--verify "your-test-command"`. Four consecutive
verification failures stop the task as incomplete. A one-shot run exits with
status 1 for failed checks, exhausted turn budgets, or stalled actions, so scripts
can distinguish a completed task from a stopped one. Passing the configured
checks does not guarantee the generated code is correct; review changes.

## Usage dashboard

```sh
potluck usage
potluck usage models
potluck usage requests
potluck usage machines
potluck usage --window 30 --direction served
potluck usage --json
```

Explore requests, reported tokens, route totals and latency from your terminal.
The overview has larger headline numbers, average first-token and p95 total
latency, and a chart with UTC bucket labels. Use 1–4 for views, `w` for the time
window, `d` for usage/contribution, `t` for themes and `q` to quit.

Usage is recorded on this computer, not aggregated across the fleet. End-to-end
tokens/sec includes prefill and routing; it is not live decode speed. Missing
measurements stay unavailable. The dashboard does not enable network sharing.
Both CLI and runtime must be upgraded; restart a running runtime after upgrading.

## Household worker recovery

When a household worker becomes unreachable before output starts, the gateway
can retry another eligible computer with the same requested model and pin.
Once output has started, an interruption returns an error without silently
replaying the answer. Quiet requests use reachability probes to detect
disconnected workers sooner while preserving slow, healthy model generation.
You can issue a new request after connectivity returns.

## Trusted circles

A circle is a small group of people who let each other's machines answer their
requests. Sign in first with `potluck login`.

```sh
potluck circle create "Friends"
potluck circle invite "Friends"
potluck circle join ABCD-EFGH-JKMN
potluck circle list
potluck contribute share circles
potluck run "Say hello" --scope circle --circle "Friends"
```

`invite` prints a code that works once and expires in seven days. `join` shows
what joining means and joins only with `--yes`. `contribute share circles` lets
people in your circles send requests to this machine; it is never offered open
pool work. A circle request goes through the Potluck coordinator to one machine
in the circle, and that machine sees the conversation. `potluck circle leave`
and `potluck circle delete` need `--yes`.

## Background runtime

For Homebrew installations:

```sh
brew services start potluck
brew services stop potluck
```

For standalone installations:

```sh
potluck service install
potluck service status
potluck service uninstall
```

Stop a CLI-owned engine with `potluck down` before switching to a service.
Services start at user login; Linux requires a working systemd user session.
Uninstalling a service retains your accounts, models, and settings.

## Scope of this release

Local runtime, model management, diagnostics, gateway controls, user services
and account sign-in are available. `potluck login` starts the engine if needed
and opens your browser to approve this terminal; over SSH it prints a link to
open on any device. With desktop 0.1.7 or later on the same computer, the CLI
uses the app's engine and sign-in.
Household connection requires the separately installed Potluck mesh daemon and
a signed-in account. This download does not install that daemon or automatically
enable network sharing. Windows, Intel Mac, and Linux ARM64 archives are not
included in this release.

## Updates and support

Homebrew: `brew update && brew upgrade potluck`.
Standalone: install a newer archive, then rerun `potluck service install` if you
use its service. Previous versions are retained under the installation prefix.
Stop and uninstall services before removing binaries. Keep `~/.potluck` to
preserve your data.

Downloads and installation help: https://github.com/newtorob/homebrew-potluck
Product: https://trypotluck.ai
