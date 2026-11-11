import { hostname } from "node:os";
import { mkdtemp, readFile, realpath, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { prepareHarness, recoverHarness, runHarness, validatePlan, type HarnessPlan, type RunHarnessOptions } from "../src/agentHarness.js";
import { DomainRejected, DomainUnavailable, type DomainAdapter, type DomainPrepared } from "../src/agentHarnessDomain.js";

const identity = "a".repeat(64);
const frozen: DomainPrepared = { identity, binding: { fixed: true }, request: { messages: [{ role: "system", content: "FROZEN SYSTEM" }, { role: "user", content: "FROZEN SOURCE" }], response_format: { type: "json_object", schema: { type: "object" } } } };
let root: string, hub: string, dir: string, plan: HarnessPlan;
let adapter: DomainAdapter, execute: NonNullable<RunHarnessOptions["execute"]>;
const options = () => ({ endpoint: "http://127.0.0.1:31777/v1", apiKey: "fixture", domainAdapter: adapter, execute });
const read = async (name: string) => JSON.parse(await readFile(join(dir, name), "utf8"));
const write = async (name: string, data: unknown) => writeFile(join(dir, name), JSON.stringify(data));
beforeEach(async () => {
  root = await realpath(await mkdtemp(join(tmpdir(), "potluck-typed-"))); hub = join(root, "hub"); dir = join(hub, "batches/typed/jobs/review");
  await writeFile(join(root, "brain.json"), JSON.stringify({ version: "potluck-agent-brain-v1", project: "sample", revision: "1", updatedAt: new Date(Date.now() - 1000).toISOString(), expiresAt: new Date(Date.now() + 3600000).toISOString(), brief: "SECRET BRAIN", priorities: ["priority"], constraints: ["read only"], provenance: ["note"] }));
  await writeFile(join(root, "source-envelope.json"), "{}"); await writeFile(join(root, "draft.jsonl"), "{}\n");
  plan = { version: "potluck-agent-plan-v4", id: "typed", brain: join(root, "brain.json"), root, model: "fixture", contextWindowTokens: 8192, limits: { concurrency: 1, maxSteps: 1, outputTokens: 700, timeoutMs: 2000, totalOutputTokens: 700 }, jobs: [{ id: "review", domain: "company-brain-coverage-v1", objective: "SECRET ASSIGNMENT", successCriteria: ["secret criteria"], files: [{ path: "source-envelope.json", name: "source-envelope.json" }, { path: "draft.jsonl", name: "draft.jsonl" }] }] };
  adapter = { prepare: vi.fn(async () => structuredClone(frozen)), decode: vi.fn(async () => ({ identity, result: { status: "needs_review", findings: [] } })) };
  execute = vi.fn(async (_h, _t, o) => { o.callbacks.onToken("raw output"); return { status: "finished" }; });
});
afterEach(async () => { await rm(root, { recursive: true, force: true }); });
const prep = () => prepareHarness(plan, hub, { domainAdapter: adapter });
const run = () => runHarness(hub, plan.id, options());

it("dispatches only frozen messages with tools disabled and publishes raw before decoding", async () => {
  adapter.decode = vi.fn(async (_b, raw) => { expect((await read("raw-receipt.json")).trace).toBe(raw); expect((await read("state.json")).status).toBe("needs_domain_validation"); return { identity, result: { status: "needs_review", findings: [] } }; });
  execute = vi.fn(async (history, task, o) => { expect(history).toEqual([frozen.request.messages[0]]); expect(task).toBe("FROZEN SOURCE"); expect(JSON.stringify([history, task])).not.toContain("SECRET"); expect(o.toolsDisabled).toBe(true); expect(o.responseFormat).toEqual(frozen.request.response_format); o.callbacks.onToken("raw output"); return { status: "finished" }; });
  await prep(); expect((await run()).jobs[0].status).toBe("needs_review"); expect((await read("receipt.json")).result.status).toBe("needs_review");
});
it("resumes unavailable validation without executing again", async () => {
  adapter.decode = vi.fn().mockRejectedValueOnce(new DomainUnavailable()).mockResolvedValue({ identity, result: { status: "needs_review", findings: [] } });
  await prep(); expect((await run()).jobs[0].status).toBe("needs_domain_validation"); expect((await run()).jobs[0].status).toBe("needs_review"); expect(execute).toHaveBeenCalledTimes(1);
});
it("persists a rejection and never retries, including an empty finished trace", async () => {
  adapter.decode = vi.fn(async () => { throw new DomainRejected(); }); execute = vi.fn(async () => ({ status: "finished" }));
  await prep(); expect((await run()).jobs[0].status).toBe("failed"); expect((await read("raw-receipt.json")).trace).toBe(""); await run(); expect(execute).toHaveBeenCalledTimes(1);
});
it("rejects identity drift before dispatch and leaves decode drift pending", async () => {
  await prep(); adapter.prepare = vi.fn(async () => ({ ...frozen, identity: "b".repeat(64) }));
  await expect(run()).rejects.toThrow(); expect(execute).not.toHaveBeenCalled();
  adapter.prepare = vi.fn(async () => structuredClone(frozen)); adapter.decode = vi.fn(async () => ({ identity: "b".repeat(64), result: { status: "needs_review" } }));
  expect((await run()).jobs[0].status).toBe("needs_domain_validation"); expect(execute).toHaveBeenCalledTimes(1);
});
it("keeps results pending when source changes during decoding", async () => {
  adapter.decode = vi.fn(async () => { await writeFile(join(root, "draft.jsonl"), "changed"); return { identity, result: { status: "needs_review" } }; });
  await prep(); expect((await run()).jobs[0].status).toBe("needs_domain_validation"); await expect(read("receipt.json")).rejects.toThrow();
});
it.each(["pending", "final"])("detects raw receipt tampering in %s state", async mode => {
  if (mode === "pending") adapter.decode = vi.fn(async () => { throw new DomainUnavailable(); });
  await prep(); await run(); await writeFile(join(dir, "raw-receipt.json"), "{}"); await expect(run()).rejects.toThrow();
});
it("recovers orphan running output and resumes without inference", async () => {
  adapter.decode = vi.fn().mockRejectedValueOnce(new DomainUnavailable()).mockResolvedValue({ identity, result: { status: "needs_review" } });
  await prep(); await run(); await write("state.json", { status: "running", updatedAt: new Date().toISOString() });
  await writeFile(join(hub, "run-lock.json"), JSON.stringify({ host: hostname(), pid: 99999999, token: "orphan", batch: plan.id, startedAt: new Date().toISOString() }));
  await recoverHarness(hub); expect((await read("state.json")).status).toBe("needs_domain_validation"); expect((await run()).jobs[0].status).toBe("needs_review"); expect(execute).toHaveBeenCalledTimes(1);
});
it("replays a final receipt after state-write crash and rejects tampered results", async () => {
  await prep(); await run(); const state = await read("state.json");
  await write("state.json", { status: "needs_domain_validation", updatedAt: new Date().toISOString(), rawReceiptSha256: state.rawReceiptSha256 });
  expect((await run()).jobs[0].status).toBe("needs_review"); expect(execute).toHaveBeenCalledTimes(1);
  const final = await read("receipt.json"); final.result.findings = ["tampered"]; await write("receipt.json", final);
  await write("state.json", { status: "needs_domain_validation", updatedAt: new Date().toISOString(), rawReceiptSha256: state.rawReceiptSha256 });
  await expect(run()).rejects.toThrow(); expect(execute).toHaveBeenCalledTimes(1);
});
it("requires the adapter and strict v4 limits", async () => {
  await expect(prepareHarness(plan, hub)).rejects.toThrow();
  for (const limits of [{ ...plan.limits, outputTokens: 699 }, { ...plan.limits, maxSteps: 2 }, { ...plan.limits, concurrency: 2 }, { ...plan.limits, timeoutMs: 180001 }]) expect(() => validatePlan({ ...plan, limits })).toThrow();
});
