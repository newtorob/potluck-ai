import { describe, expect, it } from "vitest";
import { roleMessages, roleResponseFormat, validateRole, validateRoleResult, type RoleJob } from "../src/agentHarnessRoles.js";

const job: RoleJob = { role: "omission-review-v3", output: { version: "potluck-omission-result-v3", maxWords: 100, maxFindings: 2 }, objective: "Review retention.", successCriteria: ["Keep expiry."] };
const header = { record: "answer", question: "What is retained?", verdict: "source_rule", selected_ids: ["R3", "R4"], covered_by_exact_binding_ids: ["R5"], rule: "Selected and retained units count as coverage." };
const source = { record: "source", id: "D1", headings: ["Archive"], retained_bindings: [{ start: 0, end: 13, quote: "Retained rule" }] };
const units = [
  { id: "R1", state: "supplied_only", text: "Records expire after thirty days." },
  { id: "R2", state: "supplied_only", text: "The archive is painted blue." },
  { id: "R3", state: "selected", text: "Records require approval." },
  { id: "R4", state: "selected", text: "Archives are monitored." },
  { id: "R5", state: "retained_binding", text: "Retained rule" },
];
const packet = (rows: unknown[] = [header, source, ...units]) => rows.map(row => JSON.stringify(row)).join("\n");
const sources = () => ({ "policy.jsonl": packet() });
const output = () => ({ status: "complete", assessments: [
  { file: "policy.jsonl", unit_id: "R1", decision: "omission", quote: "expire after thirty days", reason: "Expiry is missing.", covered_by: [] as string[] },
  { file: "policy.jsonl", unit_id: "R2", decision: "already_covered", quote: "painted blue", reason: "The answer covers it.", covered_by: ["R3"] },
], limitations: [] as string[] });
const validate = (value: unknown, data = sources(), contract = job) => validateRoleResult(JSON.stringify(value), contract, data);

describe("omission review v3 coverage references", () => {
  it("preserves assessments and normalizes only omissions; a valid reference does not prove semantic coverage", () => {
    const value = output(), result = validate(value);
    expect(result).toMatchObject({ assessments: value.assessments, findings: [{ detail: "Expiry is missing.", evidence: [{ file: "policy.jsonl", unit_id: "R1", quote: "expire after thirty days" }] }] });
    expect(validate(value)).toEqual(result);
  });
  it("accepts selected and retained references, including up to three distinct references", () => {
    const value = output(); value.assessments[1].covered_by = ["R3", "R4", "R5"];
    expect(validate(value)).toMatchObject({ assessments: value.assessments });
  });
  it.each([[], ["R3", "R3"], ["R1"], ["UNKNOWN"], ["R3", "R4", "R5", "R3"]])("rejects invalid coverage references %j", refs => {
    const value = output(); value.assessments[1].covered_by = refs;
    expect(() => validate(value)).toThrow();
  });
  it.each(["omission", "not_applicable", "prose"])("requires no references for %s", decision => {
    const value = output(); value.assessments[1].decision = decision;
    expect(() => validate(value)).toThrow();
    value.assessments[1].covered_by = []; expect(() => validate(value)).not.toThrow();
  });
  it("rejects a reference found only in a different packet", () => {
    const otherHeader = { ...header, selected_ids: ["OTHER"], covered_by_exact_binding_ids: [] };
    const other = packet([otherHeader, { ...source, retained_bindings: [] }, { id: "OTHER", state: "selected", text: "Different packet." }]);
    const value = output(); value.assessments[1].covered_by = ["OTHER"];
    expect(() => validate(value, { ...sources(), "other.jsonl": other })).toThrow();
  });
  it("rejects missing, duplicate, unknown or retained candidate assessments and bad quotes", () => {
    const missing = output(); missing.assessments.pop();
    const duplicate = output(); duplicate.assessments.push({ ...duplicate.assessments[0] });
    for (const value of [missing, duplicate]) expect(() => validate(value)).toThrow();
    for (const change of [{ unit_id: "R3", quote: units[2].text }, { unit_id: "UNKNOWN" }, { quote: units[1].text }, { decision: "unknown" }]) {
      const value = output(); Object.assign(value.assessments[0], change); expect(() => validate(value)).toThrow();
    }
  });
  it("enforces reason length, total words including references, and omission count", () => {
    const longReason = output(); longReason.assessments[0].reason = "one two three four five six seven";
    expect(() => validate(longReason)).toThrow();
    const maxReason = output(); maxReason.assessments[0].reason = "one two three four five six"; expect(() => validate(maxReason)).not.toThrow();
    const many = output(); many.assessments[1].decision = "omission"; many.assessments[1].covered_by = [];
    expect(() => validate(many, sources(), { ...job, output: { ...job.output, maxFindings: 1 } })).toThrow();
    const low = output(); low.assessments[1].covered_by = ["R3", "R4", "R5"];
    low.limitations = ["one two three four five six seven eight nine ten eleven"];
    const withoutRefs = structuredClone(low); withoutRefs.assessments[1].decision = "not_applicable"; withoutRefs.assessments[1].covered_by = [];
    const bound = { ...job, output: { ...job.output, maxWords: 32 } };
    expect(() => validate(withoutRefs, sources(), bound)).not.toThrow();
    expect(() => validate(low, sources(), bound)).toThrow();
  });
  it("rejects malformed JSON, duplicate keys, bad schema and insufficient without limitation", () => {
    expect(() => validateRoleResult('{"status":"complete","status":"complete","assessments":[],"limitations":[]}', job, sources())).toThrow();
    expect(() => validateRoleResult("not json", job, sources())).toThrow();
    const value = output(); value.status = "insufficient"; expect(() => validate(value)).toThrow();
    value.limitations.push("Draft incomplete."); expect(validate(value).status).toBe("insufficient");
    expect(() => validate({ ...output(), extra: true })).toThrow();
  });
  it("keeps versions paired and preserves the candidate cap", () => {
    expect(validateRole(job.role, job.output).role).toBe(job.role);
    expect(() => validateRole(job.role, { ...job.output, version: "potluck-omission-result-v2" })).toThrow();
    expect(validateRole("omission-review-v2", { ...job.output, version: "potluck-omission-result-v2" }).role).toBe("omission-review-v2");
    const candidates = Array.from({ length: 17 }, (_, i) => ({ id: `X${i}`, state: "supplied_only", text: `Rule ${i}.` }));
    const data = { "policy.jsonl": packet([{ ...header, selected_ids: [], covered_by_exact_binding_ids: [] }, { ...source, retained_bindings: [] }, ...candidates]) };
    expect(() => roleMessages(job, data)).toThrow(); expect(() => roleResponseFormat(job, data)).toThrow();
  });
});

import { mkdtemp, readFile, realpath, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach } from "vitest";
import { prepareHarness, runHarness, type HarnessPlan, type RunHarnessOptions } from "../src/agentHarness.js";
let root: string, hub: string, plan: HarnessPlan;
beforeEach(async () => {
  root = await realpath(await mkdtemp(join(tmpdir(), "potluck-omission-v3-"))); hub = join(root, "hub");
  await writeFile(join(root, "brain.json"), JSON.stringify({ version: "potluck-agent-brain-v1", project: "sample", revision: "1", updatedAt: new Date(Date.now() - 1000).toISOString(), expiresAt: new Date(Date.now() + 3600000).toISOString(), brief: "Review retention.", priorities: ["Keep expiry."], constraints: ["Read only"], provenance: ["Reviewed note"] }));
  await writeFile(join(root, "policy.jsonl"), packet());
  plan = { version: "potluck-agent-plan-v3", id: "omission-v3-test", brain: join(root, "brain.json"), root, model: "fixture", contextWindowTokens: 8192,
    limits: { concurrency: 1, maxSteps: 1, outputTokens: 700, timeoutMs: 2000, totalOutputTokens: 700 }, jobs: [{ id: "review", ...job, files: [{ path: "policy.jsonl", name: "policy.jsonl" }] }] };
});
afterEach(async () => { await rm(root, { recursive: true, force: true }); });
it.each([true, false])("mocked harness validates coverage reference (valid=%s)", async valid => {
  await prepareHarness(plan, hub); const value = output(); if (!valid) value.assessments[1].covered_by = ["R1"];
  const execute: NonNullable<RunHarnessOptions["execute"]> = async (_h, _t, o) => { o.callbacks.onToken(JSON.stringify(value)); return { status: "finished" }; };
  const result = await runHarness(hub, plan.id, { endpoint: "http://127.0.0.1:31777/v1", apiKey: "fixture", execute });
  expect(result.jobs[0].status).toBe(valid ? "needs_review" : "failed");
  const receipt = JSON.parse(await readFile(join(hub, "batches", plan.id, "jobs/review/receipt.json"), "utf8"));
  if (valid) expect(receipt).toMatchObject({ needsPrimaryReview: true, outputCheck: "passed_coverage_reference_checks_only", result: { assessments: value.assessments } });
  else expect(receipt.result).toBeUndefined();
});
