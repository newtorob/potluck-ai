import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { parseArgs } from "../src/args.js";
import { setupModel } from "../src/model-setup.js";
import { runtimeRequest } from "../src/runtime.js";
import { readProjectModelLock } from "../src/model-lock.js";
import { loadCliPrefs, saveCliPrefs } from "../src/config.js";
vi.mock("../src/runtime.js", async original => ({ ...await original<typeof import("../src/runtime.js")>(), runtimeRequest: vi.fn() }));
vi.mock("../src/model-lock.js", async original => ({ ...await original<typeof import("../src/model-lock.js")>(), readProjectModelLock: vi.fn() }));
vi.mock("../src/config.js", () => ({ loadCliPrefs: vi.fn(), saveCliPrefs: vi.fn() }));
const hash = "a".repeat(64);
const model = { id: "small", name: "Small", sizeBytes: 1024 ** 3, fits: true, installed: false, loaded: false, reasons: [], estimatedWorkingGiB: 2.7 };
const base = { schemaVersion: 1, hardware: { ram_total_gb: 16, ram_available_gb: 8 }, diskFreeBytes: 100 * 1024 ** 3,
  currentModel: null, recommendedModel: "small", models: [model] };
const deps = { wait: async () => {}, now: () => 0 };
const request = vi.mocked(runtimeRequest);
beforeEach(() => { vi.mocked(loadCliPrefs).mockResolvedValue({ scope: "mine", verify: "npm test" }); });
afterEach(() => vi.resetAllMocks());
function serve(options = base, states = ["downloading", "verifying", "complete"]) {
  request.mockImplementation(async (route, opts) => {
    if (route === "/models/setup-options") return options as never;
    if (route.endsWith("/install")) return { status: "downloading" } as never;
    if (route.endsWith("/download-progress")) return { status: states.shift() ?? "complete", bytes_downloaded: 1024 ** 3, bytes_total: 1024 ** 3 } as never;
    if (route.endsWith("/test")) return { status: "passed", model: "small", scope: "local", durationMs: 25 } as never;
    return { status: "loaded" } as never;
  });
}
it("takes an explicit unattended request through download, verification, loading and actual response before saving preferences", async () => {
  serve();
  const result = await setupModel(parseArgs(["setup", "--model", "small", "--no-input"]), undefined, deps);
  expect(result).toMatchObject({ status: "ready", model: "small", scope: "local" });
  expect(request.mock.calls.map(c => c[0])).toEqual(["/models/setup-options", "/models/small/install", "/models/small/download-progress",
    "/models/small/download-progress", "/models/small/download-progress", "/models/small/load", "/models/small/test"]);
  expect(saveCliPrefs).toHaveBeenCalledWith({ scope: "mine", verify: "npm test", model: "small" });
});
it("declining a download changes neither model nor preferences", async () => {
  serve(); const ask = vi.fn().mockResolvedValueOnce("").mockResolvedValueOnce("no");
  expect(await setupModel(parseArgs(["setup"]), { ask, progress: vi.fn() }, deps)).toEqual({ status: "skipped" });
  expect(request).toHaveBeenCalledTimes(1); expect(saveCliPrefs).not.toHaveBeenCalled();
});
it("reuses an already loaded model without another download or model swap", async () => {
  serve({ ...base, currentModel: "small" as never, models: [{ ...model, installed: true, loaded: true }] });
  await setupModel(parseArgs(["setup", "--with-model"]), undefined, deps);
  expect(request.mock.calls.map(c => c[0])).toEqual(["/models/setup-options", "/models/small/test"]);
});
it("does not silently replace a pin and verifies it before and after loading", async () => {
  vi.mocked(readProjectModelLock).mockResolvedValue({ schemaVersion: 1, path: "/project/potluck.lock", model: { id: "small", format: "gguf", sizeBytes: model.sizeBytes, sha256: hash } });
  serve({ ...base, recommendedModel: "other", models: [{ ...model, installed: true }] });
  const original = request.getMockImplementation()!;
  request.mockImplementation(async (route, opts) => route.endsWith("/install-locked")
    ? { model_lock_version: 1, status: "verified", sha256: hash } as never : original(route, opts));
  await setupModel(parseArgs(["setup", "--with-model"]), undefined, deps);
  expect(request).toHaveBeenCalledWith("/models/small/test", expect.objectContaining({ body: { sha256: hash } }));
  expect(saveCliPrefs).not.toHaveBeenCalled();
  await expect(setupModel(parseArgs(["setup", "--model", "other"]), undefined, deps)).rejects.toThrow("pins small");
});
it.each(["error", "cancelled"])("does not load or save after a failed download: %s", async state => {
  serve(base, [state]);
  await expect(setupModel(parseArgs(["setup", "--with-model"]), undefined, deps)).rejects.toThrow("Download");
  expect(request.mock.calls.some(c => c[0].endsWith("/load"))).toBe(false); expect(saveCliPrefs).not.toHaveBeenCalled();
});
it("refuses insufficient memory/disk before starting a download", async () => {
  serve({ ...base, models: [{ ...model, fits: false, reasons: ["Insufficient RAM"] }] });
  await expect(setupModel(parseArgs(["setup", "--model", "small"]), undefined, deps)).rejects.toThrow("Insufficient RAM");
  expect(request).toHaveBeenCalledTimes(1);
});
it("cancels only the download this setup started and preserves preferences on interrupt", async () => {
  serve(); const abort = new AbortController();
  await expect(setupModel(parseArgs(["setup", "--model", "small"]), { ask: vi.fn(), progress: vi.fn(), signal: abort.signal },
    { now: () => 0, wait: async () => { abort.abort(); } })).rejects.toThrow("interrupted");
  expect(request).toHaveBeenCalledWith("/models/small/cancel-download", expect.anything());
  expect(saveCliPrefs).not.toHaveBeenCalled();
});
it("does not report readiness or save preferences when inference returns no success", async () => {
  serve(); const original = request.getMockImplementation()!;
  request.mockImplementation(async (route, opts) => route.endsWith("/test") ? { status: "error" } as never : original(route, opts));
  await expect(setupModel(parseArgs(["setup", "--model", "small"]), undefined, deps)).rejects.toThrow("local response check");
  expect(saveCliPrefs).not.toHaveBeenCalled();
});
it("follows a preexisting locked download without cancelling it, then verifies exact bytes", async () => {
  const { ControlError } = await import("../src/control.js");
  vi.mocked(readProjectModelLock).mockResolvedValue({ schemaVersion: 1, path: "/project/potluck.lock", model: { id: "small", format: "gguf", sizeBytes: model.sizeBytes, sha256: hash } });
  serve(base, ["downloading", "complete"]);
  const original = request.getMockImplementation()!; let installs = 0;
  request.mockImplementation(async (route, opts) => {
    if (route.endsWith("/install-locked")) {
      if (++installs === 1) throw new ControlError("http_error", "busy", 409);
      return { model_lock_version: 1, status: "verified", sha256: hash } as never;
    }
    return original(route, opts);
  });
  expect((await setupModel(parseArgs(["setup", "--with-model"]), undefined, deps)).status).toBe("ready");
  expect(installs).toBe(2);
  expect(request.mock.calls.some(c => c[0].endsWith("/cancel-download"))).toBe(false);
});
