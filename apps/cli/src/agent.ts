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

  const args =
    typeof obj.args === "object" && obj.args !== null && !Array.isArray(obj.args)
      ? (obj.args as Record<string, unknown>)
      : {};

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
    "Tools:",
    '  read_file   {"path": string}                       read a UTF-8 file (auto-runs)',
    '  list_dir    {"path": string}                       list a directory (auto-runs)',
    '  write_file  {"path": string, "content": string}    create/overwrite a file (asks the user)',
    '  run_command {"command": string}                    run a shell command (asks the user)',
    "",
    "Rules:",
    "- Emit at most ONE action per turn; put the JSON inside the fence.",
    `- Paths are relative to the working directory: ${cwd}  (you cannot escape it).`,
    "- Read before you write. Make minimal, correct changes.",
    "- Keep prose short. Never fabricate tool output; wait for TOOL_RESULT.",
    "- The user must approve every write_file and run_command, so explain risky steps.",
  ].join("\n");
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
    for await (const token of streamChat(cfg, history, {
      model,
      signal: opts.signal,
    })) {
      assistant += token;
      callbacks.onToken(token);
    }
    callbacks.onTurnEnd();
    history.push({ role: "assistant", content: assistant });

    const parsed = parseAction(assistant);

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
