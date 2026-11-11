import { mkdtemp, readFile, realpath, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { roleMessages, roleResponseFormat, validateRole, validateRoleResult, type RoleJob } from "../src/agentHarnessRoles.js";
import { prepareHarness, runHarness, type HarnessPlan, type RunHarnessOptions } from "../src/agentHarness.js";
const job: RoleJob = { role: "scope-review-v3", output: { version: "potluck-scope-result-v3", maxWords: 100, maxFindings: 2 }, objective: "Review retention.", successCriteria: ["Keep expiry."] };
const old: RoleJob = { ...job, role: "scope-review-v2", output: { ...job.output, version: "potluck-scope-result-v2" } };
const units = [{ id: "Q1", state: "supplied_only", text: "Archives expire after thirty days." }, { id: "Q2", state: "selected", text: "Managers approve archive access." }];
const packet = (selected = ["Q2"], body = units) => [
  { record: "answer", question: "How long are archives retained?", verdict: "source_rule", selected_ids: selected, covered_by_exact_binding_ids: [], rule: "Selected units count as retained." },
  { record: "source", id: "D1", headings: ["Archive policy"], retained_bindings: [] }, ...body,
].map(r => JSON.stringify(r)).join("\n");
const sources = { "archive.jsonl": packet(), "@brain": "Context one" };
const output = { status: "complete", assessments: [{ unit_id: "Q1", applicability: "governing", reason: "Sets expiry." }, { unit_id: "Q2", applicability: "different_situation", reason: "Access only." }], limitations: [] };

describe("scope review v3", () => {
  it("pairs the role and version", () => {
    expect(validateRole(job.role, job.output).role).toBe(job.role);
    expect(() => validateRole(job.role, old.output)).toThrow();
    expect(() => validateRole(old.role, job.output)).toThrow();
  });
  it("changes generic guidance while preserving the schema and v2 behavior", () => {
    const v2 = roleMessages(old, sources);
    expect(roleMessages(job, sources).history[0].content).not.toEqual(v2.history[0].content);
    expect(roleMessages(old, sources)).toEqual(v2);
    expect(roleResponseFormat(job, sources)).toEqual(roleResponseFormat(old, sources));
    expect(validateRoleResult(JSON.stringify(output), job, sources)).toEqual(validateRoleResult(JSON.stringify(output), old, sources));
  });
  it("remains blind to retention, filename, assignment and brain", () => {
    const changed = units.map(u => u.id === "Q1" ? { ...u, state: "selected" } : u);
    const other = { "renamed.jsonl": packet(["Q1", "Q2"], changed), "@brain": "Other context" };
    const otherJob = { ...job, objective: "Different objective", successCriteria: ["Different criteria"] };
    expect(roleMessages(job, sources)).toEqual(roleMessages(otherJob, other));
    expect(roleResponseFormat(job, sources)).toEqual(roleResponseFormat(otherJob, other));
    expect(validateRoleResult(JSON.stringify(output), job, sources)).toMatchObject({ scopeAssessments: output.assessments, findings: [{ evidence: [{ file: "archive.jsonl", unit_id: "Q1", quote: units[0].text }] }] });
    expect(validateRoleResult(JSON.stringify(output), otherJob, other)).toMatchObject({ scopeAssessments: output.assessments, findings: [] });
  });
});
let root: string, hub: string, plan: HarnessPlan;
beforeEach(async () => {
  root = await realpath(await mkdtemp(join(tmpdir(), "potluck-scope-v3-"))); hub = join(root, "hub");
  await writeFile(join(root, "brain.json"), JSON.stringify({ version: "potluck-agent-brain-v1", project: "sample", revision: "1", updatedAt: new Date(Date.now() - 1000).toISOString(), expiresAt: new Date(Date.now() + 3600000).toISOString(), brief: "Review retention.", priorities: ["Keep expiry."], constraints: ["Read only"], provenance: ["Reviewed note"] }));
  await writeFile(join(root, "archive.jsonl"), packet());
  plan = { version: "potluck-agent-plan-v3", id: "scope-v3-test", brain: join(root, "brain.json"), root, model: "fixture", contextWindowTokens: 8192, limits: { concurrency: 1, maxSteps: 1, outputTokens: 700, timeoutMs: 2000, totalOutputTokens: 700 }, jobs: [{ id: "review", ...job, files: [{ path: "archive.jsonl", name: "archive.jsonl" }] }] };
});
afterEach(async () => { await rm(root, { recursive: true, force: true }); });
it("records the scope and coverage checks in a mocked run", async () => {
  await prepareHarness(plan, hub);
  const execute: NonNullable<RunHarnessOptions["execute"]> = async (_h, _t, o) => { o.callbacks.onToken(JSON.stringify(output)); return { status: "finished" }; };
  const result = await runHarness(hub, plan.id, { endpoint: "http://127.0.0.1:31777/v1", apiKey: "fixture", execute });
  expect(result.jobs[0].status).toBe("needs_review");
  const receipt = JSON.parse(await readFile(join(hub, "batches", plan.id, "jobs/review/receipt.json"), "utf8"));
  expect(receipt).toMatchObject({ needsPrimaryReview: true, outputCheck: "passed_scope_and_coverage_checks_only", result: { scopeAssessments: output.assessments } });
});
