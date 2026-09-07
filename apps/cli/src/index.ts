#!/usr/bin/env node
/**
 * potluck — a terminal coding agent whose inference runs on the Potluck mesh.
 *
 * Modes:
 *   potluck                 interactive REPL (prompt-based ReAct loop)
 *   potluck -p "task"       one-shot: run a single task and exit (scripting/CI)
 *   potluck setup           configure CLI defaults (also auto-runs on first use)
 *
 * Streams the assistant output, renders actions + results, and gates every
 * edit/write/command behind a y/N confirm (auto-approved with --yes in -p mode).
 * In-session commands: /exit, /model, /scope, /verify, /snapshot, /clear, /help.
 */

import { createInterface, type Interface } from "node:readline";
import { basename } from "node:path";
import { stdin, stdout, stderr } from "node:process";

import {
  assertGatewayReady,
  cliPrefsExist,
  GatewayDisabledError,
  loadCliPrefs,
  loadConfig,
  loadSharedPrefs,
  modelCapabilityWarning,
  normalizeScope,
  resolveModel,
  resolveScope,
  resolveVerify,
  type GatewayConfig,
  type Scope,
  type VerifyResolution,
} from "./config.js";
import { listModels, type ChatMessage } from "./gateway.js";
import { runTask, systemPrompt } from "./agent.js";
import {
  briefSection,
  extractSnapshotBody,
  formatAge,
  projectRoot,
  readSnapshot,
  snapshotHeader,
  snapshotPath,
  snapshotRequest,
  writeSnapshot,
} from "./snapshot.js";
import { buildRepoMap } from "./repomap.js";
import { runVerify } from "./tools.js";
import { runSetup, printSetupHelp } from "./setup.js";
import {
  c,
  makeConfirm,
  oneShotConfirm,
  renderAction,
  renderResultPreview,
} from "./ui.js";

const VERSION = "0.1.0";

interface CliArgs {
  help: boolean;
  version: boolean;
  print: boolean;
  yes: boolean;
  model?: string;
  scope?: string;
  verify?: string;
  noVerify: boolean;
  task?: string;
  command?: "setup";
}

function parseArgs(argv: string[]): CliArgs {
  const args: CliArgs = {
    help: false,
    version: false,
    print: false,
    yes: false,
    noVerify: false,
  };
  const positionals: string[] = [];

  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === "--help" || arg === "-h") args.help = true;
    else if (arg === "--version" || arg === "-v") args.version = true;
    else if (arg === "--yes" || arg === "-y") args.yes = true;
    else if (arg === "--model" || arg === "-m") {
      const next = argv[i + 1];
      if (next !== undefined) {
        args.model = next;
        i++;
      }
    } else if (arg.startsWith("--model=")) {
      args.model = arg.slice("--model=".length);
    } else if (arg === "--scope") {
      const next = argv[i + 1];
      if (next !== undefined) {
        args.scope = next;
        i++;
      }
    } else if (arg.startsWith("--scope=")) {
      args.scope = arg.slice("--scope=".length);
    } else if (arg === "--no-verify") {
      args.noVerify = true;
    } else if (arg === "--verify") {
      const next = argv[i + 1];
      if (next !== undefined) {
        args.verify = next;
        i++;
      }
    } else if (arg.startsWith("--verify=")) {
      args.verify = arg.slice("--verify=".length);
    } else if (arg === "--print" || arg === "-p") {
      args.print = true;
      const next = argv[i + 1];
      if (next !== undefined && !next.startsWith("-")) {
        args.task = next;
        i++;
      }
    } else if (arg.startsWith("--print=")) {
      args.print = true;
      args.task = arg.slice("--print=".length);
    } else if (!arg.startsWith("-")) {
      positionals.push(arg);
    }
    // Unknown flags are ignored rather than fatal.
  }

  if (positionals[0] === "setup") {
    args.command = "setup";
  } else if (args.print && args.task === undefined && positionals.length > 0) {
    args.task = positionals.join(" ");
  }
  return args;
}

function printHelp(): void {
  stdout.write(
    [
      `${c.bold("potluck")} — a member-owned terminal coding agent`,
      "Inference runs on your own machines across the Potluck mesh.",
      "",
      `${c.bold("Usage")}`,
      "  potluck [options]              start the interactive REPL",
      '  potluck -p "task" [--yes]      run one task and exit (scripting/CI)',
      "  potluck setup                  configure CLI defaults",
      "",
      `${c.bold("Options")}`,
      "  -m, --model <id>       Model to use (default: cli.json / app / first installed)",
      "      --scope <s>        Routing scope: local | mine (default from cli.json)",
      "      --verify <cmd>     Build/test command run after each edit (default: auto-detect)",
      "      --no-verify        Disable the post-edit verify step",
      '  -p, --print "task"     Run a single task non-interactively, then exit',
      "  -y, --yes              In -p mode, auto-approve edits/writes/commands",
      "  -h, --help             Show this help",
      "  -v, --version          Show version",
      "",
      `${c.bold("Environment")}`,
      "  POTLUCK_GATEWAY_URL   Gateway base URL (default http://127.0.0.1:8321/v1)",
      "  POTLUCK_GATEWAY_KEY   Gateway API key (overrides ~/.potluck/config.json)",
      "  POTLUCK_VERIFY        Verify command (overrides cli.json; \"off\" disables)",
      "  NO_COLOR              Disable colored output",
      "",
      `${c.bold("In-session commands")}`,
      "  /model [id]   Show or switch the model",
      "  /scope [s]    Show or switch the routing scope (local | mine)",
      "  /verify       Run the verify command now",
      "  /snapshot     Save a handoff brief for the next session (~/.snapshots)",
      "  /clear        Clear the conversation and re-read the handoff brief",
      "  /help         Show help",
      "  /exit         Quit",
      "",
      "Config layers (low -> high): defaults < ~/.potluck/config.json (app) <",
      "~/.potluck/cli.json (CLI prefs) < env < flags.",
      "The gateway must be enabled in the Potluck app (Settings -> Connect tools).",
      "",
    ].join("\n"),
  );
}

function printBanner(
  model: string,
  scope: Scope,
  cfg: GatewayConfig,
  verify: VerifyResolution,
): void {
  stdout.write(
    `\n${c.bold("potluck")} ${c.dim("·")} ${c.cyan(model)} ${c.dim("·")} ${c.yellow(
      `scope: ${scope}`,
    )} ${c.dim("·")} ${c.green("running on the Potluck mesh")}\n`,
  );
  const verifyLabel = verify.command
    ? `verify: ${verify.command}${verify.autoDetected ? " (auto)" : ""}`
    : "verify: off (pass --verify '<test/build cmd>' so edits are checked)";
  stdout.write(`${c.dim(cfg.baseURL)} ${c.dim("·")} ${c.dim(verifyLabel)}\n`);
  stdout.write(
    `${c.dim("type a task, or")} ${c.bold("/help")} ${c.dim("·")} ${c.bold(
      "/model",
    )} ${c.dim("·")} ${c.bold("/scope")} ${c.dim("·")} ${c.bold("/snapshot")} ${c.dim("·")} ${c.bold("/verify")} ${c.dim(
      "·",
    )} ${c.bold("/exit")}\n\n`,
  );
}

interface BriefState {
  root: string;
  file: string;
  brief: string | null;
}

/**
 * Load the handoff brief for this project (written by /snapshot here, or by
 * Claude Code / Codex through the same store) and announce it when present.
 */
async function loadBrief(cwd: string, say: (line: string) => void): Promise<BriefState> {
  const root = await projectRoot(cwd);
  const file = snapshotPath(root);
  const snap = await readSnapshot(file);
  if (!snap) return { root, file, brief: null };
  say(c.dim(`handoff brief loaded: ${file} (written ${formatAge(snap.ageMs)} ago)`));
  return { root, file, brief: briefSection(snap.text, file, snap.ageMs) };
}

/** Stream a single task to stdout and exit (-p mode). Notices go to stderr. */
async function runOneShot(
  task: string,
  opts: {
    cfg: GatewayConfig;
    model: string;
    scope: Scope;
    cwd: string;
    yes: boolean;
    verifyCommand: string | null;
  },
): Promise<void> {
  const confirm = oneShotConfirm(opts.yes, (line) => stderr.write(`${line}\n`));
  const { brief } = await loadBrief(opts.cwd, (line) => stderr.write(`${line}\n`));
  const conversation: ChatMessage[] = [
    { role: "system", content: systemPrompt(opts.cwd, opts.verifyCommand, brief) },
  ];
  const repoMap = await buildRepoMap(opts.cwd);
  if (repoMap) conversation.push({ role: "system", content: repoMap });
  if (!opts.verifyCommand) {
    stderr.write(
      c.dim(
        "no verify command — pass --verify '<test/build cmd>' so the agent checks its own edits.\n",
      ),
    );
  }
  await runTask(conversation, task, {
    cfg: opts.cfg,
    model: opts.model,
    cwd: opts.cwd,
    confirm,
    scope: opts.scope,
    verifyCommand: opts.verifyCommand,
    callbacks: {
      onToken: (t) => stdout.write(t),
      onTurnEnd: () => stdout.write("\n"),
      onAction: (a) => stderr.write(`${renderAction(a.tool, a.args)}\n`),
      onToolResult: (r) => stderr.write(`${renderResultPreview(r)}\n`),
      onNotice: (m) => stderr.write(`${c.yellow(m)}\n`),
      onVerifyStart: (cmd) => stderr.write(`${c.dim(`running verify: ${cmd}`)}\n`),
      onVerify: (cmd, ok) =>
        stderr.write(
          ok
            ? `${c.green("✓ verify passed")} ${c.dim(cmd)}\n`
            : `${c.red("✗ verify failed")} ${c.dim(cmd)}\n`,
        ),
    },
  });
}

/**
 * A close-safe prompt bound to one readline. Resolves to null on EOF / readline
 * close (piped input ending, Ctrl-D) so callers stop instead of calling
 * question() on a closed readline (ERR_USE_AFTER_CLOSE).
 */
function createAsk(rl: Interface): (q: string) => Promise<string | null> {
  let closed = false;
  rl.once("close", () => {
    closed = true;
  });
  return (q: string): Promise<string | null> =>
    new Promise((res) => {
      if (closed) {
        res(null);
        return;
      }
      let done = false;
      const onClose = (): void => {
        if (!done) {
          done = true;
          res(null);
        }
      };
      rl.once("close", onClose);
      rl.question(q, (answer) => {
        done = true;
        rl.removeListener("close", onClose);
        res(answer);
      });
    });
}

function reportGatewayError(cfg: GatewayConfig, err: unknown, sink: NodeJS.WritableStream): void {
  const message = err instanceof Error ? err.message : String(err);
  sink.write(
    `${c.red("Could not reach the Potluck gateway")} at ${cfg.baseURL}\n${c.dim(
      message,
    )}\nIs the Potluck app running with the gateway enabled?\n`,
  );
}

async function main(): Promise<void> {
  const args = parseArgs(process.argv.slice(2));

  // `potluck setup` — interactive wizard (or its help).
  if (args.command === "setup") {
    if (args.help) {
      printSetupHelp((s) => stdout.write(s));
      return;
    }
    const rl = createInterface({ input: stdin, output: stdout });
    const ask = createAsk(rl);
    try {
      await runSetup({ ask, out: (s) => stdout.write(`${s}\n`) });
    } finally {
      rl.close();
    }
    return;
  }

  if (args.help) {
    printHelp();
    return;
  }
  if (args.version) {
    stdout.write(`${VERSION}\n`);
    return;
  }

  let cfg = await loadConfig();
  let cli = await loadCliPrefs();
  const shared = await loadSharedPrefs();
  const cwd = process.cwd();

  let verify = await resolveVerify({
    cwd,
    cliVerify: cli.verify,
    envVerify: process.env.POTLUCK_VERIFY,
    flag: args.verify,
    noVerify: args.noVerify,
  });

  // ---- One-shot (-p / --print) ------------------------------------------
  if (args.print) {
    if (!args.task || !args.task.trim()) {
      stderr.write(
        `${c.red("error:")} -p/--print needs a task, e.g. ${c.bold(
          'potluck -p "fix the build"',
        )}\n`,
      );
      process.exitCode = 1;
      return;
    }
    try {
      assertGatewayReady(cfg);
    } catch (err) {
      if (err instanceof GatewayDisabledError) {
        stderr.write(`${c.yellow(err.message)}\n`);
        process.exitCode = 1;
        return;
      }
      throw err;
    }

    const preferred = args.model ?? cli.model ?? shared.activeModelId;
    let model: string;
    try {
      model = await resolveModel(cfg, preferred);
    } catch (err) {
      reportGatewayError(cfg, err, stderr);
      process.exitCode = 1;
      return;
    }
    const capWarn = modelCapabilityWarning(model);
    if (capWarn) stderr.write(`${c.yellow(capWarn)}\n`);

    const { scope, notice } = resolveScope(args.scope, cli.scope);
    if (notice) stderr.write(`${c.yellow(notice)}\n`);

    try {
      await runOneShot(args.task.trim(), {
        cfg,
        model,
        scope,
        cwd,
        yes: args.yes,
        verifyCommand: verify.command,
      });
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      stderr.write(`${c.red("error:")} ${c.dim(message)}\n`);
      process.exitCode = 1;
    }
    return;
  }

  // ---- Interactive REPL --------------------------------------------------
  const rl = createInterface({ input: stdin, output: stdout });
  const ask = createAsk(rl);

  // First-run setup: auto-run the wizard when cli.json is missing (TTY only, so
  // pipes/CI fall back to defaults instead of hanging on a prompt).
  if (stdin.isTTY === true && !(await cliPrefsExist())) {
    await runSetup({ ask, out: (s) => stdout.write(`${s}\n`) });
    cli = await loadCliPrefs();
    cfg = await loadConfig();
    verify = await resolveVerify({
      cwd,
      cliVerify: cli.verify,
      envVerify: process.env.POTLUCK_VERIFY,
      flag: args.verify,
      noVerify: args.noVerify,
    });
    stdout.write("\n");
  }

  try {
    assertGatewayReady(cfg);
  } catch (err) {
    if (err instanceof GatewayDisabledError) {
      stdout.write(`${c.yellow(err.message)}\n`);
      process.exitCode = 1;
      rl.close();
      return;
    }
    rl.close();
    throw err;
  }

  const preferred = args.model ?? cli.model ?? shared.activeModelId;
  let model: string;
  try {
    model = await resolveModel(cfg, preferred);
  } catch (err) {
    reportGatewayError(cfg, err, stdout);
    process.exitCode = 1;
    rl.close();
    return;
  }
  const capWarn = modelCapabilityWarning(model);
  if (capWarn) stdout.write(`${c.yellow(capWarn)}\n`);

  const initialScope = resolveScope(args.scope, cli.scope);
  let scope: Scope = initialScope.scope;
  if (initialScope.notice) stdout.write(`${c.yellow(initialScope.notice)}\n`);

  const confirm = makeConfirm((q) => ask(q).then((a) => a ?? "n"));
  const briefState = await loadBrief(cwd, (line) => stdout.write(`${line}\n`));
  const conversation: ChatMessage[] = [
    { role: "system", content: systemPrompt(cwd, verify.command, briefState.brief) },
  ];
  const repoMap = await buildRepoMap(cwd);
  if (repoMap) conversation.push({ role: "system", content: repoMap });

  // Ctrl-C cancels the active turn; at an idle prompt it exits.
  let active: AbortController | null = null;
  rl.on("SIGINT", () => {
    if (active) {
      active.abort();
    } else {
      stdout.write(`\n${c.dim("bye")}\n`);
      rl.close();
      process.exit(0);
    }
  });

  printBanner(model, scope, cfg, verify);

  while (true) {
    const answer = await ask(c.green("you ▸ "));
    if (answer === null) break; // EOF / readline closed
    const line = answer.trim();
    if (line.length === 0) continue;

    if (line === "/exit" || line === "/quit") break;
    if (line === "/help") {
      printHelp();
      continue;
    }
    if (line === "/scope" || line.startsWith("/scope ")) {
      const target = line.slice("/scope".length).trim();
      if (target) {
        const resolved = normalizeScope(target, scope);
        if (resolved.notice) stdout.write(`${c.yellow(resolved.notice)}\n`);
        scope = resolved.scope;
        stdout.write(`${c.dim("scope set to")} ${c.yellow(scope)}\n`);
      } else {
        stdout.write(
          `${c.dim("current scope:")} ${c.yellow(scope)} ${c.dim(
            "(local = this machine, mine = your machines)",
          )}\n`,
        );
      }
      continue;
    }
    if (line === "/model" || line.startsWith("/model ")) {
      const target = line.slice("/model".length).trim();
      if (target) {
        model = target;
        stdout.write(`${c.dim("model set to")} ${c.cyan(model)}\n`);
      } else {
        try {
          const models = await listModels(cfg);
          stdout.write(`${c.dim("current:")} ${c.cyan(model)}\n`);
          stdout.write(`${c.dim("available:")}\n`);
          for (const m of models) {
            const owner = m.owned_by ? c.dim(` (${m.owned_by})`) : "";
            stdout.write(`  ${m.id}${owner}\n`);
          }
        } catch (err) {
          const message = err instanceof Error ? err.message : String(err);
          stdout.write(`${c.red("could not list models:")} ${c.dim(message)}\n`);
        }
      }
      continue;
    }
    if (line === "/snapshot") {
      // Ask the model for the brief on a COPY of the conversation so the
      // request does not linger in the real history, then write the file
      // ourselves: the store lives outside the workspace confinement and a
      // deterministic header (time, branch, commit, dirty count) goes on top.
      const draft = [...conversation];
      active = new AbortController();
      stdout.write(`\n${c.dim("potluck ▸ ")}`);
      try {
        await runTask(draft, snapshotRequest(), {
          cfg,
          model,
          cwd,
          confirm,
          scope,
          signal: active.signal,
          verifyCommand: null,
          maxSteps: 4,
          callbacks: {
            onToken: (t) => stdout.write(t),
            onTurnEnd: () => stdout.write("\n"),
            onAction: (a) => stdout.write(`${renderAction(a.tool, a.args)}\n`),
            onToolResult: (r) => stdout.write(`${renderResultPreview(r)}\n`),
            onNotice: (m) => stdout.write(`${c.yellow(m)}\n`),
          },
        });
      } catch (err) {
        const message = active.signal.aborted
          ? "snapshot cancelled"
          : err instanceof Error
            ? err.message
            : String(err);
        stdout.write(`\n${c.yellow(message)}\n`);
        active = null;
        continue;
      } finally {
        active = null;
      }
      const last = draft[draft.length - 1];
      const body = last?.role === "assistant" ? extractSnapshotBody(last.content) : "";
      if (!body) {
        stdout.write(
          `${c.yellow("snapshot not written:")} ${c.dim(
            "the model did not answer with a brief. Try /snapshot again.",
          )}\n`,
        );
        continue;
      }
      const header = await snapshotHeader("potluck", briefState.root);
      await writeSnapshot(briefState.file, {
        title: basename(briefState.root),
        header,
        body,
      });
      stdout.write(
        `${c.green("snapshot written:")} ${briefState.file} ${c.dim("(safe to /clear)")}\n`,
      );
      continue;
    }
    if (line === "/clear") {
      const fresh = await loadBrief(cwd, (l) => stdout.write(`${l}\n`));
      briefState.brief = fresh.brief;
      conversation.length = 0;
      conversation.push({
        role: "system",
        content: systemPrompt(cwd, verify.command, briefState.brief),
      });
      if (repoMap) conversation.push({ role: "system", content: repoMap });
      stdout.write(
        `${c.dim("context cleared")}${briefState.brief ? c.dim(" · briefed from the snapshot above") : ""}\n`,
      );
      continue;
    }
    if (line === "/verify") {
      if (!verify.command) {
        stdout.write(
          `${c.yellow("verify is off")} ${c.dim(
            "(set one with --verify, POTLUCK_VERIFY, or `potluck setup`)",
          )}\n`,
        );
      } else {
        stdout.write(`${c.dim("running verify:")} ${c.cyan(verify.command)}\n`);
        const { code, output } = await runVerify(verify.command, cwd);
        if (code === 0) {
          stdout.write(`${c.green("✓ verify passed")}\n`);
        } else {
          stdout.write(`${c.red(`✗ verify failed (exit ${code})`)}\n`);
          if (output.trim()) stdout.write(`${c.dim(output)}\n`);
        }
      }
      continue;
    }
    if (line.startsWith("/")) {
      stdout.write(`${c.yellow(`unknown command: ${line}`)} ${c.dim("(try /help)")}\n`);
      continue;
    }

    const snapshot = conversation.length;
    active = new AbortController();
    stdout.write(`\n${c.dim("potluck ▸ ")}`);

    try {
      await runTask(conversation, line, {
        cfg,
        model,
        cwd,
        confirm,
        scope,
        signal: active.signal,
        verifyCommand: verify.command,
        callbacks: {
          onToken: (t) => stdout.write(t),
          onTurnEnd: () => stdout.write("\n"),
          onAction: (a) => stdout.write(`${renderAction(a.tool, a.args)}\n`),
          onToolResult: (r) => stdout.write(`${renderResultPreview(r)}\n`),
          onNotice: (m) => stdout.write(`${c.yellow(m)}\n`),
          onVerifyStart: (cmd) =>
            stdout.write(`${c.gray("  ↳")} ${c.dim(`running verify: ${cmd}`)}\n`),
          onVerify: (cmd, ok) =>
            stdout.write(
              ok
                ? `${c.gray("  ↳")} ${c.green("verify passed")} ${c.dim(cmd)}\n`
                : `${c.gray("  ↳")} ${c.red("verify failed")} ${c.dim(cmd)}\n`,
            ),
        },
      });
    } catch (err) {
      if (active.signal.aborted) {
        stdout.write(`\n${c.yellow("✗ turn cancelled")}\n`);
        conversation.length = snapshot; // drop the cancelled turn
      } else {
        const message = err instanceof Error ? err.message : String(err);
        stdout.write(`\n${c.red("error:")} ${c.dim(message)}\n`);
        conversation.length = snapshot;
      }
    } finally {
      active = null;
    }
    stdout.write("\n");
  }

  rl.close();
}

main().catch((err: unknown) => {
  const message = err instanceof Error ? err.stack ?? err.message : String(err);
  process.stderr.write(`${message}\n`);
  process.exit(1);
});
