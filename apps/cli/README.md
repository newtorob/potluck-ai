# @potluck/cli

A terminal interface to Potluck, with setup, account, household, model, gateway,
and diagnostic commands. `potluck` also includes a coding-agent REPL whose
inference runs on **your own machines across the Potluck mesh**, never a third
party. It talks to the Potluck sidecar's local OpenAI-compatible gateway
(`http://127.0.0.1:8321/v1`), so the model, the routing, and the data all stay
inside your network.

The gateway exposes plain chat completions with **no function-calling**, so the
agent loop is prompt-based (ReAct): the model emits one fenced action at a time,
the CLI runs it, and feeds the result back. Actions normally use JSON; complete
file writes can also carry literal code without JSON string escaping.

## Requirements

- For the coding agent: a running Potluck gateway (desktop or headless).
- The gateway enabled: `potluck gateway enable` or **Settings → Connect tools** (this writes
  `gateway.enabled` + `gateway.api_key` to `~/.potluck/config.json`).
- Node.js ≥ 20.

`status`, `doctor`, `logs`, and command help work without the desktop app or a
running model. On macOS/Linux, setup and member login use the shared local engine.
Household connection requires the existing `potluck-mesh` daemon. Standalone
archives bundle the runtime, and `potluck service` manages a user service.
Install with `brew install newtorob/potluck/potluck` or use the standalone
downloads linked in the [public tap](https://github.com/newtorob/homebrew-potluck).
See [download and first-run instructions](packaging/DOWNLOAD.md).

## Install

A native archive includes the CLI and Python runtime; it does not require Python,
a source checkout, or the desktop app. Install Node.js 20 or newer, verify the
archive's SHA-256 against the release checksum, extract it, then run:

```bash
node potluck-cli-<version>-<platform>-<arch>/install.mjs
export PATH="$HOME/.local/bin:$PATH"
potluck setup --no-input
```

Use `--prefix /absolute/path` for a custom installation. Never use sudo. Existing
versions are retained for rollback. After upgrading, rerun `potluck service
install` to update its executable paths.

For development from this repository:

```bash
cd apps/cli
pnpm install
pnpm build
node dist/index.js --help
```

To use the `potluck` command globally, link the built package:

```bash
pnpm link --global   # then: potluck
```

## Project instructions

The built-in coding agent automatically reads `AGENTS.md` from the repository
root through the current working directory, in that order. A more specific file
applies within its directory subtree. Discovery stops at the nearest `.git`
directory or worktree marker; outside a repository, only the working directory's
`AGENTS.md` is loaded. Files above that boundary, siblings and descendants are
not loaded automatically. The agent is instructed to inspect additional
descendant `AGENTS.md` files before editing there; that discovery depends on the
model following the instruction.

The CLI names the loaded files before sending a task. It rereads them before
each new REPL task and `/snapshot`, on `/clear`, and when starting or resuming a
one-shot run. Changed instructions replace the previous copy. Saved sessions
reload current files instead of restoring old system instructions. A failed
refresh stops the next task; it never silently reuses stale rules.

Instruction files must be regular UTF-8 files, not symlinks. Each file is limited
to 8 KiB; the combined message, including source labels, is limited to 16 KiB.
Oversized, unreadable or invalid files stop the task with a correction message.
Instructions are never silently truncated or dropped to fit the model. If they
leave insufficient context for the request, shorten the applicable `AGENTS.md`
files or load a model with a larger context window.

The user's explicit request takes precedence over project guidance. Project
files cannot bypass edit/command approval, workspace containment, the fixed
verification command, routing scope or the project's exact model pin.

## Project model pinning

`potluck models pin <installed-model-id>` creates a portable `potluck.lock` with
that GGUF's actual SHA-256. Commit it with your project. On another machine,
`potluck models sync` installs/verifies the same bytes; then load the model.
`potluck models lock` shows the pin and `potluck models unpin` explicitly removes it.
The agent enforces the lock across local and household routing, including retries.
Older runtimes and mismatched model files fail clearly instead of substituting.
See [project model locking](../../docs/project-model-locking.md) for commands,
download behavior, and supported backends.

## Usage

Managed split models run one pinned GGUF across explicitly selected owned machines
on Mac and Linux: `potluck split worker on`,
`potluck split plan|start <pinned-model> --peer <name>`, and
`potluck split status|stop`. See [setup, limits and recovery](../../docs/managed-split-models.md).

```bash
potluck                       # start the REPL (project lock, then global/app defaults)
potluck --model llama-3.1-8b  # select a model for this session (must match project lock)
potluck --scope local         # route only to this machine (default: mine)
potluck --verify "pnpm build" # run this after each edit; feed failures back to fix
potluck --no-verify           # disable the post-edit verify step
potluck -p "fix the build"    # one-shot: run a single task and exit
potluck -p "rename foo" --yes # one-shot, auto-approving edits/writes/commands
potluck setup                 # resume engine/account/gateway/household setup
potluck setup --preferences   # configure coding-agent defaults
potluck login                 # opens your browser to approve; prints a link over SSH
potluck whoami                # stored member profile, no credentials
potluck logout                # revoke this session and sign out locally
potluck household create      # idempotently create your household
potluck household list        # your household and machines
potluck connect --name server # connect this machine to your signed-in household
potluck disconnect            # remove this machine's local mesh enrollment
potluck up                    # start the local engine, or attach to the existing one
potluck down                  # gracefully stop a CLI-owned engine
potluck service install       # start a background engine and enable it at login
potluck service status        # OS service status; use status for engine readiness
potluck service restart       # restart the background engine
potluck service stop          # stop the background engine
potluck service uninstall     # remove startup service, retain your data
potluck serve                 # foreground engine for a process supervisor
potluck gateway enable        # enable gateway access, preserving an existing key
potluck models recommend      # up to three picks for this machine, with reasons
potluck models recommend --use coding  # tailor picks to the work you plan to do
potluck models list           # catalog and installed models
potluck models install <id>   # start/resume a verified download
potluck models progress <id>  # check download progress
potluck models load <id>      # load on this computer
potluck models test           # run the engine's answer check on the loaded model
potluck models autoload      # show the saved startup model and toggle
potluck models autoload off  # start without a model (on re-enables restore)
potluck benchmark             # show and measure the model this machine runs
potluck benchmark --model <id> --yes  # switch to another model, measure it, switch back
potluck status                # runtime, mesh, gateway and model readiness
potluck doctor --scope local  # diagnose this machine without requiring the mesh
potluck doctor --json         # structured checks and suggested actions
potluck logs --source mesh    # bounded, credential-redacted log tail
potluck --help
```

Inside the REPL:

```
you ▸ find every call to renderAction and add a unit test for it
```

In-session commands:

| Command       | Effect                                  |
| ------------- | --------------------------------------- |
| `/model`      | List available models                   |
| `/model <id>` | Switch model for the next turns         |
| `/scope`      | Show the routing scope                   |
| `/scope <s>`  | Switch scope (`local` \| `mine`)        |
| `/verify`     | Run the verify command now              |
| `/help`       | Show help                               |
| `/exit`       | Quit                                    |

`Ctrl-C` cancels the current turn cleanly; pressing it at an idle prompt quits.

The agent validates tool names and argument types before execution. A completed
fenced action missing only its final outer `}` can be recovered without changing
its arguments. Complete action JSON may also omit the closing Markdown fence;
these repairs are never combined. Unfinished strings and inner objects are
rejected with specific feedback. Before executing an action or accepting a final
answer, the coding agent requires an explicit successful finish and the stream's
completion marker. A token-limit cutoff, filtered response, unsupported tool-call
ending or missing completion cannot authorize an edit, even when the action JSON
looks complete. Narrow the task or use a larger loaded context when output is cut
off. Remote replies must report their actual finish status; if an older peer or
an engine cannot provide it, the coding agent stops without executing that
reply. Update Potluck on the serving machine or use a model loaded locally.
Use an updated CLI and local runtime together; older runtimes may report a
remote reply as complete without preserving the model's actual finish status.
Recovery preserves the usual edit/command approval gate.

Literal file writes use a strict JSON path header and an unescaped body:

````text
```write_file {"path":"src/math.py"}
def add(a, b):
    return a + b
```
````

The matching closing fence is required. The body, including its final newline,
is the complete file. Use a longer outer fence when the body contains backtick
fences, or use the existing JSON action format when exact content has no final
newline. Nested code/action examples are file contents, not additional tools.
Existing files must have been read completely before a whole-file overwrite;
truncated reads and unreadable files do not satisfy that guard. Approval and
workspace containment apply to both formats.

`read_file` accepts a one-based `offset` and a line `limit` (default 200,
maximum 2,000). Output is capped at 16,000 characters. Partial reads show where
to continue and permit targeted edits, but never grant permission to replace
the entire file. A single oversized line is marked as truncated and needs a
bounded character read through an approved shell command.

Before each task, the CLI checks the loaded context window for the selected
model and `local` or `mine` route. It reserves output space and uses a conservative
UTF-8 byte estimate plus template overhead for history. This is not an exact
tokenizer count. Older engines without window metadata use a clearly announced
4,096-token fallback. A model reload or household failover can change capacity
after the check; the gateway still rejects a request that exceeds the new window.
Instructions and the current task are kept intact; older exchanges and oversized
tool feedback may be shortened with explicit notices. If the required content
cannot fit, the task stops incomplete and asks for a larger loaded context or a
smaller task. Compacted file contents must be read again before a whole-file rewrite.

The initial repository map includes bounded Python and JavaScript/TypeScript
declaration hints (names and line numbers), within a 512-byte initial map. These are a heuristic to locate the
owning file; the agent still needs to read it before editing. Ignored files,
symlinked entries and binary contents are skipped. The map is omitted when the
loaded window is unknown or at most 4,096 tokens, leaving room for actual tool
feedback; file search remains available.

Repeated unchanged reads, failed edits, and same-content writes receive recovery
feedback, then stop after three identical action/result pairs without a successful
change. Intervening reads do not reset a failed-edit loop. Actual edits reset the
progress check. Read feedback includes the file path; successful edits and
failed matches/no-op edits include a bounded excerpt of the actual current file.
This lets the agent inspect placement and repair stale or wrong-file edits without
guessing another target. It does not automatically redirect or apply failed edits.
Failed verification stays in subsequent tool feedback, and an
early final answer gets a chance to repair within the original turn budget.
During repair, a bounded excerpt of the original request is also placed beside
the latest feedback, consuming the existing context budget. Passing configured
checks is necessary but does not establish complete task correctness.
When verification is configured but no check has run, a final response triggers
the fixed check even if the model made no edits. An executed shell command makes
any earlier check result out of date, including when the command exits with an
error. The CLI runs the fixed check again before accepting a final response.
Explaining a repair does not bypass verification.
After four consecutive verification failures the task stops incomplete, before
further unverified edits; the fixed verify command and existing limits still apply.

One-shot agent runs (`-p`) exit with status 1 when they exhaust the step limit,
stop after repeated tool failures, or finish with automatic verification still
failing. A model saying “done” does not override a failed check. Saved sessions
remain available for follow-up work. Status 0 means the loop reached a final
response without these failures; it does not prove correctness beyond the
checks you configured.

### Terminal usage dashboard

`potluck usage` opens a live Hearth-styled dashboard with Overview, Models,
Requests and Machines views. Switch windows and usage/contribution, pause,
scroll history and change themes with the keyboard. Use `--once` for a snapshot
or `--json --watch` for NDJSON. It requires a runtime that includes the dashboard
endpoint; an older running runtime shows an unavailable screen with upgrade guidance.
See [controls and metric definitions](../../docs/cli-usage-dashboard.md).

### Diagnostics and logs

`status` and `doctor` probe the local runtime, mesh IPC and the configured gateway
concurrently. Gateway checks authenticate with the gateway key and list models;
they do not run inference or modify settings. `doctor` includes recovery actions.
Scope comes from `--scope local|mine` or CLI preferences. Mesh problems are warnings
for local-only checks. Member login is not inferred from a successful mesh probe.

Use `--json` for one JSON document with `schemaVersion: 1`. Diagnostics exit with
`0` when checks pass, `1` when attention is needed, and `2` for invalid usage.
`--timeout <milliseconds>` bounds each probe (default 3000, maximum 30000).
An advertised model is not a guarantee that the next request will have capacity.

`logs --source runtime|mesh|app --lines 100` reads at most 256 KiB and 1000 lines,
redacts known gateway credentials and common secret fields, and strips terminal
escape sequences. It does not follow the file. Missing files/permission errors
are reported explicitly. Foreground and system-managed services may log to their
terminal or OS journal instead. Review logs before sharing: redaction cannot
identify every kind of sensitive content.

`POTLUCK_DATA_DIR` selects an isolated data directory for configuration and logs,
matching the Python runtime. `POTLUCK_SIDECAR_URL` selects a literal loopback HTTP
origin for health checks; otherwise `POTLUCK_PYTHON_PORT` or port 8321 is used.
`POTLUCK_LOCAL_API_SOCKET` overrides mesh IPC. Defaults match the app's Unix socket
or Windows named pipe, including the fixed Linux system-service socket.

Unknown commands/options fail rather than starting the agent. Non-interactive
invocations must use an operational command or `-p`; interactive setup and the
REPL exit with guidance instead of waiting for input on a pipe.

### Local runtime and models (macOS/Linux)

`potluck up` starts the bundled `potluck-local` executable. In a
source checkout it can use `apps/desktop/python/.venv/bin/python`; build/install
that environment using the Python runtime's instructions first. An explicit
`POTLUCK_RUNTIME_BIN=/absolute/path/to/potluck-local` overrides discovery.
This command does not install runtime dependencies, mesh services, or models.

The engine writes a private control record in `~/.potluck/run` (0700 directory,
0600 file), locks that directory for its lifetime, and binds before publishing
credentials. Both clients authenticate the recorded instance. An existing
unmanaged runtime or unsafe permissions cause a clear error. The packaged app
can attach on port 8321 and leaves a CLI-owned engine running when it quits;
`down` refuses to stop an engine owned by the app or a service. Shared runtime
management is not available on Windows yet; use the desktop app there. On a
POSIX host,
`node apps/cli/scripts/validate-posix-runtime.cjs` checks the shared engine from
the repository: that the app's runtime discovery and session reader see the CLI's
engine and sign-in, that a pending device login and account settings survive an
engine restart, that logout revokes the server session, and that a clean shutdown
removes the discovery record. It uses temporary state and a loopback account
fixture, and stops its test engine afterward. Set `POTLUCK_RUNTIME_BIN` to run it
against a built bundle.

`up`/`down` use a 15-second default timeout; models use 30 seconds. All accept
`--timeout` up to 30000 milliseconds and `--json`. A timeout does not imply an
operation was cancelled: check status/logs before repeating it. `up` is safe to
repeat. `POTLUCK_SIDECAR_URL` can select a literal loopback HTTP origin for an
isolated instance; the desktop currently requires the default port. Set
`POTLUCK_GATEWAY_URL` separately for agent traffic on a custom port.

`gateway status|enable|disable|rotate` manages the separate gateway credential.
Enable preserves an existing key; rotate explicitly invalidates it. Commands
never print the key. Shared settings changes go through the engine, which saves
them atomically; CLI preferences remain in `cli.json`.

`models recommend [--use coding|writing|reasoning|general] [--json]` inspects this computer's CPU, GPU, available RAM and
model disk space, then shows up to three fitting models. Local answer checks and
benchmarks lead the ranking. Without that evidence, catalog tiers and GPU memory
headroom guide the choice; CPU-only machines start with smaller models. Apple
unified memory counts as one pool. Estimates do not promise speed or a particular
context length. The command does not download, load or change your selected model.
Run `potluck setup --model <id>` to install if needed, load and test a choice.
Guided setup asks what you plan to do: coding, writing and brainstorming, analysis
and reasoning, or general use. Enter/skip chooses general. `setup --use coding`
skips that question; unattended setup also needs `--with-model` or `--model`.
The choice applies to that setup or recommendation command. General is the default
when no use is supplied. Ordered catalog `use_cases` tags guide ranking within the
same evidence and hardware classes; they are editorial guidance, not measured task
scores or a guarantee of tool reliability. Untagged imports remain eligible as
fallbacks. Search still requires a search tool; choosing a model does not enable it.
Setup defaults to the first recommendation; an explicit model or project pin takes
precedence. The local runtime must be running (`potluck up`). These
picks describe this computer, even if coding-agent traffic uses another gateway.

`models list|install|load|unload|progress|cancel` uses the engine's existing model
APIs. Every action except list takes a catalog model ID. Install starts a
background download; poll progress before loading. Cancellation preserves
resumable download state. Loading is local; it does not enable contribution or
broaden routing scope. Successful local loads are remembered across engine
restarts. Restore runs in the background using the current context/GPU/thread
settings; `status`/`doctor` show loading or failed restoration. Explicit unload
or deletion clears the saved choice. RPC and pipeline loads are not restored
and clear any previous local startup choice. A missing or invalid model leaves
the engine running with no model loaded; use `models load <id>` to retry.

`models test [<model-id>] [--full] [--force] [--json]` runs the engine's answer check
on demand, on the loaded model unless one is named. After a model loads, the engine
looks at the answers it gives on this setup, falls back to the CPU when the GPU is
what gives wrong answers, and remembers the result. `models test` prints each check
with passed or failed and whether it was required, where it ran (GPU or CPU, and the
backend and device), the driver, and the engine's own sentence when there is one.
A stored result is reused unless you add `--force`; `--full` runs the longer set of
checks. Exit 0: passed (a run that fell back to the CPU still passes, and says so);
1: failed. `--timeout` can go up to 300000 here (default 120000). The JSON has
`schemaVersion`, `action`, `model`, `status`, `level`, `forced`, `durationMs`,
`outputTokens`, `acceptance` and `message`. An engine from before the check was added
answers the test but reports no check; the output says so.

`models autoload [on|off]` reads or changes the same preference as Settings →
Advanced → Startup. Turning it off keeps the saved model but skips loading on
the next engine start. Opening the app does not override the engine's choice.

With an installed runtime, terminal-only local use is:

```bash
potluck up
potluck gateway enable
potluck models list
potluck models install <model-id>
potluck models progress <model-id>  # wait for complete
potluck models load <model-id>
potluck doctor --scope local
potluck -p "explain this project" --scope local
```

### Background services (macOS/Linux)

`potluck service install` registers and starts a per-user launchd service on
macOS or a systemd user service on Linux. No sudo is used. The service starts at
login and restarts after a crash. On a headless Linux server, running through
logout/reboot requires an existing user manager with lingering enabled; this
command does not change the machine's login policy.

A service waits while the app or `potluck up` owns the engine, then takes over
when that engine exits. Opening or closing the app does not terminate a service
engine. To switch from `up`, run `potluck down`. Use `service stop` or `service
restart` for an engine owned by a service. SIGTERM drains the engine and removes
its private discovery record. An abrupt kill can leave a stale record, which
shared discovery handles on the next start.

Native `service stop` leaves login startup configured. `service uninstall`
removes the service while retaining models, settings and account state.
`POTLUCK_DATA_DIR` creates an independent native service profile. Reinstall the
service after changing its environment, upgrading Node, or upgrading Potluck.
macOS logs are available through `potluck logs`; Linux service logs use
`journalctl --user -u ai.trypotluck.runtime.service` (custom profiles have a suffix,
shown by `service status`).

For a Homebrew installation, these commands delegate to `brew services` so one
manager owns startup. `service install` maps to `brew services start potluck`;
`service stop` and `uninstall` map to `brew services stop potluck`, which also
disables automatic startup. The formula and user data remain installed. Homebrew
services use the default profile; use `serve` or `up` for temporary profiles.
Installing a formula or archive alone does not start a service, enable the
gateway, download a model or connect to the mesh.

### Account and household setup (macOS/Linux)

`potluck setup` starts/attaches to the engine, keeps existing CLI preferences,
and asks before enabling the gateway, signing in, or connecting your household.
New preferences start with `local` scope. Repeating setup resumes from existing
service state; it does not rotate keys or enroll an already-connected machine
again. For unattended setup, only explicitly requested steps run:

```bash
potluck setup --no-input --enable-gateway --json
potluck login --no-wait --json
# Open the reported verificationUriComplete on any device; the code is filled in.
# A browser that approved a terminal before approves with one click. Otherwise
# enter your email and approve from the emailed link.
potluck login --wait --json
potluck setup --no-input --connect --name server --json
```

`login` starts the engine in the background if needed (as `tailscale login`
does with its daemon), opens your browser with the code filled in, and waits.
`logout`, `whoami`, `household` and `connect` also start the engine if needed;
on a computer where the desktop app is running they use the app's engine and
sign-in (an app too old to share its engine produces an error that asks you to
update it). Over SSH, in CI,
on Linux without a display, or with `--no-browser`, it prints the link instead.
With `--json`, `--no-wait`, or piped input it returns approval instructions
immediately and never opens a browser. `--wait` finishes or resumes
the request. Ctrl-C and engine restarts preserve pending approval until expiry.
`--timeout` bounds each request, not the overall approval window (15 minutes).
An explicit non-waiting login exits 0 with `state: pending`; setup awaiting
approval exits 1. Denied/expired login and signed-out `whoami` also exit 1.

Member and device bearers stay in private files under `~/.potluck/auth`
(directory 0700, files 0600), bound to the configured cloud origin. CLI reports
never contain these credentials. The packaged POSIX app reads the same member
session and imports its legacy session once. A signed-out record prevents old
app state from restoring a logged-out account. `logout` revokes the server session;
if offline, `logout --local` removes it here without server revocation. Logout
does not remove mesh enrollment, alter gateway access, or revoke other sessions.

To join a household without signing the destination machine into your account:

```bash
# Signed-in machine:
potluck household invite --output /private/path/invite.json
# Securely transfer the file, preserving chmod 600. On the destination:
potluck connect --invite-file /private/path/invite.json --name server
```

Invitations are one-use credentials: never place their contents in shell arguments
or public logs. Export refuses to overwrite a file. Import checks ownership,
permissions, expiry, and that the mesh daemon uses the same coordinator.
`disconnect` removes local mesh keys and enrollment; the old coordinator entry
may remain until cleanup. It does not sign the member out.

Account requests go to the cloud API at `https://api.trypotluck.ai` by default. For
local development, set `POTLUCK_CLOUD_API_URL` before starting the engine.
Household requests use `network.coordinator_url`, then `POTLUCK_COORDINATOR_URL`,
then `https://coordinator.trypotluck.ai`; the mesh daemon must agree.

### One-shot mode (`-p` / `--print`)

`potluck -p "task"` runs a single task non-interactively and exits, for scripts
and CI. Assistant prose goes to **stdout**; actions, results, and notices go to
**stderr**, so `potluck -p "…" > out.txt` captures just the answer. Reads
auto-run; **edits, writes, and commands are refused** unless you pass `--yes`.

### Network scope

`--scope local|mine` (and `/scope` in-session) sets the per-request routing
scope, sent to the gateway as the `X-Potluck-Scope` header:

- `local`: this machine only.
- `mine`: your machines (household). **Default.**

`circle` / `cooperative` are accepted but are **single-turn only** (not available
for agent sessions yet); they fall back to `mine` with a notice.

### Verify-and-fix

After a successful `edit_file`/`write_file`, the CLI runs a **fixed** verify
command (a build or test) and folds the outcome into the `TOOL_RESULT` fed back
to the model. On failure the model sees the (tail-capped) errors and fixes them;
the task is not done until verify passes. The latest result stays in subsequent
tool feedback while it is current. An approved `run_command` that starts may
change files, even if it exits with an error, so its feedback marks earlier
verification as out of date. The fixed check runs again before accepting the
next final response, or immediately after a later file edit. Diagnostic shell
commands do not each trigger a full check.

Read-only file tools, no-op edits, declined commands and commands that could not
start leave the current check result unchanged. If the model finishes without
any check, the CLI runs the configured check once before accepting completion.
A failure gives it a chance to repair within the remaining turn budget.

The command is the resolved one only: the model can't choose it, and it runs
without the `[y/N]` confirm (you opted in by configuring it). After 4 consecutive
failures the CLI stops the task as incomplete. One-shot runs exit with status 1.

The command is resolved (low → high) `auto-detect < cli.json.verify <
POTLUCK_VERIFY < --verify`; `--no-verify` (or `verify: "off"` / `""` in
`cli.json`) disables it. **Auto-detect**: a `package.json` with a `build` script →
`pnpm build` (if `pnpm-lock.yaml`) or `npm run build`; else a `tsconfig.json` →
`npx tsc --noEmit`; else none. The resolved command shows in the banner
(`verify: pnpm build`), and `/verify` runs it on demand.

### Setup wizard

`potluck setup --preferences` configures the CLI's defaults and writes `~/.potluck/cli.json`.
It can **import** the default model and coordinator URL from the desktop app's
`~/.potluck/config.json`, and can write those **safe shared fields back** so the
app and CLI stay in sync, using the authenticated engine API. The preference
wizard does not enable or rotate the gateway; use `potluck gateway enable`.
The wizard also runs
automatically the first time you use `potluck` in a terminal.

### Configuration

Configuration uses two files under `~/.potluck` (private runtime control and
member credentials live separately in `run/` and `auth/`):

- `config.json`: owned by the **runtime**. The CLI reads the gateway
  key/switch, the coordinator URL, and the default model (`active_model_id`); it
  sends opt-in shared changes through the settings API instead of rewriting it.
- `cli.json`: owned by the **CLI** (default model, default scope, gateway URL).

Layering, lowest → highest precedence:

```
defaults  <  config.json (app)  <  cli.json (CLI prefs)  <  env  <  flags
```

| Setting  | Resolution (low → high)                                                       |
| -------- | ---------------------------------------------------------------------------- |
| base URL | `http://127.0.0.1:8321/v1` → `cli.json.gatewayUrl` → `POTLUCK_GATEWAY_URL`    |
| API key  | `gateway.api_key` (config.json) → `POTLUCK_GATEWAY_KEY`                       |
| enabled  | `gateway.enabled` (config.json); forced `true` if `POTLUCK_GATEWAY_KEY` set   |
| model    | `active_model_id` (config.json) → `cli.json.model` → `--model`                |
| scope    | `mine` → `cli.json.scope` → `--scope`                                         |
| verify   | auto-detect → `cli.json.verify` → `POTLUCK_VERIFY` → `--verify` (`--no-verify` off) |

If the gateway is off or has no key, the CLI prints an actionable message and
exits non-zero:

```
The Potluck gateway is off (or has no API key).
Enable the gateway in the Potluck app: Settings -> Connect tools.
```

## The agent: action protocol

Each assistant turn is EITHER a final prose answer OR exactly one action:

````
```action
{"tool": "read_file", "args": {"path": "src/foo.ts"}}
```
````

The CLI runs the tool and appends a user message `TOOL_RESULT: <result>`, then
the loop continues. The loop ends when the model replies with no action block.
Malformed JSON is fed back as an error so the model can retry.

### Tools & the safety model

| Tool           | Args                                  | Behavior                                       |
| -------------- | ------------------------------------- | ---------------------------------------------- |
| `search_files` | `{query, path?, regex?}`              | grep-like content search. **Auto-runs** (read).|
| `glob`         | `{pattern}`                           | Find files by name, e.g. `**/*.ts`. **Auto-runs**. |
| `read_file`    | `{path}`                              | Read a UTF-8 file. **Auto-runs** (read).       |
| `list_dir`     | `{path}`                              | List a directory. **Auto-runs** (read).        |
| `edit_file`    | `{path, old_string, new_string}`      | Targeted edit to an existing file. **Asks first.** |
| `write_file`   | `{path, content}`                     | Create a new file / full overwrite. **Asks first.** |
| `run_command`  | `{command}`                           | Run a shell command. **Asks first.**           |

- **Read-only tools auto-run.** Anything that changes your machine is gated.
- `search_files` and `glob` honor `.gitignore` (and always skip `.git` and
  `node_modules`), and cap their output (noting truncation).
- `search_files.path` can select a file or directory. Missing or ignored paths
  return an error so the agent can correct its search.
- `edit_file` replaces one **exact, unique** `old_string`; it fails clearly if
  the string is missing or appears more than once. Use it for existing files;
  `write_file` is for creating new ones.
- `edit_file` / `write_file` show a **unified diff** of the change before asking `[y/N]`.
- `run_command` shows the exact command before asking `[y/N]`.
- Declining feeds `TOOL_RESULT: user declined` back to the model.
- All paths resolve **under the current working directory**; anything that
  escapes it is refused.
- File contents only ever travel to the local gateway (`127.0.0.1`).

## Development

```bash
pnpm build      # tsc -> dist/
pnpm test       # vitest (action parser, config resolution, tool executors)
pnpm dev        # build + run
```

No live gateway is needed for the build or the tests.

### Guided model setup

Interactive `potluck setup` shows available memory and disk space, recommends
a fitting model, and lets you choose an installed/catalog model or skip. After
confirmation it waits for the verified download, loads locally, and runs a short
synthetic inference test. It saves a default only after a real response succeeds.
Existing installed/loaded models are preferred; resource estimates are conservative
and the inference test is the readiness check. A project lock always takes priority.

```bash
potluck setup                                      # guided choice and progress
potluck setup --no-input --with-model               # explicitly accept the recommendation
potluck setup --no-input --model qwen3-1.7b-q4       # explicit model, download/load/test
potluck setup --no-input --with-model --enable-gateway --json
```

When the engine's reply to that test carries an answer check, setup prints one line
before `Ready:` saying the answers were checked and where (for example `Answers
checked: passed on the GPU (CUDA).`), or the engine's own sentence when the GPU gave
wrong answers and the model runs on the CPU. If the engine refuses the test, setup
fails with the engine's plain sentence and nothing else from its reply. With `--json`
the model report gains an `acceptance` key. `potluck models test` runs the check again
whenever you want.

Unattended setup without `--with-model` or `--model` keeps its existing behavior
and never starts model downloads. Gateway access, login, connection, and sharing
remain separate choices. Tests use only a fixed local prompt, never project files.
`model_ready` means local generation passed; an enabled gateway is additionally
required for coding agents and external tools. Rerun setup after interruption;
verified weights are reused and matching partial downloads resume. Ctrl-C in the
interactive download step requests cancellation only of a transfer it started.
Native model loads may still finish after a timeout; check `potluck status` before
retrying. The setup endpoints require a runtime that includes them.

### Benchmark and switch models

```sh
potluck benchmark                           # in a terminal: pick a model; otherwise measure the loaded one
potluck benchmark --model qwen3-8b-q4 --yes # switch to this model, measure it, load the previous one again
potluck benchmark --model gemma-3-12b-it-q4 --keep --yes  # switch and stay on the new model
potluck benchmark --runs 5 --json
potluck benchmark --scope mine              # also time models your other machines have loaded
```

`benchmark` shows this machine's hardware with its total and currently available
memory in GiB (the same figures `setup` prints), the model loaded now, where it runs
(GPU or CPU, how many layers are on the GPU, context size and backend, as the
engine reports them) and the engine's last benchmark for it. The available memory is
what the fit estimates use, so when little is free many models are listed as not
possible. Then it measures:
one warm-up call that is not counted, followed by `--runs` timed calls (1 to 10,
default 3) of the engine's own benchmark. The output has this shape:

```text
This machine: <hardware>, <n> GiB memory, <n> GiB available now
Loaded: <model> on the GPU (<backend>), all <n> layers, <n>-token context
<model>, 3 runs: <n> tokens/s (<min> to <max>), first token <n> ms, prompt processing <n> tokens/s
Engine's own figure: <n> tokens/s, counted over the whole response including the wait for the first token.
```

Each number is the median of the timed runs, with the slowest and fastest run in
parentheses when they differ. In the JSON, rates and times are rounded to one decimal. Generation speed is `(output tokens - 1) /
(duration - time to first token)`, so it leaves out the wait for the first
token; it is not reported when a response has one token or fewer. The engine's own
`tokens_per_sec` divides by the whole duration, so the wait for the first token
counts against it, and it is printed separately for what it is. Output tokens are the
engine's count of streamed chunks, which is one per token with llama.cpp. Prompt processing comes from a fixed prompt of about 700 tokens and is
left out when the engine does not report it. If the model runs on the CPU, or
only partly on the GPU, while the engine lists a GPU, one line says so.

When the engine checks the model's answers, one line follows the `Loaded:` line:
`Answers checked on this setup: passed, <date>` (with `(reused from an earlier
check)` when a stored result was used), the engine's own sentence when the GPU gave
wrong answers and the model was moved to the CPU or the model failed the check,
`still running`, or `no, the check is turned off`. After a move to the CPU the
engine's sentence replaces the note about the CPU above. A model that failed the check
is still measured, and the summary says its answers did not pass. Nothing is printed
for an engine without the check. The JSON has the same information under `acceptance`
(null when there is none).

Choosing the model:

- `--model <id>` uses that model.
- In a terminal without `--model`, it lists installed models, models that can be
  downloaded with their size, and models that do not fit with the reason, then
  asks. Enter keeps the loaded model, an ID picks that model, `q` quits and
  changes nothing.
- Without a terminal (or with `--json`) and without `--model`, it measures the
  loaded model. If nothing is loaded it stops and asks for `--model`, except with
  `--scope mine`, where it skips the local part with one line and measures only the
  other machines.

A model that is not loaded needs a switch: download it if it is not installed,
replace the model loaded on this machine, and load the new one. It says so and asks
for a yes in a terminal. Without a terminal, `--yes` is required. A model the
engine marks as not fitting is refused with its reasons, as in `setup`. The
download finishes before the loaded model is replaced, and a failed download
leaves it alone. The engine remembers the last model loaded on this machine and
loads it again when the engine restarts, so a switch changes that choice until the
previous model is loaded again.

After a switch, the machine is put back as it was. If a model was loaded before, it
is loaded again. If none was, the benchmark model is unloaded (which also clears it as
the startup model). In a terminal it asks first: `Keep <id> loaded, or switch back
to <previous>?` or, when nothing was loaded, `Keep <id> loaded, or unload it?` with
keep as the default. `--keep` leaves the benchmark model loaded and `--restore`
puts the machine back without asking. If putting it back fails, the output names the
model that is loaded now and the command exits 1. A model loaded as a split or
pipeline model cannot be loaded again this way, so `benchmark` will not switch away
from one.

`--scope mine` then also times the models your other machines have loaded, from
this machine, over the mesh. Each model gets a warm-up and `--runs` timed requests
of one fixed short prompt, timed on the client: time to first token, and
chunks per second after the first one. These rows include the network and are
labelled that way. The engine says only that one of your other machines answered.
When just one machine offers the model, the row names it; when several do, the row
lists them and says the engine does not say which answered. A model that is also
loaded on this machine is skipped, because a request for it is answered here. With no
model loaded here and no `--model`, the local part is skipped and the command exits 0
if at least one other machine was measured. At most 8 models are measured per run. Nothing is
switched or downloaded on other machines.

The benchmark only ever measures the Potluck engine on this machine. `--scope mine`
does not use `gatewayUrl` from `cli.json`, `POTLUCK_GATEWAY_URL` or
`POTLUCK_GATEWAY_KEY`. It sends its requests to the address in the verified runtime
record, with the key the engine reports for its own gateway, and it counts an answer
only when the engine marks it as served by one of your other machines. It needs the
gateway on (`potluck gateway enable`) and checks that before anything is switched.
If your configured gateway address is different, the output says it was not used.

Ctrl-C stops, cancels a download only if this command started it, and puts the
machine back as it was if a switch had begun; a second Ctrl-C stops at once. Exit
codes: 0 measured, 1 not measured (or the previous model could not be restored),
130 interrupted. `--timeout` applies to each engine call (default 120000 ms, up to
300000); a load that outlasts it may still finish, so check `potluck status`.

`--json` prints one object with `schemaVersion: 1`, `status`, `machine`, `model`,
`ranOn`, `runs` (each run's numbers), `summary` (median, min and max), `switch`
(what happened to the model that was loaded before), `loadedAtEnd`, `localSkipped`
(set when only the other machines were measured) and, with `--scope mine`, `mine`. Engine error text, addresses and keys are never printed.
It needs a running engine and a runtime that has the model setup and benchmark
routes (`potluck up`).

### Launch Aider

Install [Aider](https://aider.chat/docs/install.html) separately, then run:

```sh
potluck launch aider --scope local
potluck launch aider --message "Explain this project without editing files"
```

The launcher selects the project pin first, checks model readiness, and starts a
private loopback bridge for this invocation. It enforces the selected model,
SHA-256 and `local`/`mine` scope on every request, including Aider's weak/editor
models. Your gateway key stays in Potluck; the child receives a temporary bridge
key. Aider's existing config files are preserved. Automatic commits, analytics,
and update checks are disabled for the launch. Other Aider behavior, including
file editing and history, remains under Aider's controls. Aider must already be
installed; `POTLUCK_AIDER_BIN` can specify its absolute executable path.
Noninteractive `--message` launches also disable Aider's `.gitignore` edit prompt.
If the bridge rejects a one-shot request for its context or request format,
Potluck exits with a failure status even if Aider exits successfully.

Before starting Aider, Potluck requires a known **loaded** context of at least
8,192 tokens on the selected route and a completed synthetic readiness probe.
This minimum is Potluck's launcher policy, not an Aider requirement. If context
is unknown, update the serving runtime and load the model with a known context;
for a smaller context, reload it with at least 8,192 tokens and retry. A model's
advertised maximum does not substitute for the context actually loaded.

Potluck supplies Aider model metadata and history/repository-map budgets derived
from that loaded context. It reserves one quarter for output, capped at 4,096
tokens, plus 512 tokens of safety margin. Aider's metadata and history/map budgets
are advisory. The bridge separately caps output and checks the entire plain-text
request using a conservative UTF-8 byte estimate with message/template overhead,
not the model's exact tokenizer. It rejects oversized requests without truncating
files: clear old chat history, include fewer or smaller files, or load a larger
context and restart the launcher. The runtime remains authoritative about what
fits. The bridge rechecks the pinned model's context before each request; a
smaller or unknown context requires restarting the launcher. Checks are snapshots,
so a later reload or route change can still cause the runtime to refuse a request.

Validated with Aider 0.86.2. The repeatable integration check is
`POTLUCK_AIDER_BIN=/absolute/path/to/aider node scripts/validate-aider.cjs` from
`apps/cli`; it uses an isolated project and test gateway.

With a `potluck.lock`, `potluck doctor` and `status` also check gateway lock
compatibility, the exact loaded route, installed local checksums and estimated
memory fit. These are read-only snapshots; inference checks the pin again when
admitted. Missing local weights are a warning when a matching own machine can
serve the project. Use `--json` for stable check IDs and actionable failures.

### Contribution controls

```sh
potluck contribute status --json
potluck contribute limits --jobs 1 --max-tokens 2048 --min-free-memory 2
potluck contribute schedule 22:00-06:00 --days mon,tue,wed,thu,fri --timezone America/New_York
potluck contribute on
potluck contribute share circles
potluck contribute off
potluck contribute schedule off
```

Trusted circles are people who lend each other spare compute:

```sh
potluck circle create "Friends"
potluck circle invite "Friends"          # prints a code that works once
potluck circle join ABCD-EFGH-JKMN       # shows what the invite is for
potluck circle join ABCD-EFGH-JKMN --yes # joins
potluck circle list                      # who is in each circle, and what it offers now
potluck circle leave "Friends" --yes
```

Joining lets the people in a circle send requests to your machines that share
with circles, and lets you send requests to theirs. The machine that answers
sees the conversation. To lend this machine, run `potluck contribute share
circles` and `potluck contribute on`. To borrow, run `potluck run --scope
circle`.

These controls apply to coordinator jobs served by the shared runtime. `share`
chooses who else the machine answers while contribution is on: `mine` (your own
machines only, the default), `circles` (members of your trusted circles) or
`everyone` (circles and the open pool, where the build has it). A machine that
shares with circles is never offered open-pool work, and it refuses a
coordinator that cannot enforce that. Changing
limits or schedules does not enable contribution. A schedule uses machine local
time unless an IANA timezone is supplied; overnight windows belong to their
starting weekday. Outside the window, capabilities are withdrawn and new jobs
are declined. Already admitted jobs finish. `off` stops the contributor connection.
A zero job limit pauses admissions. Jobs above the configured output-token cap
are declined so another contributor can serve them. The free-memory threshold is
checked at admission; it is not a process memory ceiling or a GPU throttle.
Household inference, the loaded model, gateway routing, and other settings retain
their existing controls. Status reports whether the contributor is connected and
accepting jobs; enabling it alone does not prove successful registration.

### Plain inference

```sh
potluck run "Explain continuous batching" --scope local
cat notes.txt | potluck run "Summarize these notes" --json
potluck run "Reply briefly" --max-tokens 128 --timeout 60000
```

`run` sends a single plain chat request without coding tools, repository scanning,
or history persistence. The project model lock takes precedence. Stdin follows
the prompt, with a combined 1 MiB limit. The default deadline is two minutes and
includes reading stdin and model discovery. Text streams to stdout; errors go to
stderr. JSON returns one complete result with model, scope, checksum, text,
duration and first-token timing. A disconnected or malformed stream fails;
partial text may already have been printed in text mode. Exit codes are 0 for
success, 1 for inference failure, 2 for invalid input, 124 for timeout, and 130
for cancellation. The JSON envelope is structured; generated text is not forced
into a JSON schema.

### Saved coding-agent sessions

```sh
potluck --save-session
potluck --save-session -p "Explain the test failure" --scope local
potluck sessions list --json
potluck --resume <session-id>
potluck --resume <session-id> -p "Continue with the next step"
potluck sessions delete <session-id>
```

Saving is opt-in and requires `potluck.lock`. Completed turns, including tool
results, are stored under `~/.potluck/sessions` (0700 directory, 0600 files).
Transcripts can contain project content and are not encrypted. They are limited
to 8 MiB; an unsuccessful save leaves the prior save unchanged. Listing shows
metadata only. Resume checks the original project, working directory, model ID,
checksum, byte size and routing scope before sending history. It forks a new
session, retaining the original, rebuilds system/repository context, and never
replays stored tool calls. Current approval and verification settings still
apply. Saving history does not snapshot or restore files; cancelled or failed
turns do not replace the last completed save.

### Live status

```sh
potluck status --scope local
potluck status --scope mine --watch
potluck status --watch --interval 5000 --json
```

Watch refreshes after each completed sample, avoiding overlapping polls. In a
terminal it redraws the view; `--json` emits one JSON object per sample for log
collectors. Ctrl-C stops promptly, including during a service request. The view
shows runtime activity, loaded context, free RAM, the loaded model's last
benchmark, and recorded request latency over the last 24 hours. Missing samples
stay unavailable. Benchmark rates are historical measurements, not current
throughput. With `mine` scope, own-machine capability probes are bounded and
reported as aggregate counts/model availability; incomplete probes are marked
partial. No other member identities or prompt text appear in these metrics.

When the engine checks the loaded model's answers, one more line says what it found
(the same line `benchmark` prints), and the JSON has an `acceptance` key, null for an
engine without the check.

### Household batch jobs

Summarize selected text files across your own computers, with saved progress and
whole-file retries. This workflow has no tools and cannot edit files or execute
instructions in a document. The runtime must include batch support.

```sh
potluck up
potluck models load <model-id>
potluck models pin <model-id>
potluck batch submit notes/meeting.txt notes/plan.md --scope mine --jobs 3
potluck batch status <job-id> --watch
potluck batch results <job-id> --output summaries.txt
```

Use `--scope local` to process everything on this computer. `mine` explicitly
allows document content to go directly to your own household computers. Each
worker needs batch support, the same loaded model ID and SHA-256, and household
serving enabled. The scheduler does not download/load models, change sharing
settings, or use a circle, open pool, or cloud fallback. Upgraded workers report
complete versus token-limited responses; older peers are skipped.

The runtime owns the queue, so exiting the terminal or stopping `--watch` does
not stop the batch. Keep the submitting computer and its runtime running. One
batch runs at a time, with one piece per eligible machine and up to `--jobs`
concurrent pieces (1–8, default 3). The same machine may also be serving other
applications; this is not an exclusive capacity reservation.

```sh
potluck batch list
potluck batch cancel <job-id>
potluck batch resume <job-id>
potluck batch results <job-id> --json --output summaries.json
potluck batch delete <job-id>
```

Only complete results are saved as finished. A failed worker's partial summary
is discarded, and its file can retry on another eligible worker. Each file has
at most three attempts across all resumes. Completed files are not repeated.
When no eligible workers remain, the batch pauses. Load the pinned model or
restore connectivity, then resume. A runtime restart also pauses unfinished
jobs. Resume requires the original project pin and reuses the saved input, even
if the original file changed or disappeared. `cancel` stops admissions and drains
active requests; wait for that drain before resuming or deleting. Files that
exhaust their attempts or hit the output limit remain failed; submit a new batch
to retry them after correcting the cause.

Select 1–64 regular UTF-8 files explicitly. No directory recursion or leaf
symlinks; each file is limited to 16 KiB, with 256 KiB total input. Files must
also fit the selected model's context. Large-document splitting, PDF extraction
and repository-wide review are not included. `--max-tokens` is 64–2048 (default
512) per summary; a token-limited result is reported as failed, not completed.
Summary quality and formatting depend on the selected model.

Input snapshots, the prompt template and completed results are stored in private
plaintext under `~/.potluck/batches` (or `POTLUCK_DATA_DIR`), with a 0700 directory
and 0600 SQLite file on POSIX. At most 32 saved batches are retained; delete an
old batch to free a slot. Peers do not save document content, and the coordinator
does not receive it. `delete` removes the batch and its input/output records.
`--output` creates a new 0600 file and refuses to overwrite an existing path.

JSON status includes file names, input checksums and attempt metadata, without
document text or summaries. `results` explicitly returns summaries; incomplete
jobs return exit 1 with completed pieces still available. `status --watch`
emits one JSON object per sample with `--json`, ends on a terminal/paused state,
and Ctrl-C only stops the watcher. Submission returns a job ID immediately;
an uncertain connection failure tells you which ID to inspect before retrying.

See [architecture and recovery semantics](../../docs/architecture/household-batch-jobs.md).

### What do my connected computers add?

Run `potluck models available` (or add `--json`) to see models currently loaded on your own responding machines, exact model hashes, idle serving-machine counts, and compatible split workers. In a project, the report checks `potluck.lock`; the same model name with different bytes does not satisfy a pin.

Discovery does not enable sharing or change request scope. Choose `--scope mine` explicitly for owned-machine inference. For a split, enable workers on their computers, pin the installed model on the head, then preview with `potluck split plan <pinned-model> --peer <owned-machine>`. Start remains a separate explicit command. `potluck split status` reports elapsed time, loading phase, and transferred bytes; remote placement is verified before the model becomes ready.
