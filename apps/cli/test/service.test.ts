import { mkdtemp, mkdir, readFile, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { parseArgs } from "../src/args.js";
import { serviceSpec, manageService } from "../src/service.js";

let directory: string;
beforeEach(async () => {
  directory = await mkdtemp(join(tmpdir(), "potluck-service-test-"));
  vi.stubEnv("HOME", directory);
  vi.stubEnv("POTLUCK_DATA_DIR", join(directory, "private data"));
});
afterEach(async () => { vi.unstubAllEnvs(); await rm(directory, { recursive: true, force: true }); });

describe("service safety", () => {
  it("requires explicit lifecycle verbs and rejects irrelevant foreground flags", () => {
    expect(parseArgs(["service", "install", "--json"])).toMatchObject({ command: "service", action: "install", json: true });
    expect(parseArgs(["service"])).toMatchObject({ action: "status" });
    for (const args of [["service", "destroy"], ["serve", "--json"], ["serve", "--timeout", "1"], ["serve", "--scope", "mine"]]) expect(() => parseArgs(args)).toThrow();
  });
  it("does not persist credentials or shell syntax as executable service code", () => {
    const env = { PATH: '/bin', POTLUCK_SIDECAR_TOKEN: 'secret', POTLUCK_GATEWAY_KEY: 'private',
      POTLUCK_RUNTIME_BIN: '/tmp/a $user %h & <tag> "quoted"/potluck-local' };
    const mac = serviceSpec("darwin", directory, env).content;
    expect(mac).not.toContain('secret'); expect(mac).not.toContain('POTLUCK_GATEWAY_KEY');
    expect(mac).toContain('&amp; &lt;tag&gt; &quot;quoted&quot;');
    const linux = serviceSpec("linux", directory, env).content;
    expect(linux).toContain('POTLUCK_RUNTIME_BIN=/tmp/a $user %%h');
    expect(linux).not.toContain('secret'); expect(linux).not.toContain('POTLUCK_GATEWAY_KEY');
    expect(linux).toContain('UMask=0077');
    expect(() => serviceSpec("linux", directory, { PATH: '/bin\nExecStart=oops' })).toThrow('control characters');
  });
  it("assigns different service labels to independent data directories", () => {
    const a = serviceSpec();
    vi.stubEnv("POTLUCK_DATA_DIR", join(directory, "second"));
    expect(serviceSpec().label).not.toBe(a.label);
  });
  it("refuses to replace a foreign service or follow a service-file symlink", async () => {
    const spec = serviceSpec();
    await mkdir(dirname(spec.file), { recursive: true });
    await writeFile(spec.file, "another installer's service", { mode: 0o600 });
    await expect(manageService("install", 100)).rejects.toThrow("another installer");
    expect(await readFile(spec.file, "utf8")).toBe("another installer's service");
    await rm(spec.file);
    const target = join(directory, "protected");
    await writeFile(target, 'private');
    await symlink(target, spec.file);
    await expect(manageService("uninstall", 100)).rejects.toThrow("unsafe permissions");
    expect(await readFile(target, "utf8")).toBe('private');
  });
  it("leaves a Homebrew service under Homebrew ownership", async () => {
    vi.stubEnv("POTLUCK_HOMEBREW", "1");
    await expect(manageService("install", 100)).rejects.toThrow("brew services");
  });
});
