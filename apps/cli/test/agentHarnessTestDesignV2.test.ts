import { mkdtemp, readFile, realpath, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, expect, it, vi } from "vitest";
import { prepareHarness, runHarness, type HarnessPlan, type RunHarnessOptions } from "../src/agentHarness.js";
import { roleMessages, roleResponseFormat, validateRole, validateRoleResult, type RoleJob } from "../src/agentHarnessRoles.js";

const job: RoleJob = { role: "test-design-v2", output: { version: "potluck-test-design-result-v2", maxWords: 50, maxFindings: 2 }, objective: "Design a test", successCriteria: ["Check the boundary"] };
const source = "First paragraph.\nContinues exactly.\n\nSecond paragraph keeps punctuation and 900.";
const sources = { "source.md": source };
const units = () => JSON.parse(roleMessages(job, sources).task).paragraphs as Array<{id:string;file:string;text:string}>;
const output = (ids: string[]) => JSON.stringify({ status: "complete", summary: "Proposed test.", findings: [{ detail: "Check boundary and expected rejection.", evidence_ids: ids }], limitations: [] });

it("public role materializes exact source paragraphs without generated quotation", () => {
  expect(validateRole(job.role, job.output).role).toBe(job.role);
  const p = units(); expect(p.map(u => u.text)).toEqual(["First paragraph.\nContinues exactly.", "Second paragraph keeps punctuation and 900."]);
  const result = validateRoleResult(output([p[1].id]), job, sources);
  expect(result.findings[0].evidence[0]).toEqual({ file: "source.md", quote: p[1].text, unit_id: p[1].id });
  const format = roleResponseFormat(job, sources); expect(JSON.stringify(format)).toContain(p[0].id);
});

it("rejects foreign, duplicate, stale, malformed and excessive output without quote repair", () => {
  const p = units();
  expect(() => validateRoleResult(output(["foreign"]), job, sources)).toThrow("unbound_reference");
  expect(() => validateRoleResult(output([p[0].id, p[0].id]), job, sources)).toThrow("unbound_reference");
  const repeated = JSON.parse(output([p[0].id])); repeated.findings.push({ ...repeated.findings[0], detail: "Check a distinct second boundary." });
  expect(validateRoleResult(JSON.stringify(repeated), job, sources).findings).toHaveLength(2);
  expect(() => validateRoleResult(output([p[0].id]), job, { "source.md": source + " changed" })).toThrow("unbound_reference");
  expect(() => validateRoleResult(output([p[0].id]), job, { "other.md": source })).toThrow("unbound_reference");
  expect(() => validateRoleResult(output([p[0].id]) + "{}", job, sources)).toThrow("output_json");
  const x = JSON.parse(output([p[0].id])); x.findings[0].detail = "word ".repeat(51);
  expect(() => validateRoleResult(JSON.stringify(x), job, sources)).toThrow("word_limit");
  expect(() => roleMessages(job, { "bad.md": "bad\0data" })).toThrow("source_invalid");
  const huge = { "source.md": "x".repeat(32769) }; const id = JSON.parse(roleMessages(job, huge).task).paragraphs[0].id;
  expect(() => validateRoleResult(output([id]), job, huge)).toThrow("evidence_bytes");
});

it("preserves v1 rejection of invented punctuation and numeric quotation", () => {
  const old = { ...job, role: "test-design-v1" as const, output: { ...job.output, version: "potluck-agent-result-v1" as const } };
  const existing = { "a.md": "Reject partial source catalogs and display overflow without trimming. The limit is 900." };
  for (const quote of ["Reject partial source catalogs.", "The limit is 990."]) {
    const raw = JSON.stringify({ status: "complete", summary: "Test.", findings: [{ detail: "Check limit.", evidence: [{ file: "a.md", quote }] }], limitations: [] });
    expect(() => validateRoleResult(raw, old, existing)).toThrow("output_unbound_quote");
  }
});

let root = "";
afterEach(async () => { if (root) await rm(root, { recursive: true, force: true }); root = ""; });
it("actual harness saves bound result, resumes without calls, and rejects stale source", async () => {
  root = await realpath(await mkdtemp(join(tmpdir(), "test-design-v2-"))); const hub = join(root, "hub");
  const brain = { version: "potluck-agent-brain-v1", project: "sample", revision: "1", updatedAt: new Date(Date.now()-1000).toISOString(), expiresAt: new Date(Date.now()+3600000).toISOString(), brief: "Preserve evidence.", priorities: ["Retain timing."], constraints: ["Read only"], provenance: ["Reviewed note"] };
  await writeFile(join(root, "brain.json"), JSON.stringify(brain)); await writeFile(join(root, "input.md"), source);
  const plan: HarnessPlan = { version: "potluck-agent-plan-v3", id: "batch", brain: join(root, "brain.json"), root, model: "fixture", contextWindowTokens: 8192,
    limits: { concurrency: 1, maxSteps: 1, outputTokens: 700, timeoutMs: 2000, totalOutputTokens: 700 }, jobs: [{ id: "review", ...job, files: [{ path: "input.md", name: "source.md" }] }] };
  const execute = vi.fn<NonNullable<RunHarnessOptions["execute"]>>(async (_history, task, options) => {
    const paragraph = JSON.parse(task).paragraphs.find((p: {file:string}) => p.file === "source.md");
    const reply = JSON.parse(output([paragraph.id])); reply.findings.push({ detail: "Check a second boundary.", evidence_ids: [paragraph.id] });
    options.callbacks.onToken(JSON.stringify(reply)); return { status: "finished" };
  });
  const opts = { endpoint: "http://127.0.0.1:31777/v1", apiKey: "fixture", execute };
  await prepareHarness(plan, hub); expect((await runHarness(hub, plan.id, opts)).jobs[0].status).toBe("needs_review");
  const receipt = JSON.parse(await readFile(join(hub, "batches", plan.id, "jobs/review/receipt.json"), "utf8"));
  expect(receipt).toMatchObject({ needsPrimaryReview: true, outputCheck: "passed_reference_binding_checks_only" });
  expect(receipt.result.findings[0].evidence[0]).toMatchObject({ file: "source.md", quote: "First paragraph.\nContinues exactly." });
  expect(receipt.result.findings).toHaveLength(2);
  expect(receipt.result.findings[1].evidence[0]).toEqual(receipt.result.findings[0].evidence[0]);
  await runHarness(hub, plan.id, opts); expect(execute).toHaveBeenCalledTimes(1);
  const second = { ...plan, id: "stale" }; await prepareHarness(second, hub); await writeFile(join(root, "input.md"), "changed");
  await expect(runHarness(hub, second.id, opts)).rejects.toThrow("Source changed"); expect(execute).toHaveBeenCalledTimes(1);
});


it("rejects source change during execution without saving a result or retry", async () => {
  root = await realpath(await mkdtemp(join(tmpdir(), "test-design-change-"))); const hub = join(root, "hub");
  const brain = { version: "potluck-agent-brain-v1", project: "sample", revision: "1", updatedAt: new Date(Date.now()-1000).toISOString(), expiresAt: new Date(Date.now()+3600000).toISOString(), brief: "Preserve evidence.", priorities: ["Retain timing."], constraints: ["Read only"], provenance: ["Reviewed note"] };
  await writeFile(join(root, "brain.json"), JSON.stringify(brain)); await writeFile(join(root, "input.md"), source);
  const plan: HarnessPlan = { version: "potluck-agent-plan-v3", id: "change", brain: join(root, "brain.json"), root, model: "fixture", contextWindowTokens: 8192, limits: { concurrency: 1, maxSteps: 1, outputTokens: 700, timeoutMs: 2000, totalOutputTokens: 700 }, jobs: [{ id: "review", ...job, files: [{ path: "input.md", name: "source.md" }] }] };
  const execute = vi.fn<NonNullable<RunHarnessOptions["execute"]>>(async (_history, task, options) => {
    const paragraph = JSON.parse(task).paragraphs.find((p: {file:string}) => p.file === "source.md");
    options.callbacks.onToken(output([paragraph.id])); await writeFile(join(root, "input.md"), "changed during inference"); return { status: "finished" };
  });
  const opts = { endpoint: "http://127.0.0.1:31777/v1", apiKey: "fixture", execute };
  await prepareHarness(plan, hub); expect((await runHarness(hub, plan.id, opts)).jobs[0].status).toBe("failed");
  const receipt = JSON.parse(await readFile(join(hub, "batches", plan.id, "jobs/review/receipt.json"), "utf8"));
  expect(receipt.result).toBeUndefined(); expect(execute).toHaveBeenCalledTimes(1);
});
