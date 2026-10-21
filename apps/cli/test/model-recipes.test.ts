import { afterEach, describe, expect, it, vi } from "vitest";
import { parseArgs } from "../src/args.js";
import { ControlError } from "../src/control.js";
import { runOperation } from "../src/operations.js";
import { runtimeRequest } from "../src/runtime.js";

vi.mock("../src/runtime.js", async importOriginal => ({ ...await importOriginal<typeof import("../src/runtime.js")>(), runtimeRequest: vi.fn() }));
afterEach(() => vi.resetAllMocks());
const recipe = () => ({ id: "tiny-cpu", name: "Tiny CPU", qualification: "experimental", notes: "Not qualified for coding.",
  model: { id: "tiny-q4", repository: "owner/model-GGUF", revision: "a".repeat(40), filename: "model-q4.gguf", sha256: "b".repeat(64), size_bytes: 512 * 1024 ** 2 },
  engine: { backend: "llama-server", commit: "c".repeat(40), acceleration: "cpu", platforms: [{ os: "Darwin", arch: "arm64" }, { os: "Linux", arch: "x86_64" }] },
  runtime: { context_size: 8192, gpu_layers: 0, native_cache_mib: 256 }, resources: { kv_bytes_per_token: 12288, overhead_bytes: 512 * 1024 ** 2 } });
const preflight = () => ({ schema_version: 1, recipe: recipe(), can_install: true, can_load: true, installed: true, artifact_verified: true,
  issues: [] as { code: string; message: string; blocks: string[] }[], warnings: ["Estimates require local validation."],
  resources: { estimated_ram_bytes: 2 * 1024 ** 3, available_ram_bytes: 8 * 1024 ** 3, disk_free_bytes: 10 * 1024 ** 3, required_disk_bytes: 0 } });
async function run(...args: string[]) {
  let stdout = "";
  const code = await runOperation(parseArgs(["models", ...args]), text => { stdout += text; });
  return { code, stdout };
}

describe("model recipe inspection", () => {
  it("lists pinned experimental artifacts without hardware probes, downloads or loading", async () => {
    vi.mocked(runtimeRequest).mockResolvedValue({ schema_version: 1, recipes: [recipe()] });
    const { code, stdout } = await run("recipes");
    expect(code).toBe(0);
    expect(stdout).toContain("tiny-cpu: Tiny CPU (experimental)");
    expect(stdout).toContain("Context: 8192 tokens");
    expect(stdout).toContain("potluck models preflight tiny-cpu");
    expect(stdout).toContain("not model quality, speed or hardware qualification");
    expect(runtimeRequest).toHaveBeenCalledExactlyOnceWith("/models/recipes", { timeout: 30000 });
  });
  it("returns a bounded allowlist, excluding engine credentials, file paths and download URLs", async () => {
    vi.mocked(runtimeRequest).mockResolvedValue({ schema_version: 1, api_key: "secret", recipes: [{ ...recipe(), path: "/private/weights",
      model: { ...recipe().model, download_url: "https://example.invalid/?token=secret" }, engine: { ...recipe().engine, api_key: "secret" } }] });
    const { stdout } = await run("recipes", "--json");
    expect(JSON.parse(stdout)).toEqual({ schemaVersion: 1, action: "recipes", recipes: [recipe()] });
    expect(stdout).not.toContain("secret"); expect(stdout).not.toContain("/private");
  });
  it("reports an empty recipe catalog without suggesting an install", async () => {
    vi.mocked(runtimeRequest).mockResolvedValue({ schema_version: 1, recipes: [] });
    expect((await run("recipes")).stdout).toContain("No recipes available");
  });
  it("reports exact artifact and resource checks without treating them as qualification", async () => {
    vi.mocked(runtimeRequest).mockResolvedValue(preflight());
    const { stdout, code } = await run("preflight", "tiny-cpu");
    expect(code).toBe(0); expect(stdout).toContain("Pinned artifact verified: yes");
    expect(stdout).toContain("RAM: 2.0 GiB estimated / 8.0 GiB available");
    expect(stdout).toContain("Load requirements: pass");
    expect(stdout).toContain("replace the current local model for this session");
    expect(stdout).toContain("not model quality, speed or hardware qualification");
    expect(runtimeRequest).toHaveBeenCalledExactlyOnceWith("/models/recipes/tiny-cpu/preflight", { timeout: 120000 });
  });
  it("exits 1 for missing weights even when installation is possible, with stable JSON", async () => {
    vi.mocked(runtimeRequest).mockResolvedValue({ ...preflight(), can_load: false, installed: false, artifact_verified: false,
      issues: [{ code: "not_installed", message: "Install the pinned weights.", blocks: ["load"] }] });
    const { code, stdout } = await run("preflight", "tiny-cpu", "--json");
    expect(code).toBe(1);
    expect(JSON.parse(stdout)).toMatchObject({ schemaVersion: 1, action: "preflight", can_install: true, can_load: false, artifact_verified: false,
      issues: [{ code: "not_installed", blocks: ["load"] }] });
    expect((await run("preflight", "tiny-cpu")).stdout).toContain("potluck models install-recipe tiny-cpu");
  });
  it("shows unknown live CUDA capacity as blocked, without treating total memory as available", async () => {
    vi.mocked(runtimeRequest).mockResolvedValue({ ...preflight(), can_load: false, recipe: { ...recipe(), engine: { ...recipe().engine, acceleration: "cuda", platforms: [{ os: "Linux", arch: "x86_64" }] }, runtime: { ...recipe().runtime, gpu_layers: -1 } },
      resources: { ...preflight().resources, estimated_vram_bytes: 2 * 1024 ** 3, available_vram_bytes: null },
      issues: [{ code: "vram_unknown", message: "Live free VRAM is unavailable.", blocks: ["load"] }] });
    const { code, stdout } = await run("preflight", "tiny-cpu");
    expect(code).toBe(1); expect(stdout).toContain("VRAM: 2.0 GiB estimated / unknown available");
    expect(stdout).toContain("Blocks load: Live free VRAM is unavailable.");
    expect(stdout).not.toContain("potluck models load-recipe");
  });
  it.each([
    { schema_version: 2 }, { can_load: "yes" }, { installed: false }, { artifact_verified: false },
    { recipe: { ...recipe(), id: "other" } }, { resources: { ...preflight().resources, estimated_ram_bytes: -1 } },
    { resources: { ...preflight().resources, available_ram_bytes: null } },
    { resources: { ...preflight().resources, available_ram_bytes: 1 } },
    { resources: { ...preflight().resources, disk_free_bytes: null } },
    { issues: [{ code: "memory", message: "No RAM", blocks: ["load"] }] },
    { issues: [{ code: "disk", message: "No disk", blocks: ["install"] }] },
    { issues: [{ code: "bad", message: "Bad", blocks: ["unknown"] }] },
  ])("refuses malformed or contradictory preflight data (%j)", async change => {
    vi.mocked(runtimeRequest).mockResolvedValue({ ...preflight(), ...change });
    await expect(run("preflight", "tiny-cpu", "--json")).rejects.toThrow("invalid model recipe data");
    expect(runtimeRequest).toHaveBeenCalledTimes(1);
  });
  it.each([
    { qualification: "qualified" }, { model: { ...recipe().model, revision: "main" } },
    { model: { ...recipe().model, filename: "../model.gguf" } }, { model: { ...recipe().model, sha256: "wrong" } },
    { runtime: { ...recipe().runtime, context_size: 0 } }, { runtime: { ...recipe().runtime, gpu_layers: -1 } },
    { engine: { ...recipe().engine, commit: "main" } }, { engine: { ...recipe().engine, platforms: [] } },
    { model: { ...recipe().model, filename: "model-00001-of-00002.gguf" } },
    { engine: { ...recipe().engine, acceleration: "metal" }, runtime: { ...recipe().runtime, gpu_layers: -1 } },
  ])("refuses unpinned or unsupported recipe fields (%j)", async change => {
    vi.mocked(runtimeRequest).mockResolvedValue({ schema_version: 1, recipes: [{ ...recipe(), ...change }] });
    await expect(run("recipes", "--json")).rejects.toThrow("invalid model recipe data");
  });
});

describe("explicit recipe mutations", () => {
  it.each(["downloading", "already_installed"])("requests one pinned install (%s) without loading or changing preferences", async status => {
    vi.mocked(runtimeRequest).mockResolvedValue({ status, recipe_id: "tiny-cpu", slug: "tiny-q4", error: "private-path" });
    const { code, stdout } = await run("install-recipe", "tiny-cpu", "--json");
    expect(code).toBe(0); expect(JSON.parse(stdout)).toMatchObject({ schemaVersion: 1, action: "install-recipe", recipe_id: "tiny-cpu", model: "tiny-q4", status });
    expect(stdout).not.toContain("private-path");
    expect(runtimeRequest).toHaveBeenCalledExactlyOnceWith("/models/recipes/tiny-cpu/install", { method: "POST", timeout: 120000 });
    if (status === "downloading") expect(JSON.parse(stdout).next).toBe("Check: potluck models progress tiny-q4");
  });
  it("acknowledges a session-only load and reports that restart restore is cleared", async () => {
    vi.mocked(runtimeRequest).mockResolvedValue({ status: "loaded", recipe_id: "tiny-cpu", model_id: "tiny-q4", autoload: false, private_key: "secret" });
    const { code, stdout } = await run("load-recipe", "tiny-cpu", "--timeout", "300000");
    expect(code).toBe(0); expect(stdout).toContain("Session only: startup model restore is cleared");
    expect(stdout).toContain("Runtime preferences are unchanged"); expect(stdout).not.toContain("secret");
    expect(runtimeRequest).toHaveBeenCalledExactlyOnceWith("/models/recipes/tiny-cpu/load", { method: "POST", timeout: 300000 });
    expect(JSON.parse((await run("load-recipe", "tiny-cpu", "--json")).stdout)).toEqual({ schemaVersion: 1, action: "load-recipe", recipe_id: "tiny-cpu", model: "tiny-q4", status: "loaded", autoload: false, sessionOnly: true });
  });
  it.each([
    { status: "loading", recipe_id: "tiny-cpu", model_id: "tiny-q4", autoload: false },
    { status: "loaded", recipe_id: "different", model_id: "tiny-q4", autoload: false },
    { status: "loaded", recipe_id: "tiny-cpu", model_id: "tiny-q4", autoload: true },
    { status: "loaded", recipe_id: "tiny-cpu", model_id: "../weights", autoload: false },
  ])("does not claim a successful load without explicit session-only acknowledgement (%j)", async response => {
    vi.mocked(runtimeRequest).mockResolvedValue(response);
    await expect(run("load-recipe", "tiny-cpu")).rejects.toThrow("invalid model recipe data");
  });
  it("requires a valid pinned install acknowledgement", async () => {
    for (const response of [{ status: "downloading", recipe_id: "other", slug: "tiny-q4" },
      { status: "error", recipe_id: "tiny-cpu", slug: "tiny-q4" }, { status: "downloading", recipe_id: "tiny-cpu", slug: "../private" }]) {
      vi.mocked(runtimeRequest).mockResolvedValue(response);
      await expect(run("install-recipe", "tiny-cpu")).rejects.toThrow("invalid model recipe data");
    }
  });
  it("preserves an ambiguous load timeout without retrying or falling back", async () => {
    vi.mocked(runtimeRequest).mockRejectedValue(new ControlError("timeout", "Request timed out."));
    await expect(run("load-recipe", "tiny-cpu")).rejects.toThrow("may still finish. Check potluck status before retrying");
    expect(runtimeRequest).toHaveBeenCalledTimes(1);
  });
  it("explains refusal without exposing raw backend details", async () => {
    vi.mocked(runtimeRequest).mockRejectedValue(new ControlError("http_error", "Service returned HTTP 409.", 409, "/private/path?token=secret"));
    await expect(run("install-recipe", "tiny-cpu")).rejects.toThrow("potluck models preflight tiny-cpu");
    expect(runtimeRequest).toHaveBeenCalledTimes(1);
  });
  it("suggests upgrading an older runtime instead of silently using the normal catalog", async () => {
    vi.mocked(runtimeRequest).mockRejectedValue(new ControlError("http_error", "Service returned HTTP 404.", 404));
    await expect(run("recipes")).rejects.toThrow("Upgrade the Potluck runtime");
    expect(runtimeRequest).toHaveBeenCalledTimes(1);
  });
});

it("documents commands without contacting a runtime", async () => {
  const { stdout, code } = await run("load-recipe", "--help");
  expect(code).toBe(0); expect(stdout).toContain("install-recipe|load-recipe <recipe-id>");
  expect(stdout).toContain("clears startup restore"); expect(runtimeRequest).not.toHaveBeenCalled();
});
it("validates recipe arguments and timeouts before making any request", () => {
  for (const args of [["recipes", "extra"], ["preflight"], ["install-recipe", "../weights"], ["load-recipe", "a", "extra"],
    ["load-recipe", "tiny-cpu", "--timeout", "300001"], ["recipes", "--timeout", "30001"], ["preflight", "tiny-cpu", "--scope", "mine"],
    ["install-recipe", "tiny-cpu", "--model", "other"], ["load-recipe", "tiny-cpu", "--full"]]) {
    expect(() => parseArgs(["models", ...args])).toThrow();
  }
  expect(runtimeRequest).not.toHaveBeenCalled();
  for (const action of ["preflight", "install-recipe", "load-recipe"]) expect(parseArgs(["models", action, "tiny-cpu"]).timeout).toBe(120000);
  expect(parseArgs(["models", "load", "tiny-q4"]).timeout).toBe(30000);
});
