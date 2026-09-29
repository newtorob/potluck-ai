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

describe("split memory visibility", () => {
  function status(state: string, placement?: Array<{ device: string; modelMiB: number }>) {
    return { state, engine: { available: true, backend: "cuda" }, workerEnabled: false,
      session: { model: { id: "coder" }, workers: [{ name: "mac", sentBytes: 1024, receivedBytes: 0 }], placement } };
  }
  it("shows verified CPU overflow and sums repeated backend records", async () => {
    vi.mocked(runtimeRequest).mockResolvedValue(status("ready", [
      { device: "CUDA", modelMiB: 2048 }, { device: "CUDA", modelMiB: 4096 },
      { device: "RPC", modelMiB: 512 }, { device: "CPU", modelMiB: 6800 },
    ]));
    let out = "";
    await runSplit(parseArgs(["split", "status"]), s => out += s);
    expect(out).toContain("Verified model placement: CUDA 6.00 GiB · Workers 512 MiB · CPU 6.64 GiB");
    expect(out).toContain("restart the split");
  });
  it.each(["loading", "failed"])("does not call %s allocations verified", async state => {
    vi.mocked(runtimeRequest).mockResolvedValue(status(state, [{ device: "CUDA", modelMiB: 2048 }]));
    let out = "";
    await runSplit(parseArgs(["split", "status"]), s => out += s);
    expect(out).toContain("Reported model placement:");
    expect(out).not.toContain("Verified model placement:");
  });
  it("does not invent placement for older runtimes or zero-byte fit probes", async () => {
    for (const placement of [undefined, [], [{ device: "CPU", modelMiB: 0 }]]) {
      vi.mocked(runtimeRequest).mockResolvedValue(status("ready", placement));
      let out = "";
      await runSplit(parseArgs(["split", "status"]), s => out += s);
      expect(out).toContain("Model placement: not reported yet.");
      expect(out).not.toContain("restart the split");
    }
  });
  it("shows the worker budget without presenting system RAM as CUDA VRAM", async () => {
    vi.mocked(runtimeRequest).mockResolvedValue({ context: 8192, note: "Placement verified after loading.", workers: [
      { node_name: "mac", backend: "metal", sharedMemory: { availableMiB: 3277, reserveMiB: 2048, budgetMiB: 1229 } },
      { node_name: "gpu", backend: "cuda" },
    ] });
    let out = "";
    await runSplit(parseArgs(["split", "plan", "coder", "--peer", "mac", "--peer", "gpu"]), s => out += s);
    expect(out).toContain("mac: 3.20 GiB available, 1.20 GiB budget (2.00 GiB headroom)");
    expect(out).not.toContain("gpu:");
    expect(out).toContain("checked again at start");
  });
  it("keeps JSON machine readable and unchanged", async () => {
    const report = status("ready", [{ device: "CPU", modelMiB: 6800 }]);
    vi.mocked(runtimeRequest).mockResolvedValue(report);
    let out = "";
    await runSplit(parseArgs(["split", "status", "--json"]), s => out += s);
    expect(JSON.parse(out)).toEqual(report);
  });
});
