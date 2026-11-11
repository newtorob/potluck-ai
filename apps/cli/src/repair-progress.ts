const EDITS = new Set(["edit_file", "write_file"]);

/** An explicit repair task must decide before spending every turn inspecting. */
export class RepairProgress {
  private inspections = 0;
  private allowance: number;
  private rejected = 0;
  changed = false;
  private failure?: string;
  private repeatedFailure = false;
  private failedCheck = false;

  pendingCheck(): void {
    this.failure = undefined;
    this.failedCheck = false;
    this.repeatedFailure = false;
  }

  checked(ok: boolean, fingerprint: string): void {
    this.repeatedFailure = !ok && this.failure === fingerprint;
    this.failure = ok ? undefined : fingerprint;
    this.failedCheck = !ok;
  }

  constructor(maxSteps: number) {
    this.allowance = Math.min(4, Math.max(1, maxSteps - 3));
  }

  get deciding(): boolean { return this.inspections >= this.allowance; }
  allows(tool: string): boolean { return !this.deciding || EDITS.has(tool); }
  reject(): boolean { return ++this.rejected >= 2; }

  observe(tool: string, changed: boolean): void {
    if (changed) {
      this.changed = true;
      this.inspections = 0;
      this.allowance = 2;
      this.rejected = 0;
    } else if (!EDITS.has(tool)) {
      // Failed inspections and shell commands also consume the allowance.
      // Neither can masquerade as a verified source change to refill it.
      this.inspections++;
    }
  }

  guidance(): string {
    const state = this.deciding
      ? "DECISION STAGE: investigation is closed. Only edit_file/write_file are available. Use the source already observed for a small justified repair. If evidence is insufficient, give a final answer naming the missing fact and why it prevents an edit; this ends incomplete. Do not guess, repeat a read or substitute a shell command."
      : `INVESTIGATION STAGE: ${this.allowance - this.inspections} inspection actions remain before a decision. Read the likely owning function and necessary caller/test; avoid broad exploration. Edit as soon as the evidence supports a repair.`;
    const recovery = this.failedCheck
      ? " CORRECTION: the fixed check failed" + (this.repeatedFailure ? " with the same failure evidence" : "") +
        ". Use the literal failing input/assertion and expected/actual evidence in the latest result. " +
        "Identify which observed function or branch controls that behavior. If the edited wrapper delegates, inspect the named callee using source navigation or a focused search. " +
        "Correct the cause with a minimal edit while preserving neighboring inputs and independent parameters. Keep the tests and required behavior unchanged; a nearby edit or an explanation is not proof."
      : "";
    return "BOUNDED REPAIR: this task requires an actual source change and the fixed check to pass. " + state + recovery;
  }
}
