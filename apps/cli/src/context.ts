import type { ChatMessage } from "./gateway.js";

const FALLBACK_CONTEXT_TOKENS = 4096;
const MESSAGE_OVERHEAD = 16;
const TEMPLATE_MARGIN = 128;
const HISTORY_MARKER = "[Earlier messages trimmed to fit the context window.]";
const FEEDBACK_MARKER = "\n[Tool output truncated to fit context. This is a partial view; use read_file offset/limit or search_files. Never rewrite a file from this excerpt.]\n";

export class ContextBudgetError extends Error {
  constructor() {
    super("Task incomplete: the system instructions, current request, or latest action cannot fit the available context. Use a model with a larger context window, shorten the request/project brief or project instructions (AGENTS.md), or ask for smaller targeted edits.");
    this.name = "ContextBudgetError";
  }
}

export interface ContextOptions {
  /** Complete rendered prompt, including tools/template, on the selected route. */
  measureTokens?: (messages: ChatMessage[], maxOutputTokens: number) => Promise<number | undefined>;
  signal?: AbortSignal;
  /** Recent relevant source observations, in most-useful-first order. */
  priorityMessages?: ChatMessage[];
  /** The selected runtime's actual context limit, not its advertised maximum. */
  contextWindowTokens?: number;
  maxOutputTokens?: number;
  /** An additional hard cap on message characters, for existing callers. */
  charBudget?: number;
  /** Exact user-request position, so literal TOOL_RESULT text is not misclassified. */
  currentTaskIndex?: number;
  /** Tool definitions are part of the model input, even though outside messages. */
  toolDefinitionTokens?: number;
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
    sum + Buffer.byteLength(message.content, "utf8") + metadataCost(message, text => Buffer.byteLength(text, "utf8")) + MESSAGE_OVERHEAD, 0);
}

const isFeedback = (message: ChatMessage): boolean =>
  message.role === "tool" || message.role === "user" && message.content.startsWith("TOOL_RESULT:");

function metadataCost(message: ChatMessage, size: (text: string) => number): number {
  return (message.tool_calls ? size(JSON.stringify(message.tool_calls)) : 0) +
    (message.tool_call_id ? size(message.tool_call_id) : 0);
}

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
  messageOverhead: number, currentTaskIndex?: number, priorityMessages: ChatMessage[] = [],
): ChatMessage[] {
  const cost = (messages: ChatMessage[]): number => messages.reduce((sum, message) =>
    sum + size(message.content) + metadataCost(message, size) + messageOverhead, 0);
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
  const order = [...new Set([
    groups.length - 1,
    ...priorityMessages.map(message => groups.findIndex(group => group.some(item => item.message === message))),
    ...groups.map((_, index) => index).reverse(),
  ])].filter(index => index >= 0);
  for (const index of order) {
    let group = groups[index];
    if (cost([...pinned, ...group.map(item => item.message), ...recent.map(item => item.message)]) > budget) {
      // Earlier tasks are optional context. A long previous final response
      // must not prevent a fitting new request, and dropping an older action
      // also drops its feedback instead of leaving an orphan result.
      if (group[group.length - 1].index < taskIndex) continue;
      if (recent.length > 0) {
        if (priorityMessages.length > 0) continue;
        break;
      }
      const fixed = group.filter(item => !isFeedback(item.message));
      const feedback = group.filter(item => isFeedback(item.message));
      const room = budget - cost([...pinned, ...fixed.map(item => item.message)]) - feedback.length * messageOverhead -
        feedback.reduce((sum, item) => sum + metadataCost(item.message, size), 0);
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
  const toolTokens = options.toolDefinitionTokens ?? 0;
  if (!Number.isSafeInteger(toolTokens) || toolTokens < 0) throw new ContextBudgetError();
  let budget = window - maxOutputTokens - TEMPLATE_MARGIN - toolTokens;
  if (options.charBudget !== undefined) {
    if (!positiveInteger(options.charBudget)) throw new ContextBudgetError();
    budget = Math.min(budget, options.charBudget);
  }
  const messages = compactHistory(history, budget, text => Buffer.byteLength(text, "utf8"), MESSAGE_OVERHEAD, options.currentTaskIndex, options.priorityMessages);
  return { messages, maxOutputTokens, compacted: messages !== history, usedFallback };
}

class TokenCountUnavailable extends Error {}

export async function prepareContextMeasured(history: ChatMessage[], options: ContextOptions = {}): Promise<PreparedContext> {
  try { return await prepareMeasured(history, options); }
  catch (error) {
    options.signal?.throwIfAborted();
    if (!(error instanceof TokenCountUnavailable)) throw error;
    return prepareContext(history, options);
  }
}

async function prepareMeasured(
  history: ChatMessage[], options: ContextOptions = {},
): Promise<PreparedContext> {
  options.signal?.throwIfAborted();
  if (!options.measureTokens) return prepareContext(history, options);
  const usedFallback = !positiveInteger(options.contextWindowTokens);
  const window = usedFallback ? FALLBACK_CONTEXT_TOKENS : options.contextWindowTokens!;
  const defaultOutput = Math.min(4096, Math.max(1, Math.floor(window / 4)));
  const maxOutputTokens = positiveInteger(options.maxOutputTokens)
    ? Math.min(options.maxOutputTokens, defaultOutput) : defaultOutput;
  // The callback includes the template, so no estimated template margin or
  // per-message byte overhead is subtracted a second time.
  const inputBudget = window - maxOutputTokens;
  if (inputBudget < 1 || (options.charBudget !== undefined && !positiveInteger(options.charBudget))) {
    throw new ContextBudgetError();
  }
  if (options.currentTaskIndex !== undefined && (!Number.isInteger(options.currentTaskIndex) ||
      options.currentTaskIndex < 0 || history[options.currentTaskIndex]?.role !== "user")) {
    throw new ContextBudgetError();
  }

  let measurements = 0;
  const fits = async (messages: ChatMessage[]): Promise<boolean> => {
    options.signal?.throwIfAborted();
    // Logarithmic searches below normally take fewer than thirty calls. Bound
    // the work even for pathological histories/tokenizers; never loop forever.
    if (++measurements > 64) throw new ContextBudgetError();
    const tokens = await options.measureTokens!(messages, maxOutputTokens);
    if (tokens === undefined) throw new TokenCountUnavailable();
    options.signal?.throwIfAborted();
    if (!Number.isSafeInteger(tokens) || tokens < 0) {
      throw new Error("The model tokenizer returned an invalid token count.");
    }
    return tokens <= inputBudget && (options.charBudget === undefined ||
      messages.reduce((sum, message) => sum + message.content.length + metadataCost(message, text => text.length), 0) <= options.charBudget);
  };
  const prepared = (messages: ChatMessage[]): PreparedContext =>
    ({ messages, maxOutputTokens, compacted: messages !== history, usedFallback });
  if (await fits(history)) return prepared(history);

  let taskIndex = options.currentTaskIndex ?? -1;
  if (options.currentTaskIndex === undefined) {
    for (let index = history.length - 1; index >= 0; index--) {
      if (history[index].role === "user" && !isFeedback(history[index])) {
        taskIndex = index;
        break;
      }
    }
  }
  const pinned = new Set<number>();
  const groups: number[][] = [];
  for (let index = 0; index < history.length; index++) {
    const message = history[index];
    if (message.role === "system" || index === taskIndex) {
      pinned.add(index);
    } else if (isFeedback(message)) {
      const previous = groups.at(-1);
      if (!previous || history[previous[0]].role !== "assistant" || previous.at(-1) !== index - 1) {
        throw new ContextBudgetError();
      }
      previous.push(index);
    } else {
      groups.push([index]);
    }
  }
  // Current-run actions are indivisible with their feedback. The caller keeps
  // authoritative verification/failure state in the latest feedback, so never
  // discard that group in favour of old file reads. Historical exchanges from
  // before a new request may be dropped, including oversized old final prose.
  const latest = groups.at(-1);
  const required = latest && latest.at(-1)! > taskIndex ? latest : [];
  for (const index of required) pinned.add(index);
  const optional = required.length ? groups.slice(0, -1) : groups;
  const marker: ChatMessage = { role: "user", content: HISTORY_MARKER };
  const render = (drop: number, replacement?: { index: number; message: ChatMessage }): ChatMessage[] => {
    const kept = new Set(pinned);
    for (const group of optional.slice(drop)) for (const index of group) kept.add(index);
    const messages = history.flatMap((message, index) => kept.has(index)
      ? [replacement?.index === index ? replacement.message : message] : []);
    const firstNonSystem = messages.findIndex(message => message.role !== "system");
    messages.splice(firstNonSystem < 0 ? messages.length : firstNonSystem, 0, marker);
    return messages;
  };

  let fitting: ChatMessage[] | undefined;
  const chosen = new Set<number>();
  const ordered = [...new Set([
    ...(options.priorityMessages ?? []).map(message => optional.findIndex(group => group.some(i => history[i] === message))).filter(i => i >= 0),
    ...optional.map((_, i) => i).reverse(),
  ])];
  const selected = () => {
    const kept = new Set(pinned);
    for (const i of chosen) for (const index of optional[i]) kept.add(index);
    const messages = history.filter((_, i) => kept.has(i));
    const first = messages.findIndex(m => m.role !== "system");
    messages.splice(first < 0 ? messages.length : first, 0, marker);
    return messages;
  };
  // Keep current action/results first, then active source, then recent history.
  // Every accepted view is actually measured; token counts need not be additive.
  const minimum = selected();
  if (await fits(minimum)) {
    fitting = minimum;
    for (const i of ordered.slice(0, 48)) {
      chosen.add(i);
      const candidate = selected();
      if (await fits(candidate)) fitting = candidate;
      else chosen.delete(i);
    }
    if (!(await fits(fitting))) throw new ContextBudgetError();
    return prepared(fitting);
  }

  // All optional history is gone. Only latest tool feedback may be excerpted;
  // system instructions, the request and the action are never silently cut.
  const feedbackIndex = required.at(-1);
  if (feedbackIndex === undefined || !isFeedback(history[feedbackIndex])) throw new ContextBudgetError();
  const feedback = history[feedbackIndex];
  const points = Array.from(feedback.content);
  const excerpt = (count: number): ChatMessage[] => {
    const head = Math.ceil(count * 0.6);
    const tail = count - head;
    return render(optional.length, { index: feedbackIndex, message: {
      ...feedback, content: points.slice(0, head).join("") + FEEDBACK_MARKER +
        (tail ? points.slice(-tail).join("") : ""),
    } });
  };
  // Retain the TOOL_RESULT prefix and real evidence on both sides. A marker
  // with no useful observation would encourage the model to invent its input.
  let low = 80;
  let high = points.length - 1;
  if (high < low) throw new ContextBudgetError();
  fitting = excerpt(low);
  if (!(await fits(fitting))) throw new ContextBudgetError();
  while (low < high) {
    const middle = Math.ceil((low + high) / 2);
    const candidate = excerpt(middle);
    if (await fits(candidate)) {
      low = middle;
      fitting = candidate;
    } else {
      high = middle - 1;
    }
  }
  if (!(await fits(fitting))) throw new ContextBudgetError();
  return prepared(fitting);
}
