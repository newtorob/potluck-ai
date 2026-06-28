/**
 * Terminal rendering: ANSI helpers, the default y/N confirm gate, and small
 * render helpers for actions and results. Color is off when stdout is not a TTY
 * or NO_COLOR is set.
 */

import type { Confirm, ConfirmContext } from "./tools.js";

const USE_COLOR = process.stdout.isTTY === true && !process.env.NO_COLOR;

function wrap(open: number, close: number): (s: string) => string {
  if (!USE_COLOR) return (s) => s;
  return (s) => `[${open}m${s}[${close}m`;
}

export const c = {
  bold: wrap(1, 22),
  dim: wrap(2, 22),
  red: wrap(31, 39),
  green: wrap(32, 39),
  yellow: wrap(33, 39),
  blue: wrap(34, 39),
  cyan: wrap(36, 39),
  gray: wrap(90, 39),
};

/** Colorize a unified diff by line prefix (headers/+/-/context). */
export function renderDiff(diff: string): string {
  return diff
    .split("\n")
    .map((line) => {
      if (line.startsWith("+++") || line.startsWith("---")) return c.bold(line);
      if (line.startsWith("+")) return c.green(line);
      if (line.startsWith("-")) return c.red(line);
      return c.dim(line);
    })
    .join("\n");
}

/**
 * Build the default interactive confirm gate from a question function (so the
 * single readline interface is shared with the REPL). Shows a diff for
 * write_file and the command for run_command, then asks y/N (default No).
 */
export function makeConfirm(ask: (q: string) => Promise<string>): Confirm {
  return async (ctx: ConfirmContext): Promise<boolean> => {
    if (ctx.tool === "write_file") {
      process.stdout.write(`\n${c.yellow("● write_file")} ${c.bold(ctx.subject)}\n`);
      process.stdout.write(`${renderDiff(ctx.summary)}\n`);
      const answer = await ask(c.yellow("Apply this write? [y/N] "));
      return /^y(es)?$/i.test(answer.trim());
    }

    process.stdout.write(`\n${c.yellow("● run_command")}\n`);
    process.stdout.write(`${c.cyan(`$ ${ctx.summary}`)}\n`);
    const answer = await ask(c.yellow("Run this command? [y/N] "));
    return /^y(es)?$/i.test(answer.trim());
  };
}

/** One-line summary of an action about to run. */
export function renderAction(tool: string, args: Record<string, unknown>): string {
  const detail =
    typeof args.path === "string"
      ? args.path
      : typeof args.command === "string"
        ? args.command
        : "";
  return `${c.cyan("▶")} ${c.bold(tool)} ${c.dim(detail)}`;
}

/** A short, dimmed preview of a tool result (read-only tools auto-run). */
export function renderResultPreview(result: string): string {
  const firstLine = result.split("\n")[0] ?? "";
  const lineCount = result.split("\n").length;
  const head = firstLine.length > 100 ? `${firstLine.slice(0, 100)}…` : firstLine;
  const suffix = lineCount > 1 ? c.dim(` (+${lineCount - 1} more lines)`) : "";
  return `${c.gray("  ↳")} ${c.dim(head)}${suffix}`;
}
