import { describe, expect, it } from "vitest";
import { roleMessages, roleResponseFormat, validateRole, validateRoleResult, type RoleJob } from "../src/agentHarnessRoles.js";

const job: RoleJob = { role: "omission-review-v2", output: { version: "potluck-omission-result-v2", maxWords: 100, maxFindings: 1 }, objective: "Review retention policy.", successCriteria: ["Keep expiry rules."] };
const header = { record: "answer", question: "What is retained?", verdict: "source_rule", selected_ids: ["P3"], covered_by_exact_binding_ids: [], rule: "Only selected units and exact retained bindings count as coverage." };
const source = { record: "source", id: "D1", headings: ["Retention"], retained_bindings: [] };
const units = [
  { id: "P1", state: "supplied_only", text: "Archive records expire after thirty days." },
  { id: "P2", state: "supplied_only", text: "The archive is painted blue." },
  { id: "P3", state: "selected", text: "Managers approve access." },
];
const packet = (rows: unknown[] = [header, source, ...units]) => rows.map(row => JSON.stringify(row)).join("\n");
const sources = (rows?: unknown[]) => ({ "policy.jsonl": packet(rows) });
const output = () => ({ status: "complete", assessments: [
  { file: "policy.jsonl", unit_id: "P1", decision: "omission", quote: "expire after thirty days", reason: "Expiry is missing." },
  { file: "policy.jsonl", unit_id: "P2", decision: "not_needed", quote: "painted blue", reason: "Color is irrelevant." },
], limitations: [] as string[] });
const validate = (value: unknown, data = sources()) => validateRoleResult(JSON.stringify(value), job, data);

describe("exhaustive omission review v2", () => {
  it("preserves every assessment while normalizing only omissions to findings", () => {
    const value = output(), result = validate(value);
    expect(result).toMatchObject({ status: "complete", assessments: value.assessments, findings: [{ detail: "Expiry is missing.", evidence: [{ file: "policy.jsonl", unit_id: "P1", quote: "expire after thirty days" }] }] });
    expect(validate(value)).toEqual(result);
  });
  it("accepts zero omissions only after assessing every candidate", () => {
    const value = output(); value.assessments[0].decision = "not_needed";
    expect(validate(value)).toMatchObject({ status: "complete", findings: [], assessments: value.assessments });
  });
  it("accepts an empty complete assessment when there are no candidates", () => {
    const data = sources([{ ...header, selected_ids: ["P3"] }, source, units[2]]);
    expect(validate({ status: "complete", assessments: [], limitations: [] }, data)).toMatchObject({ status: "complete", findings: [], assessments: [] });
  });
  it("rejects missing, duplicate and extra assessments", () => {
    const missing = output(); missing.assessments.pop();
    const duplicate = output(); duplicate.assessments.push({ ...duplicate.assessments[0] });
    const extra = output(); extra.assessments.push({ ...extra.assessments[0], unit_id: "P3", quote: units[2].text });
    for (const value of [missing, duplicate, extra]) expect(() => validate(value)).toThrow();
  });
  it("rejects illegal decisions, wrong unit quotes and unbound references", () => {
    for (const change of [
      { decision: "maybe" }, { quote: units[1].text }, { unit_id: "P3", quote: units[2].text },
      { unit_id: "UNKNOWN" }, { file: "@brain" }, { file: "unknown.jsonl" },
    ]) {
      const value = output(); Object.assign(value.assessments[0], change);
      expect(() => validate(value, { ...sources(), "@brain": packet() })).toThrow();
    }
  });
  it("limits omission decisions and counts text in every assessment", () => {
    const tooMany = output(); tooMany.assessments[1].decision = "omission";
    expect(() => validate(tooMany)).toThrow();
    const tooLong = output(); tooLong.assessments[1].reason = "word ".repeat(101);
    expect(() => validate(tooLong)).toThrow();
  });
  it("requires a limitation for insufficient output and still requires full coverage", () => {
    const value = output(); value.status = "insufficient";
    expect(() => validate(value)).toThrow();
    value.limitations.push("The draft was incomplete.");
    expect(validate(value).status).toBe("insufficient");
    value.assessments.pop(); expect(() => validate(value)).toThrow();
  });
  it("rejects more than sixteen candidates before constructing prompt or schema", () => {
    const candidates = Array.from({ length: 17 }, (_, i) => ({ id: `X${i}`, state: "supplied_only", text: `Distinct rule number ${i}.` }));
    const data = sources([header, source, ...candidates, units[2]]);
    expect(() => roleMessages(job, data)).toThrow();
    expect(() => roleResponseFormat(job, data)).toThrow();
    expect(() => validate(output(), data)).toThrow();
    const atLimit = sources([header, source, ...candidates.slice(0, 16), units[2]]);
    expect(() => roleMessages(job, atLimit)).not.toThrow();
    expect(() => roleResponseFormat(job, atLimit)).not.toThrow();
    const tooSmall = { ...job, output: { ...job.output, maxWords: 32 } };
    expect(() => roleMessages(tooSmall, atLimit)).toThrow();
    expect(() => roleResponseFormat(tooSmall, atLimit)).toThrow();
  });
  it("preserves the explicitly versioned v1 contract", () => {
    expect(validateRole("omission-review-v1", { ...job.output, version: "potluck-omission-result-v1" }).role).toBe("omission-review-v1");
    expect(() => validateRole(job.role, { ...job.output, version: "potluck-omission-result-v1" })).toThrow();
  });
});

import { mkdtemp, readFile, realpath, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach } from "vitest";
import { prepareHarness, runHarness, type HarnessPlan, type RunHarnessOptions } from "../src/agentHarness.js";
let root: string, hub: string, plan: HarnessPlan;
beforeEach(async () => {
  root = await realpath(await mkdtemp(join(tmpdir(), "potluck-omission-v2-"))); hub = join(root, "hub");
  await writeFile(join(root, "brain.json"), JSON.stringify({ version: "potluck-agent-brain-v1", project: "sample", revision: "1", updatedAt: new Date(Date.now() - 1000).toISOString(), expiresAt: new Date(Date.now() + 3600000).toISOString(), brief: "Review retention rules.", priorities: ["Retain expiry."], constraints: ["Read only"], provenance: ["Reviewed note"] }));
  await writeFile(join(root, "policy.jsonl"), packet());
  plan = { version: "potluck-agent-plan-v3", id: "omission-v2-test", brain: join(root, "brain.json"), root, model: "fixture", contextWindowTokens: 8192,
    limits: { concurrency: 1, maxSteps: 1, outputTokens: 700, timeoutMs: 2000, totalOutputTokens: 700 },
    jobs: [{ id: "review", ...job, files: [{ path: "policy.jsonl", name: "policy.jsonl" }] }] };
});
afterEach(async () => { await rm(root, { recursive: true, force: true }); });
it.each([true, false])("mocked harness routes complete coverage=%s", async complete => {
  await prepareHarness(plan, hub);
  const value = output(); if (!complete) value.assessments.pop();
  const execute: NonNullable<RunHarnessOptions["execute"]> = async (_h, _t, o) => { o.callbacks.onToken(JSON.stringify(value)); return { status: "finished" }; };
  const result = await runHarness(hub, plan.id, { endpoint: "http://127.0.0.1:31777/v1", apiKey: "fixture", execute });
  expect(result.jobs[0].status).toBe(complete ? "needs_review" : "failed");
  const receipt = JSON.parse(await readFile(join(hub, "batches", plan.id, "jobs/review/receipt.json"), "utf8"));
  if (complete) expect(receipt).toMatchObject({ needsPrimaryReview: true, outputCheck: "passed_candidate_coverage_checks_only", result: { assessments: value.assessments } });
  else expect(receipt.result).toBeUndefined();
});
