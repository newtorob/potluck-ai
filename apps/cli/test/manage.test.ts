import { afterEach, describe, expect, it, vi } from "vitest";
import { parseArgs } from "../src/args.js";
import { runOperation } from "../src/operations.js";
import { runtimeRequest } from "../src/runtime.js";

vi.mock("../src/runtime.js", () => ({ runtimeRequest: vi.fn(), startRuntime: vi.fn(), stopRuntime: vi.fn() }));
afterEach(() => vi.resetAllMocks());
async function run(argv: string[]) {
  let stdout = "";
  const code = await runOperation(parseArgs(argv), text => { stdout += text; });
  return { code, stdout };
}

describe("management commands", () => {
  it.each([undefined, "on", "off"])("shares the startup restore preference with the app (%s)", async target => {
    vi.mocked(runtimeRequest).mockResolvedValue({ runtime: { startup_model_id: "tiny" },
      ui: { "potluck.autoload_on_startup": String(target !== "off") }, gateway: { api_key: "secret" } });
    const result = await run(["models", "autoload", ...(target ? [target] : []), "--json"]);
    expect(JSON.parse(result.stdout)).toEqual({ schemaVersion: 1, action: "autoload", enabled: target !== "off", model: "tiny" });
    expect(result.stdout).not.toContain("secret");
    expect(runtimeRequest).toHaveBeenCalledWith("/settings", target
      ? { method: "PATCH", body: { ui: { "potluck.autoload_on_startup": String(target === "on") } }, timeout: 30000 }
      : { timeout: 30000 });
  });
  it.each(["enable", "disable", "rotate"])("handles gateway %s without printing credentials", async action => {
    vi.mocked(runtimeRequest).mockResolvedValue({ api_key: "secret", gateway: { enabled: action !== "disable", api_key: "secret", pool_routing: true } });
    const result = await run(["gateway", action, "--json"]);
    expect(result.code).toBe(0);
    expect(result.stdout).not.toContain("secret");
    expect(JSON.parse(result.stdout)).toMatchObject({ enabled: action !== "disable", keyConfigured: true });
    expect(runtimeRequest).toHaveBeenCalledWith(action === "disable" ? "/settings" : `/settings/gateway/${action}`,
      action === "disable" ? { method: "PATCH", body: { gateway: { enabled: false } }, timeout: 3000 }
        : { method: "POST", timeout: 3000 });
  });
  it("lists allowlisted model fields and hides download URLs", async () => {
    vi.mocked(runtimeRequest).mockResolvedValue([{ slug: "tiny", name: "Tiny", installed: true, download_url: "https://host/?token=secret" }]);
    const result = await run(["models", "list", "--json"]);
    expect(JSON.parse(result.stdout).models).toEqual([{ id: "tiny", name: "Tiny", installed: true, sizeBytes: null }]);
    expect(result.stdout).not.toContain("secret");
  });
  it.each(["install", "load", "unload", "progress", "cancel"])("sends model %s to the existing runtime API", async action => {
    vi.mocked(runtimeRequest).mockResolvedValue({ status: "downloading", bytes_downloaded: 10, bytes_total: 20, error: "secret" });
    const result = await run(["models", action, "tiny", "--json"]);
    expect(result.code).toBe(0);
    expect(result.stdout).not.toContain("secret");
    expect(JSON.parse(result.stdout).progress).toBe(50);
    const suffix = action === "progress" ? "download-progress" : action === "cancel" ? "cancel-download" : action;
    expect(runtimeRequest).toHaveBeenCalledWith(`/models/tiny/${suffix}`, { method: action === "progress" ? "GET" : "POST", timeout: 30000 });
  });
  it.each([["models", "load"], ["models", "load", "../private"], ["models", "list", "extra"], ["models", "autoload", "tiny"], ["models", "autoload", "on", "extra"], ["gateway", "unknown"], ["up", "extra"]])("rejects invalid command %j before requests", async (...argv) => {
    expect(() => parseArgs(argv)).toThrow();
    expect(runtimeRequest).not.toHaveBeenCalled();
  });
  it("makes a failed background download a nonzero result", async () => {
    vi.mocked(runtimeRequest).mockResolvedValue({ status: "error", error: "private details" });
    expect((await run(["models", "progress", "tiny", "--json"])).code).toBe(1);
  });
});
