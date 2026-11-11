import { createHash } from "node:crypto";
import { relative } from "node:path";
import type { ChatMessage } from "./gateway.js";
import type { FileReadObservation } from "./tools.js";
import type { FailureEvidence } from "./repair-feedback.js";

interface Evidence {
  read: FileReadObservation;
  message: ChatMessage;
  lines: Map<number, string>;
  score: number;
  retain: boolean;
}

/** Observed ranges only: retention never grants permission to overwrite a file. */
export class SourceContext {
  private evidence: Evidence[] = [];
  private redundantReads = 0;
  private readonly words: Set<string>;
  private failure?: FailureEvidence;

  constructor(private cwd: string, private task: string) {
    this.words = new Set(task.match(/[A-Za-z_$][\w$]{2,}/g) ?? []);
  }

  clear(): void { this.evidence = []; this.redundantReads = 0; }

  focus(failure?: FailureEvidence): void { this.failure = failure; }

  symbols(): string[] {
    return [...new Set([...(this.failure?.symbols ?? []), ...this.words])].slice(0, 100);
  }

  private failureScore(item: Evidence): number {
    if (!this.failure) return 0;
    const path = relative(this.cwd, item.read.path).replaceAll("\\", "/");
    const end = item.read.startLine + item.lines.size;
    const frame = this.failure.locations.some(location => {
      const file = location.path.replaceAll("\\", "/");
      return (file === path || file === item.read.path.replaceAll("\\", "/") || file.endsWith("/" + path)) &&
        location.line >= item.read.startLine && location.line < end;
    });
    const symbols = this.failure.symbols.some(name => item.read.navigation?.some(location => location.name === name &&
      location.line >= item.read.startLine && location.line < end));
    return Number(frame) * 300 + Number(symbols) * 200;
  }

  afterCommand(): void {
    // Commands can mutate any file. Stop retaining stale source, but an actual
    // reread can still prove that its returned lines are unchanged.
    for (const item of this.evidence) item.retain = false;
  }

  invalidate(path: string): void {
    this.evidence = this.evidence.filter(item => item.read.path !== path);
    this.redundantReads = 0;
  }

  private lines(read: FileReadObservation): Map<number, string> {
    // A truncated line cannot prove equality with a complete source line.
    if (read.truncatedLine) return new Map();
    const lines = read.content.match(/[^\n]*\n|[^\n]+$/g) ?? [];
    return new Map(lines.map((line, index) => [read.startLine + index,
      createHash("sha256").update(line).digest("hex")]));
  }

  assess(read: FileReadObservation, visible: Set<ChatMessage>): { feedback: string; stalled: boolean } {
    const lines = this.lines(read);
    const previous = this.evidence.filter(item => item.read.path === read.path);
    // A changed shared line invalidates older observations of the whole file.
    if (previous.some(item => [...lines].some(([line, hash]) => item.lines.has(line) && item.lines.get(line) !== hash))) {
      this.invalidate(read.path);
      return { feedback: "", stalled: false };
    }
    const retained = previous.filter(item => visible.has(item.message));
    const covered = [...lines].filter(([line, hash]) => retained.some(item => item.lines.get(line) === hash)).length;
    const redundant = lines.size > 0 && covered === lines.size;
    this.redundantReads = redundant ? this.redundantReads + 1 : 0;
    if (redundant) return {
      feedback: "\nREAD RECOVERY: every returned line is unchanged and already present in your current context. " +
        "Use that source for a small targeted edit if you have enough evidence. Otherwise identify the specific missing fact and inspect a different range or caller; do not guess or repeat this read.",
      stalled: this.redundantReads >= 2,
    };
    if (lines.size > 0 && covered / lines.size >= 0.8) return {
      feedback: `\nREAD RECOVERY: ${covered} of ${lines.size} returned lines are unchanged and already in context. Focus on the new lines and the specific missing fact before reading more.`,
      stalled: false,
    };
    return { feedback: "", stalled: false };
  }

  remember(read: FileReadObservation, message: ChatMessage): void {
    const path = relative(this.cwd, read.path).replaceAll("\\", "/");
    const mentionedPath = this.task.replaceAll("\\", "/").includes(path);
    const declarations = [...read.content.matchAll(/^\s*(?:(?:export|default|async)\s+)*(?:def|class|function|fn|func)\s+([A-Za-z_$][\w$]*)/gm)];
    const namedDeclaration = declarations.some(match => this.words.has(match[1]));
    const score = Number(namedDeclaration) * 100 + Number(mentionedPath) * 20;
    // Keep several ranges from a file; an unrelated newer read must not evict
    // the task-named declaration just because both share the same path.
    this.evidence = this.evidence.filter(item => item.read.path !== read.path ||
      item.read.startLine !== read.startLine || item.read.content !== read.content);
    this.evidence.push({ read, message, lines: this.lines(read), score, retain: true });
    if (this.evidence.length > 32) {
      const lowest = Math.min(...this.evidence.map(item => item.score));
      this.evidence.splice(this.evidence.findIndex(item => item.score === lowest), 1);
    }
  }

  priorities(activePath?: string): ChatMessage[] {
    return this.evidence.filter(item => item.retain).map((item, index) => ({ item, index,
      score: item.score + this.failureScore(item) + Number(item.read.path === activePath) * 10 }))
      .sort((a, b) => b.score - a.score || b.index - a.index)
      .slice(0, 6).map(({ item }) => item.message);
  }
}

export function turnBudgetGuidance(remaining: number, total: number, hasFixedCheck: boolean): string {
  const check = hasFixedCheck
    ? "A successful file change automatically runs the fixed check. Leave a turn to inspect its result."
    : "Leave time to check any change before claiming completion.";
  const focus = remaining <= Math.max(3, Math.ceil(total / 2))
    ? " If this task needs an edit and you have enough evidence, make a small targeted change now. Otherwise identify the missing fact or report the blocker. Do not make speculative edits just to use the budget."
    : "";
  return `\n\nTURN BUDGET: ${remaining} model turns remain, including this one. ${check}${focus}`;
}
