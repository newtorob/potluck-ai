import { beforeEach, expect, it, vi } from "vitest";
import { availableModels, formatAvailability, type Availability } from "../src/availability.js";
import { parseArgs } from "../src/args.js";
import { runtimeRequest } from "../src/runtime.js";
import { readProjectModelLock } from "../src/model-lock.js";
vi.mock("../src/runtime.js", () => ({ runtimeRequest: vi.fn() }));
vi.mock("../src/model-lock.js", () => ({ readProjectModelLock: vi.fn() }));
const model = { id: "coder", format: "gguf" as const, sha256: "a".repeat(64), sizeBytes: 100 };
const report = (): Availability => ({ schemaVersion: 1, scope: "mine", checkedAt: 1, local: { model: null, sha256: null },
  models: [{ id: model.id, sha256: model.sha256, local: false, machines: [{ name: "desktop", meshIp: "100.64.0.2", idle: true }], idleMachineCount: 1 }],
  machines: [], additionalModels: 1, splitWorkerCount: 0, splitEngineAvailable: false, truncated: false, note: "Capacity is not a speed prediction." });
beforeEach(() => { vi.resetAllMocks(); vi.mocked(runtimeRequest).mockResolvedValue(report()); });
it("discovers without changing the model or routing scope", async () => {
  expect(parseArgs(["models", "available"]).action).toBe("available");
  expect(() => parseArgs(["models", "available", "coder"])).toThrow();
  const result = await availableModels(10000);
  expect(result.projectPin).toBeNull();
  expect(runtimeRequest).toHaveBeenCalledExactlyOnceWith("/models/available", { timeout: 10000 });
  expect(formatAvailability(result)).toContain("Use --scope mine explicitly");
});
it("requires exact bytes, not just the same ID, for project readiness", async () => {
  vi.mocked(readProjectModelLock).mockResolvedValue({ schemaVersion: 1, path: "/project/potluck.lock", model });
  expect((await availableModels(10000)).projectPin?.available).toBe(true);
  for (const sha256 of [null, "b".repeat(64)]) {
    const different = report(); different.models[0]!.sha256 = sha256;
    vi.mocked(runtimeRequest).mockResolvedValue(different);
    const result = await availableModels(10000);
    expect(result.projectPin?.available).toBe(false);
    expect(formatAvailability(result)).toContain("no model substitution");
  }
});
