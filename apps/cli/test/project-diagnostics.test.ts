import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { mkdtemp, writeFile, rm } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { diagnoseProject } from "../src/project-diagnostics.js";
import { requestJSON } from "../src/control.js";
import { runtimeRequest } from "../src/runtime.js";
vi.mock("../src/runtime.js", () => ({ runtimeRequest: vi.fn() }));
vi.mock("../src/control.js", async original => ({ ...await original<object>(), requestJSON: vi.fn() }));
const model = { id: "test", format: "gguf", sha256: "a".repeat(64), sizeBytes: 4 * 1024 ** 3 };
const cfg = { enabled: true, apiKey: "never-print", baseURL: "http://localhost/v1" };
let directory: string;
let route: "local" | "household" | null;
beforeEach(async () => {
  directory = await mkdtemp(join(tmpdir(), "potluck-doctor-")); route = "local";
  await writeFile(join(directory, "potluck.lock"), JSON.stringify({ schemaVersion: 1, model }));
  vi.mocked(requestJSON).mockImplementation(async options => options.url?.endsWith("capabilities")
    ? { model_lock_version: 1, scopes: ["local", "mine"], artifact_format: "gguf" }
    : { model_lock_version: 1, ready: Boolean(route), model: model.id, scope: (options.body as any).scope, route });
  vi.mocked(runtimeRequest).mockImplementation(async path => path.endsWith("identity") ? { model } : { hardware: { ram_available_gb: 2 } });
});
afterEach(async () => { vi.clearAllMocks(); await rm(directory, { recursive: true, force: true }); });
it("verifies a loaded pin without generating text and does not charge its memory twice", async () => {
  const checks = await diagnoseProject(cfg, "local", 1000, directory);
  expect(checks.every(c => c.state === "ok")).toBe(true);
  expect(JSON.stringify(checks)).not.toContain(cfg.apiKey);
  expect(vi.mocked(requestJSON).mock.calls[1][0].body).toEqual({ model: model.id, sha256: model.sha256, scope: "local" });
});
it("fails readiness for a stale model and reports actual installed mismatches", async () => {
  route = null;
  vi.mocked(runtimeRequest).mockResolvedValue({ model: { ...model, sha256: "b".repeat(64) }, hardware: { ram_available_gb: 2 } });
  const checks = await diagnoseProject(cfg, "local", 1000, directory);
  expect(checks.find(c => c.id === "pinned_route")?.state).toBe("error");
  expect(checks.find(c => c.id === "installed_weights")?.state).toBe("error");
  expect(checks.find(c => c.id === "hardware_fit")?.state).toBe("warning");
});
it("allows an exact own-machine route despite missing local weights", async () => {
  route = "household"; vi.mocked(runtimeRequest).mockRejectedValue(new Error("absent"));
  const checks = await diagnoseProject(cfg, "mine", 1000, directory);
  expect(checks.some(c => c.state === "error")).toBe(false);
});
it("never reports an old gateway or invalid lock as ready", async () => {
  vi.mocked(requestJSON).mockResolvedValue({});
  expect((await diagnoseProject(cfg, "local", 1000, directory)).some(c => c.state === "error")).toBe(true);
  await writeFile(join(directory, "potluck.lock"), '{}');
  expect((await diagnoseProject(cfg, "local", 1000, directory))[0].id).toBe("project_lock");
});
