#!/usr/bin/env node
/**
 * potluck — a terminal coding agent whose inference runs on the Potluck mesh.
 *
 * Reads a task, runs the prompt-based ReAct loop against the local gateway,
 * streams the assistant output, renders actions + results, and gates every
 * write/command behind a y/N confirm. Commands: /exit, /model, /help.
 */

import { createInterface } from "node:readline";
import { stdin, stdout } from "node:process";

import {
  assertGatewayReady,
  GatewayDisabledError,
  loadConfig,
  resolveModel,
  type GatewayConfig,
} from "./config.js";
import { listModels, type ChatMessage } from "./gateway.js";
import { runTask, systemPrompt } from "./agent.js";
import { c, makeConfirm, renderAction, renderResultPreview } from "./ui.js";

const VERSION = "0.1.0";

interface CliArgs {
  help: boolean;
  version: boolean;
  model?: string;
}

function parseArgs(argv: string[]): CliArgs {
  const args: CliArgs = { help: false, version: false };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === "--help" || arg === "-h") args.help = true;
    else if (arg === "--version" || arg === "-v") args.version = true;
    else if (arg === "--model" || arg === "-m") {
      const next = argv[i + 1];
      if (next !== undefined) {
        args.model = next;
        i++;
      }
    } else if (arg.startsWith("--model=")) {
      args.model = arg.slice("--model=".length);
    }
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
      "  potluck [options]",
      "",
      `${c.bold("Options")}`,
      "  -m, --model <id>   Model to use (default: first installed on the node)",
      "  -h, --help         Show this help",
      "  -v, --version      Show version",
      "",
      `${c.bold("Environment")}`,
      "  POTLUCK_GATEWAY_URL   Gateway base URL (default http://127.0.0.1:8321/v1)",
      "  POTLUCK_GATEWAY_KEY   Gateway API key (overrides ~/.potluck/config.json)",
      "  NO_COLOR              Disable colored output",
      "",
      `${c.bold("In-session commands")}`,
      "  /model [id]   Show or switch the model",
      "  /help         Show help",
      "  /exit         Quit",
      "",
      "The gateway must be enabled in the Potluck app (Settings -> Connect tools).",
      "",
    ].join("\n"),
  );
}

function printBanner(model: string, cfg: GatewayConfig): void {
  stdout.write(
    `\n${c.bold("potluck")} ${c.dim("·")} ${c.cyan(model)} ${c.dim("·")} ${c.green(
      "running on the Potluck mesh",
    )}\n`,
  );
  stdout.write(`${c.dim(cfg.baseURL)}\n`);
  stdout.write(
    `${c.dim("type a task, or")} ${c.bold("/help")} ${c.dim("·")} ${c.bold(
      "/model",
    )} ${c.dim("·")} ${c.bold("/exit")}\n\n`,
  );
}

async function main(): Promise<void> {
  const args = parseArgs(process.argv.slice(2));

  if (args.help) {
    printHelp();
    return;
  }
  if (args.version) {
    stdout.write(`${VERSION}\n`);
    return;
  }

  const cfg = await loadConfig();
  try {
    assertGatewayReady(cfg);
  } catch (err) {
    if (err instanceof GatewayDisabledError) {
      stdout.write(`${c.yellow(err.message)}\n`);
      process.exitCode = 1;
      return;
    }
    throw err;
  }

  let model: string;
  try {
    model = await resolveModel(cfg, args.model);
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    stdout.write(
      `${c.red("Could not reach the Potluck gateway")} at ${cfg.baseURL}\n${c.dim(
        message,
      )}\nIs the Potluck app running with the gateway enabled?\n`,
    );
    process.exitCode = 1;
    return;
  }

  const cwd = process.cwd();
  const rl = createInterface({ input: stdin, output: stdout });
  let closed = false;
  rl.once("close", () => {
    closed = true;
  });
  // Resolves to null when stdin hits EOF / the readline closes (piped input
  // ending, Ctrl-D), so the loop can exit instead of calling question() on a
  // closed readline (ERR_USE_AFTER_CLOSE).
  const ask = (q: string): Promise<string | null> =>
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
  const confirm = makeConfirm((q) => ask(q).then((a) => a ?? "n"));

  const conversation: ChatMessage[] = [
    { role: "system", content: systemPrompt(cwd) },
  ];

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

  printBanner(model, cfg);

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
        signal: active.signal,
        callbacks: {
          onToken: (t) => stdout.write(t),
          onTurnEnd: () => stdout.write("\n"),
          onAction: (a) => stdout.write(`${renderAction(a.tool, a.args)}\n`),
          onToolResult: (r) => stdout.write(`${renderResultPreview(r)}\n`),
          onNotice: (m) => stdout.write(`${c.yellow(m)}\n`),
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
