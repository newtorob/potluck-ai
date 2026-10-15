import type { ChatMessage } from "./gateway.js";

const FALLBACK_CONTEXT_TOKENS = 4096;
const MESSAGE_OVERHEAD = 16;
const TEMPLATE_MARGIN = 128;
const HISTORY_MARKER = "[Earlier messages trimmed to fit the context window.]";
const FEEDBACK_MARKER = "\n[Tool output truncated to fit context. This is a partial view; use read_file offset/limit or search_files. Never rewrite a file from this excerpt.]\n";

export class ContextBudgetError extends Error {
  constructor() {
    super("Task incomplete: the system instructions, current request, or latest action cannot fit the available context. Use a model with a larger context window, shorten the request/project brief, or ask for smaller targeted edits.");
    this.name = "ContextBudgetError";
  }
}

export interface ContextOptions {
  /** The selected runtime's actual context limit, not its advertised maximum. */
  contextWindowTokens?: number;
  maxOutputTokens?: number;
  /** An additional hard cap on message characters, for existing callers. */
  charBudget?: number;
  /** Exact user-request position, so literal TOOL_RESULT text is not misclassified. */
  currentTaskIndex?: number;
}

export interface PreparedContext {
  messages: ChatMessage[];
  maxOutputTokens: number;
  compacted: boolean;
  usedFallback: boolean;
}

function positiveInteger(value: number | undefined): value is number {
  return value !== undefined && Number.isSafeInteger(value) && value > 0;
}

/**
 * Conservative sizing without a model tokenizer: one UTF-8 byte per token,
 * plus message/template headroom. This is an estimate, not an exact token
 * count; in particular, a four-characters-per-token assumption is unsafe for
 * code and non-Latin text. The server remains authoritative for token limits.
 */
export function estimateContextTokens(messages: ChatMessage[]): number {
  return TEMPLATE_MARGIN + messages.reduce((sum, message) =>
    sum + Buffer.byteLength(message.content, "utf8") + MESSAGE_OVERHEAD, 0);
}

const isFeedback = (message: ChatMessage): boolean =>
  message.role === "user" && message.content.startsWith("TOOL_RESULT:");

/** Slice only between Unicode code points, even when the budget is in bytes. */
function take(text: string, budget: number, size: (text: string) => number, tail = false): string {
  const points = Array.from(text);
  let used = 0;
  const kept: string[] = [];
  for (let index = tail ? points.length - 1 : 0;
    index >= 0 && index < points.length; index += tail ? -1 : 1) {
    const point = points[index];
    const next = size(point);
    if (used + next > budget) break;
    used += next;
    kept.push(point);
  }
  return tail ? kept.reverse().join("") : kept.join("");
}

function boundFeedback(message: ChatMessage, budget: number, size: (text: string) => number): ChatMessage {
  if (size(message.content) <= budget) return message;
  const remaining = budget - size(FEEDBACK_MARKER);
  // Keep the TOOL_RESULT prefix and some real evidence. A marker alone cannot
  // support another action; stop instead of asking the model to guess.
  if (remaining < 80) throw new ContextBudgetError();
  const head = take(message.content, Math.ceil(remaining * 0.6), size);
  const tail = take(message.content, Math.floor(remaining * 0.4), size, true);
  return { ...message, content: head + FEEDBACK_MARKER + tail };
}

/**
 * Pin instructions and the current task. Keep recent assistant/feedback pairs
 * together, so a tool result never loses the action that produced it. The
 * latest feedback may be excerpted, but instructions, requests and actions are
 * never silently cut. The caller retains its complete history for follow-ups.
 */
function compactHistory(
  history: ChatMessage[], budget: number, size: (text: string) => number,
  messageOverhead: number, currentTaskIndex?: number,
): ChatMessage[] {
  const cost = (messages: ChatMessage[]): number => messages.reduce((sum, message) =>
    sum + size(message.content) + messageOverhead, 0);
  if (currentTaskIndex !== undefined && (!Number.isInteger(currentTaskIndex) ||
      currentTaskIndex < 0 || history[currentTaskIndex]?.role !== "user")) throw new ContextBudgetError();
  if (cost(history) <= budget) return history;
  if (!Number.isFinite(budget) || budget <= 0) throw new ContextBudgetError();

  let taskIndex = currentTaskIndex ?? -1;
  if (currentTaskIndex === undefined) {
    for (let index = history.length - 1; index >= 0; index--) {
      if (history[index].role === "user" && !isFeedback(history[index])) {
        taskIndex = index;
        break;
      }
    }
  }
  const instructions = history.filter(message => message.role === "system");
  const task = taskIndex >= 0 ? [history[taskIndex]] : [];
  const marker: ChatMessage = { role: "user", content: HISTORY_MARKER };
  const pinned = [...instructions, marker, ...task];
  if (cost(pinned) > budget) throw new ContextBudgetError();

  type IndexedMessage = { index: number; message: ChatMessage };
  const groups: IndexedMessage[][] = [];
  for (let index = 0; index < history.length; index++) {
    const message = history[index];
    if (message.role === "system" || index === taskIndex) continue;
    if (isFeedback(message)) {
      const previous = groups[groups.length - 1];
      if (!previous || previous[0].message.role !== "assistant" ||
          previous[previous.length - 1].index !== index - 1) throw new ContextBudgetError();
      previous.push({ index, message });
    } else {
      groups.push([{ index, message }]);
    }
  }

  let recent: IndexedMessage[] = [];
  for (let index = groups.length - 1; index >= 0; index--) {
    let group = groups[index];
    if (cost([...pinned, ...group.map(item => item.message), ...recent.map(item => item.message)]) > budget) {
      // Earlier tasks are optional context. A long previous final response
      // must not prevent a fitting new request, and dropping an older action
      // also drops its feedback instead of leaving an orphan result.
      if (group[group.length - 1].index < taskIndex) continue;
      if (recent.length > 0) break;
      const fixed = group.filter(item => !isFeedback(item.message));
      const feedback = group.filter(item => isFeedback(item.message));
      const room = budget - cost([...pinned, ...fixed.map(item => item.message)]) - feedback.length * messageOverhead;
      if (feedback.length !== 1 || room < 0) throw new ContextBudgetError();
      group = [fixed[0], { ...feedback[0], message: boundFeedback(feedback[0].message, room, size) }];
    }
    recent = [...group, ...recent];
  }
  if (taskIndex >= 0) recent.push({ index: taskIndex, message: history[taskIndex] });
  recent.sort((a, b) => a.index - b.index);
  const messages = [...instructions, marker, ...recent.map(item => item.message)];
  if (cost(messages) > budget) throw new ContextBudgetError();
  return messages;
}

/** Legacy character-budget entry point; now enforces the cap or fails clearly. */
export function trimHistory(history: ChatMessage[], budget = 48_000): ChatMessage[] {
  return compactHistory(history, budget, text => text.length, 0);
}

export function prepareContext(history: ChatMessage[], options: ContextOptions = {}): PreparedContext {
  const usedFallback = !positiveInteger(options.contextWindowTokens);
  const window = usedFallback ? FALLBACK_CONTEXT_TOKENS : options.contextWindowTokens!;
  const defaultOutput = Math.min(4096, Math.max(1, Math.floor(window / 4)));
  const maxOutputTokens = positiveInteger(options.maxOutputTokens)
    ? Math.min(options.maxOutputTokens, defaultOutput) : defaultOutput;
  let budget = window - maxOutputTokens - TEMPLATE_MARGIN;
  if (options.charBudget !== undefined) {
    if (!positiveInteger(options.charBudget)) throw new ContextBudgetError();
    budget = Math.min(budget, options.charBudget);
  }
  const messages = compactHistory(history, budget, text => Buffer.byteLength(text, "utf8"), MESSAGE_OVERHEAD, options.currentTaskIndex);
  return { messages, maxOutputTokens, compacted: messages !== history, usedFallback };
}
