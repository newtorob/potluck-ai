/**
 * The prompt-based ReAct loop.
 *
 * The gateway has no function-calling, so the protocol lives entirely in text:
 * the assistant replies with EITHER a final prose answer, OR exactly one action
 * as a ```action fenced block. After running the tool the CLI appends a user
 * message `TOOL_RESULT: <result>` and continues. The loop ends when the
 * assistant replies with no action block.
 */

import type { GatewayConfig } from "./config.js";
import { streamChat, type ChatMessage } from "./gateway.js";
import { runTool, type Confirm } from "./tools.js";

export interface ParsedAction {
  tool: string;
  args: Record<string, unknown>;
}

export type ParseResult =
  | { kind: "final"; text: string }
  | { kind: "action"; action: ParsedAction }
  | { kind: "error"; message: string };

const ACTION_BLOCK = /```[ \t]*action[^\n]*\n([\s\S]*?)```/;

/** End offset (exclusive) of the first complete ```action block, or -1 if none. */
function actionBlockEnd(text: string): number {
  const m = ACTION_BLOCK.exec(text);
  return m ? m.index + m[0].length : -1;
}

/**
 * Parse one assistant message. Tolerates prose around the block. No block means
 * a final answer; a malformed block becomes an `error` so the loop can feed the
 * reason back and let the model retry.
 */
export function parseAction(text: string): ParseResult {
  const match = ACTION_BLOCK.exec(text);
  if (!match) {
    return { kind: "final", text: text.trim() };
  }

  const json = match[1].trim();
  let parsed: unknown;
  try {
    parsed = JSON.parse(json);
  } catch (err) {
    const reason = err instanceof Error ? err.message : String(err);
    return { kind: "error", message: `invalid JSON in action block: ${reason}` };
  }

  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    return { kind: "error", message: "action must be a JSON object" };
  }

  const obj = parsed as Record<string, unknown>;
  if (typeof obj.tool !== "string") {
    return { kind: "error", message: 'action requires a string "tool" field' };
  }

  // Prefer an explicit args/arguments object. Weaker local models often inline
  // the args at the top level instead (e.g. {"tool":"run_command","command":"..."}),
  // so fall back to treating any remaining top-level keys as the args.
  const explicit = obj.args ?? obj.arguments;
  let args: Record<string, unknown>;
  if (typeof explicit === "object" && explicit !== null && !Array.isArray(explicit)) {
    args = explicit as Record<string, unknown>;
  } else {
    const { tool: _tool, args: _args, arguments: _arguments, ...rest } = obj;
    void _tool;
    void _args;
    void _arguments;
    args = rest;
  }

  return { kind: "action", action: { tool: obj.tool, args } };
}

/** Build the system prompt that defines the strict action protocol. */
export function systemPrompt(cwd: string): string {
  return [
    "You are Potluck, a terminal coding agent. Your inference runs on the user's",
    "own machines across the Potluck mesh. Be precise, concise, and safe.",
    "",
    "You operate in a strict action loop. On each turn reply with EITHER:",
    "  1. A final answer in plain prose (when the task is done), OR",
    "  2. EXACTLY ONE action as a fenced block and nothing else of substance:",
    "",
    "```action",
    '{"tool": "read_file", "args": {"path": "src/foo.ts"}}',
    "```",
    "",
    "After an action, the CLI runs it and replies with one user message starting",
    "`TOOL_RESULT:` that contains the result. Read it, then continue the loop.",
    "When you are done, reply with a final answer and NO action block.",
    "",
    "CRITICAL: emit your action block and then STOP -- write nothing after it.",
    "NEVER write a line starting with `TOOL_RESULT` yourself, and NEVER invent,",
    "predict, or guess what a tool returns. The CLI runs the tool and gives you the",
    "real result; wait for it.",
    "",
    "Tools:",
    '  search_files {"query": string, "path"?: string, "regex"?: bool}  search file contents (auto-runs)',
    '  glob         {"pattern": string}                   find files by name, e.g. "**/*.ts" (auto-runs)',
    '  read_file    {"path": string}                      read a UTF-8 file (auto-runs)',
    '  list_dir     {"path": string}                      list a directory (auto-runs)',
    '  edit_file    {"path": string, "old_string": string, "new_string": string}  targeted edit to an EXISTING file (asks the user)',
    '  write_file   {"path": string, "content": string}   create a NEW file / full overwrite (asks the user)',
    '  run_command  {"command": string}                   run a shell command (asks the user)',
    "",
    "Workflow:",
    "- Locate code with search_files (by content) and glob (by filename) first.",
    "- read_file to inspect before changing anything.",
    "- To change an existing file, use edit_file with a SMALL, UNIQUE old_string",
    "  (include enough surrounding context that it matches exactly one place).",
    "- Only use write_file to CREATE a new file (or when a full rewrite is truly",
    "  warranted). Prefer minimal, targeted edits over rewrites.",
    "",
    "Rules:",
    "- Emit at most ONE action per turn; put the JSON inside the fence.",
    `- Paths are relative to the working directory: ${cwd}  (you cannot escape it).`,
    "- Read before you edit. Make minimal, correct changes.",
    "- Keep prose short. Never fabricate tool output; wait for TOOL_RESULT.",
    "- The user must approve every edit_file, write_file, and run_command, so",
    "  explain risky steps.",
  ].join("\n");
}

/**
 * Keep a long conversation under an approximate character budget so it doesn't
 * blow the model's context window. The system prompt and the most recent turns
 * are always kept; older turns are dropped and replaced by a short marker.
 * Returns a NEW array (the caller keeps the full history for follow-ups).
 */
const DEFAULT_CHAR_BUDGET = 48_000; // ~12k tokens at ~4 chars/token

export function trimHistory(
  history: ChatMessage[],
  budget: number = DEFAULT_CHAR_BUDGET,
): ChatMessage[] {
  if (history.length === 0) return history;
  const total = history.reduce((n, m) => n + m.content.length, 0);
  if (total <= budget) return history;

  const hasSystem = history[0]?.role === "system";
  const system = hasSystem ? history.slice(0, 1) : [];
  const rest = history.slice(system.length);

  let used = system.reduce((n, m) => n + m.content.length, 0);
  const kept: ChatMessage[] = [];
  for (let i = rest.length - 1; i >= 0; i--) {
    const len = rest[i].content.length;
    // Always keep at least the most recent message, even if it alone is large.
    if (kept.length > 0 && used + len > budget) break;
    kept.unshift(rest[i]);
    used += len;
  }

  const dropped = rest.length - kept.length;
  if (dropped <= 0) return history;
  const marker: ChatMessage = {
    role: "user",
    content: `[${dropped} earlier message(s) trimmed to fit the context window]`,
  };
  return [...system, marker, ...kept];
}

export interface AgentCallbacks {
  /** A streamed content delta from the assistant. */
  onToken: (token: string) => void;
  /** A turn finished streaming (assistant produced a full message). */
  onTurnEnd: () => void;
  /** An action was parsed and is about to run. */
  onAction: (action: ParsedAction) => void;
  /** A tool returned (the string fed back as TOOL_RESULT). */
  onToolResult: (result: string) => void;
  /** A non-fatal notice (e.g. step limit hit). */
  onNotice?: (message: string) => void;
}

export interface AgentOptions {
  cfg: GatewayConfig;
  model: string;
  cwd: string;
  confirm: Confirm;
  callbacks: AgentCallbacks;
  signal?: AbortSignal;
  maxSteps?: number;
  /** Routing scope forwarded to the gateway (X-Potluck-Scope). */
  scope?: string;
  /** Approximate character budget before older turns are trimmed. */
  charBudget?: number;
}

/**
 * Run one task to completion. Mutates `history` in place (the caller owns it so
 * follow-ups keep context, and can truncate it if the turn is cancelled).
 */
export async function runTask(
  history: ChatMessage[],
  task: string,
  opts: AgentOptions,
): Promise<void> {
  const { cfg, model, cwd, confirm, callbacks } = opts;
  const maxSteps = opts.maxSteps ?? 24;

  history.push({ role: "user", content: task });

  for (let step = 0; step < maxSteps; step++) {
    let assistant = "";
    // Send a trimmed view so long sessions stay under the context window; the
    // caller keeps the full history untouched for follow-ups and cancel-restore.
    const view = trimHistory(history, opts.charBudget);
    for await (const token of streamChat(cfg, view, {
      model,
      scope: opts.scope,
      signal: opts.signal,
    })) {
      assistant += token;
      callbacks.onToken(token);
      // Stop the moment one complete action block is emitted. Weaker local models
      // tend to keep generating after it -- hallucinating a TOOL_RESULT or drifting
      // into invented output. Breaking closes the stream so they can't.
      if (actionBlockEnd(assistant) !== -1) break;
    }
    callbacks.onTurnEnd();

    const parsed = parseAction(assistant);
    // Store only up to the action block (discard any post-action hallucination);
    // a final answer (no block) is stored whole.
    const end = actionBlockEnd(assistant);
    history.push({
      role: "assistant",
      content: end === -1 ? assistant : assistant.slice(0, end),
    });

    if (parsed.kind === "final") {
      return;
    }

    if (parsed.kind === "error") {
      const result = `error: ${parsed.message}`;
      callbacks.onToolResult(result);
      history.push({ role: "user", content: `TOOL_RESULT: ${result}` });
      continue;
    }

    callbacks.onAction(parsed.action);
    const result = await runTool(parsed.action.tool, parsed.action.args, {
      cwd,
      confirm,
    });
    callbacks.onToolResult(result);
    history.push({ role: "user", content: `TOOL_RESULT: ${result}` });
  }

  callbacks.onNotice?.(
    `Reached the ${maxSteps}-step limit for this task. Ask a follow-up to continue.`,
  );
}
