import { beforeEach, describe, expect, it, vi } from "vitest";
import { parseArgs } from "../src/args.js";
import { manageSplit, runSplit } from "../src/split.js";
import { runtimeRequest } from "../src/runtime.js";
import { readProjectModelLock } from "../src/model-lock.js";
vi.mock("../src/runtime.js", () => ({ runtimeRequest: vi.fn() }));
vi.mock("../src/model-lock.js", async original => ({ ...await original<typeof import("../src/model-lock.js")>(), readProjectModelLock: vi.fn() }));
const model = { id: "coder", format: "gguf" as const, sha256: "a".repeat(64), sizeBytes: 100 };
beforeEach(() => { vi.resetAllMocks(); vi.mocked(readProjectModelLock).mockResolvedValue({ schemaVersion: 1, path: "/project/potluck.lock", model }); });
describe("managed split", () => {
  it.each([
    ["split", "start", "coder"], ["split", "start", "coder", "--peer", "a", "--peer", "a"],
    ["split", "start", "coder", "--peer", "https://attacker"], ["split", "worker"],
    ["split", "worker", "yes"], ["split", "stop", "coder"], ["split", "status", "--peer", "a"],
    ["split", "plan", "coder", "--peer", "a", "--context", "99999"], ["run", "--peer", "a"],
    ["split", "start", "coder", "--peer", "a", "--scope", "pool"],
  ])("rejects unsupported arguments %j", (...args) => expect(() => parseArgs(args)).toThrow());
  it("sends exact pin and explicit owned worker selection", async () => {
    await manageSplit(parseArgs(["split", "start", "coder", "--peer", "mac", "--context", "1024"]));
    expect(runtimeRequest).toHaveBeenCalledWith("/split/start", { method: "POST", timeout: 30000, errorHints: expect.any(Object), body: { model, peers: ["mac"], context: 1024 } });
  });
  it("refuses missing or different project pins before control requests", async () => {
    await expect(manageSplit(parseArgs(["split", "plan", "other", "--peer", "mac"]))).rejects.toThrow("match potluck.lock");
    vi.mocked(readProjectModelLock).mockResolvedValue(undefined);
    await expect(manageSplit(parseArgs(["split", "start", "coder", "--peer", "mac"]))).rejects.toThrow("pin");
    expect(runtimeRequest).not.toHaveBeenCalled();
  });
  it("worker opt-out is explicit and independent of the project lock", async () => {
    await manageSplit(parseArgs(["split", "worker", "off"]));
    expect(readProjectModelLock).not.toHaveBeenCalled();
    expect(runtimeRequest).toHaveBeenCalledWith("/split/worker", { method: "POST", timeout: 30000, errorHints: expect.any(Object), body: { enabled: false } });
  });
  it("reports failed sessions as unsuccessful", async () => {
    vi.mocked(runtimeRequest).mockResolvedValue({ state: "failed", engine: { available: true, backend: "cuda" }, workerEnabled: false, error: "Worker lost" });
    let output = "";
    expect(await runSplit(parseArgs(["split", "status"]), s => output += s)).toBe(1);
    expect(output).toContain("Worker lost");
  });
});
