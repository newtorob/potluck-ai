import { mkdtemp, readFile, realpath, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { roleMessages, roleResponseFormat, validateRole, validateRoleResult, type RoleJob } from "../src/agentHarnessRoles.js";
import { prepareHarness, runHarness, type HarnessPlan, type RunHarnessOptions } from "../src/agentHarness.js";

const job: RoleJob = { role: "scope-review-v1", output: { version: "potluck-scope-result-v1", maxWords: 100, maxFindings: 2 }, objective: "Review retention.", successCriteria: ["Keep expiry."] };
const units = [
  { id: "N1", state: "supplied_only", text: "Archives expire after thirty days. " },
  { id: "N2", state: "selected", text: "Managers approve archive access." },
  { id: "N3", state: "supplied_only", text: "The archive is painted blue." },
];
const packet = (selected = ["N2"], body = units) => [
  { record: "answer", question: "How long are archives retained?", verdict: "source_rule", selected_ids: selected, covered_by_exact_binding_ids: [], rule: "Only selected units count as retained." },
  { record: "source", id: "A1", headings: ["Archive policy"], retained_bindings: [] },
  ...body,
].map(r => JSON.stringify(r)).join("\n");
const sources = () => ({ "archive.jsonl": packet(), "@brain": "Untrusted context one" });
const output = () => ({ status: "complete", assessments: [
  { unit_id: "N1", applicability: "governing", reason: "Sets expiry." },
  { unit_id: "N2", applicability: "different_situation", reason: "Access only." },
  { unit_id: "N3", applicability: "illustration", reason: "Color only." },
], limitations: [] as string[] });
const validate = (value: unknown, data = sources(), contract = job) => validateRoleResult(JSON.stringify(value), contract, data);

describe("state-blind scope review", () => {
  it("keeps inference input and schema identical across retention, filename, assignment and brain changes", () => {
    const changedUnits = units.map(u => u.id === "N1" ? { ...u, state: "selected" } : u);
    const other = { "renamed.jsonl": packet(["N1", "N2"], changedUnits), "@brain": "Different untrusted context" };
    const otherJob = { ...job, objective: "An unrelated assignment.", successCriteria: ["Other criteria."] };
    expect(roleMessages(job, sources())).toEqual(roleMessages(otherJob, other));
    expect(roleResponseFormat(job, sources())).toEqual(roleResponseFormat(otherJob, other));
    const raw = output();
    expect(validate(raw)).toMatchObject({ scopeAssessments: raw.assessments, findings: [{ evidence: [{ file: "archive.jsonl", unit_id: "N1", quote: "Archives expire after thirty days." }] }] });
    expect(validate(raw, other, otherJob)).toMatchObject({ scopeAssessments: raw.assessments, findings: [] });
  });
  it("requires every unit exactly once, valid fields and applicability", () => {
    const missing = output(); missing.assessments.pop();
    const duplicate = output(); duplicate.assessments.push({ ...duplicate.assessments[0] });
    const unknown = output(); unknown.assessments[0].unit_id = "UNKNOWN";
    const invalid = output(); invalid.assessments[0].applicability = "maybe";
    const extra = output(); Object.assign(extra.assessments[0], { extra: true });
    for (const value of [missing, duplicate, unknown, invalid, extra]) expect(() => validate(value)).toThrow();
    expect(() => validateRoleResult('{"status":"complete","status":"complete","assessments":[],"limitations":[]}', job, sources())).toThrow();
  });
  it("requires unresolved assessments to be insufficient with a limitation", () => {
    const value = output(); value.assessments[0].applicability = "unresolved";
    expect(() => validate(value)).toThrow(); value.status = "insufficient"; expect(() => validate(value)).toThrow();
    value.limitations.push("Scope is unclear."); expect(validate(value)).toMatchObject({ status: "insufficient", scopeAssessments: value.assessments });
  });
  it("enforces reason, total word and derived omission bounds", () => {
    const value = output(); value.assessments[0].reason = "one two three four five"; expect(() => validate(value)).toThrow();
    value.assessments[0].reason = "one two three four"; expect(() => validate(value)).not.toThrow();
    value.limitations = ["word ".repeat(101)]; expect(() => validate(value)).toThrow();
    const multiple = output(); multiple.assessments[2].applicability = "governing";
    expect(() => validate(multiple, sources(), { ...job, output: { ...job.output, maxFindings: 1 } })).toThrow();
  });
  it("enforces one packet, sixteen units and unit byte limits before inference", () => {
    expect(() => roleMessages(job, { ...sources(), "second.jsonl": packet() })).toThrow();
    const many = Array.from({ length: 17 }, (_, i) => ({ id: `X${i}`, state: "supplied_only", text: `Rule ${i}.` }));
    expect(() => roleMessages(job, { "archive.jsonl": packet([], many) })).toThrow();
    const large = [{ id: "X", state: "supplied_only", text: "界".repeat(342) }];
    expect(() => roleMessages(job, { "archive.jsonl": packet([], large) })).toThrow();
    expect(() => roleResponseFormat(job, { "archive.jsonl": packet([], large) })).toThrow();
  });
  it("pairs the role with its output version", () => {
    expect(validateRole(job.role, job.output).role).toBe(job.role);
    expect(() => validateRole(job.role, { ...job.output, version: "potluck-omission-result-v5" })).toThrow();
  });
});

let root: string, hub: string, plan: HarnessPlan;
beforeEach(async () => {
  root = await realpath(await mkdtemp(join(tmpdir(), "potluck-scope-"))); hub = join(root, "hub");
  await writeFile(join(root, "brain.json"), JSON.stringify({ version: "potluck-agent-brain-v1", project: "sample", revision: "1", updatedAt: new Date(Date.now() - 1000).toISOString(), expiresAt: new Date(Date.now() + 3600000).toISOString(), brief: "Review retention.", priorities: ["Keep expiry."], constraints: ["Read only"], provenance: ["Reviewed note"] }));
  await writeFile(join(root, "archive.jsonl"), packet());
  plan = { version: "potluck-agent-plan-v3", id: "scope-test", brain: join(root, "brain.json"), root, model: "fixture", contextWindowTokens: 8192,
    limits: { concurrency: 1, maxSteps: 1, outputTokens: 700, timeoutMs: 2000, totalOutputTokens: 700 }, jobs: [{ id: "review", ...job, files: [{ path: "archive.jsonl", name: "archive.jsonl" }] }] };
});
afterEach(async () => { await rm(root, { recursive: true, force: true }); });
it("routes a mocked scope result to a review receipt", async () => {
  await prepareHarness(plan, hub); const value = output();
  const execute: NonNullable<RunHarnessOptions["execute"]> = async (_h, _t, o) => { o.callbacks.onToken(JSON.stringify(value)); return { status: "finished" }; };
  const result = await runHarness(hub, plan.id, { endpoint: "http://127.0.0.1:31777/v1", apiKey: "fixture", execute });
  expect(result.jobs[0].status).toBe("needs_review");
  const receipt = JSON.parse(await readFile(join(hub, "batches", plan.id, "jobs/review/receipt.json"), "utf8"));
  expect(receipt).toMatchObject({ needsPrimaryReview: true, outputCheck: "passed_scope_and_coverage_checks_only", result: { scopeAssessments: value.assessments } });
});
