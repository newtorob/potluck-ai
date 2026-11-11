import { mkdtemp, readFile, realpath, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { roleMessages, roleResponseFormat, validateRole, validateRoleResult, type RoleJob } from "../src/agentHarnessRoles.js";
import { prepareHarness, runHarness, type HarnessPlan, type RunHarnessOptions } from "../src/agentHarness.js";

const job: RoleJob = { role: "omission-review-v4", output: { version: "potluck-omission-result-v4", maxWords: 100, maxFindings: 2 }, objective: "Review retention.", successCriteria: ["Keep expiry."] };
const old: RoleJob = { ...job, role: "omission-review-v3", output: { ...job.output, version: "potluck-omission-result-v3" } };
const records = [
  { record: "answer", question: "What is retained?", verdict: "source_rule", selected_ids: ["K3"], covered_by_exact_binding_ids: [], rule: "Selected units count as coverage." },
  { record: "source", id: "D1", headings: ["Archives"], retained_bindings: [] },
  { id: "K1", state: "supplied_only", text: "Archives expire after thirty days." },
  { id: "K2", state: "supplied_only", text: "The archive is painted blue." },
  { id: "K3", state: "selected", text: "Managers approve archive access." },
];
const packet = records.map(r => JSON.stringify(r)).join("\n");
const sources = { "archive.jsonl": packet };
const output = () => ({ status: "complete", assessments: [
  { file: "archive.jsonl", unit_id: "K1", decision: "omission", quote: "expire after thirty days", reason: "Expiry is missing.", covered_by: [] as string[] },
  { file: "archive.jsonl", unit_id: "K2", decision: "already_covered", quote: "painted blue", reason: "The answer covers it.", covered_by: ["K3"] },
], limitations: [] as string[] });
const validate = (value: unknown, contract = job) => validateRoleResult(JSON.stringify(value), contract, sources);

describe("omission review v4 version boundary", () => {
  it("registers only with its matching output version", () => {
    expect(validateRole(job.role, job.output).role).toBe(job.role);
    expect(() => validateRole(job.role, old.output)).toThrow();
    expect(() => validateRole(old.role, job.output)).toThrow();
    expect(validateRole(old.role, old.output).role).toBe(old.role);
  });
  it("retains the v3 schema while refining the role instructions", () => {
    expect(roleResponseFormat(job, sources)).toEqual(roleResponseFormat(old, sources));
    expect(roleMessages(job, sources).history[0].content).not.toEqual(roleMessages(old, sources).history[0].content);
    expect(roleMessages(job, sources).task).toEqual(roleMessages(old, sources).task);
  });
  it("requires exact fields and complete candidate coverage", () => {
    const value = output(); expect(validate(value)).toMatchObject({ assessments: value.assessments, findings: [{ detail: "Expiry is missing." }] });
    const missing = output(); missing.assessments.pop(); expect(() => validate(missing)).toThrow();
    const extra = output(); Object.assign(extra.assessments[0], { unexpected: true }); expect(() => validate(extra)).toThrow();
    const duplicate = output(); duplicate.assessments[1].unit_id = "K1"; expect(() => validate(duplicate)).toThrow();
  });
  it("checks reference state and six-word reason limit", () => {
    const value = output(); value.assessments[1].covered_by = ["K1"]; expect(() => validate(value)).toThrow();
    value.assessments[1].covered_by = ["K3"]; value.assessments[0].reason = "one two three four five six seven";
    expect(() => validate(value)).toThrow();
    value.assessments[0].reason = "one two three four five six"; expect(() => validate(value)).not.toThrow();
  });
});

let root: string, hub: string, plan: HarnessPlan;
beforeEach(async () => {
  root = await realpath(await mkdtemp(join(tmpdir(), "potluck-omission-v4-"))); hub = join(root, "hub");
  await writeFile(join(root, "brain.json"), JSON.stringify({ version: "potluck-agent-brain-v1", project: "sample", revision: "1", updatedAt: new Date(Date.now() - 1000).toISOString(), expiresAt: new Date(Date.now() + 3600000).toISOString(), brief: "Review retention.", priorities: ["Keep expiry."], constraints: ["Read only"], provenance: ["Reviewed note"] }));
  await writeFile(join(root, "archive.jsonl"), packet);
  plan = { version: "potluck-agent-plan-v3", id: "omission-v4-test", brain: join(root, "brain.json"), root, model: "fixture", contextWindowTokens: 8192,
    limits: { concurrency: 1, maxSteps: 1, outputTokens: 700, timeoutMs: 2000, totalOutputTokens: 700 }, jobs: [{ id: "review", ...job, files: [{ path: "archive.jsonl", name: "archive.jsonl" }] }] };
});
afterEach(async () => { await rm(root, { recursive: true, force: true }); });
it("uses the v4 coverage-reference receipt for a mocked worker", async () => {
  await prepareHarness(plan, hub); const value = output();
  const execute: NonNullable<RunHarnessOptions["execute"]> = async (_h, _t, o) => { o.callbacks.onToken(JSON.stringify(value)); return { status: "finished" }; };
  const result = await runHarness(hub, plan.id, { endpoint: "http://127.0.0.1:31777/v1", apiKey: "fixture", execute });
  expect(result.jobs[0].status).toBe("needs_review");
  const receipt = JSON.parse(await readFile(join(hub, "batches", plan.id, "jobs/review/receipt.json"), "utf8"));
  expect(receipt).toMatchObject({ needsPrimaryReview: true, outputCheck: "passed_coverage_reference_checks_only", result: { assessments: value.assessments } });
});
