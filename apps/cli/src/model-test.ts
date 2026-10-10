/**
 * `potluck models test`: run the engine's answer check on demand, and the words `setup` uses for the check
 * it gets back from the same route. Everything printed is picked field by field from what the engine sent.
 */
import type { CliArgs } from "./args.js";
import { acceptanceFor, acceptanceLine, describeRanOn, label, parseAcceptance, parseInference, type Acceptance } from "./benchmark-stats.js";
import { ControlError } from "./control.js";
import { readSetupOptions, TEST_NOT_PASSED, testFailure, testRequest, validID } from "./model-setup.js";
import { RuntimeError, runtimeRequest } from "./runtime.js";

export interface ModelTestReport {
  schemaVersion: 1; action: "test"; model: string; status: "passed" | "failed";
  /** What was asked for. The level that ran is in `acceptance`. */
  level: "quick" | "full"; forced: boolean;
  durationMs: number | null; outputTokens: number | null;
  /** Null when the engine has no answer check. */
  acceptance: Acceptance | null;
  /** The engine's sentence when it refused the test. */
  message: string | null;
}
const count = (v: unknown): number | null => typeof v === "number" && Number.isFinite(v) && v >= 0 ? v : null;

/** `potluck models test [<id>] [--full] [--force]`. The model is the one loaded now unless one is named. */
export async function testModel(args: CliArgs): Promise<ModelTestReport> {
  let id = args.target;
  if (!id) {
    const loaded = (await readSetupOptions(args.timeout, "models test")).currentModel;
    if (!validID(loaded)) throw new RuntimeError("No model is loaded to test. Load one with potluck models load <model-id>.");
    id = loaded;
  }
  const level = args.full ? "full" : "quick", forced = args.force === true;
  const report: ModelTestReport = { schemaVersion: 1, action: "test", model: id, status: "failed", level, forced, durationMs: null, outputTokens: null, acceptance: null, message: null };
  let reply: Record<string, unknown>;
  try { reply = await testRequest(id, { level, force: forced }, args.timeout); }
  catch (error) {
    if (error instanceof ControlError && error.status === 503 && label(error.detail, 300)) {
      report.message = label(error.detail, 300);
      // The refusal carries only a sentence. The engine keeps the checks behind it, so ask for them, and use them only if they are this model's failure.
      try {
        const kept = acceptanceFor(parseInference(await runtimeRequest("/health/inference", { timeout: args.timeout })).acceptance, id);
        if (kept?.status === "failed") report.acceptance = kept;
      } catch { /* the sentence is enough */ }
      return report;
    }
    throw testFailure(error);
  }
  report.acceptance = parseAcceptance(reply.acceptance);
  report.durationMs = count(reply.durationMs); report.outputTokens = count(reply.outputTokens);
  if (reply.status === "passed" && reply.model === id && report.acceptance?.status !== "failed") report.status = "passed";
  else report.message = report.acceptance?.message ? null : TEST_NOT_PASSED;
  return report;
}

export function formatModelTest(r: ModelTestReport): string {
  const a = r.acceptance;
  // "passed" is only said when the engine checked the answers. An engine without the check, or with it turned off, tested one reply.
  const checked = a?.status === "passed" || a?.status === "failed";
  const lines = [checked || r.status === "failed" ? `${r.model}: answer check ${r.status}` : `${r.model}: replied to a test prompt`];
  if (a && checked) {
    const day = a.checkedAt?.slice(0, 10);
    lines.push(a.fromProof ? `Reused an earlier result${day ? ` from ${day}` : ""}; nothing ran now. Add --force to run it again.` : `Ran now${a.level ? `, ${a.level} check` : ""}.`);
    const where = describeRanOn(a.ranOn, true);
    if (where) lines.push(`Ran on: ${where}`);
    if (a.ranOn?.driver) lines.push(`Driver: ${a.ranOn.driver}`);
    if (a.checks.length) lines.push("Checks:", ...a.checks.map(c => `  ${c.id}: ${c.passed ? "passed" : "failed"}, ${c.required ? "required" : "not required"}`));
  } else if (a) {
    lines.push(acceptanceLine(a) ?? "The engine has no answer check result for this model yet.");
  } else if (r.status === "passed") lines.push("This engine does not report an answer check, so only one reply was tested.");
  const message = a?.message ?? r.message;
  if (message) lines.push(message);
  return lines.join("\n");
}
