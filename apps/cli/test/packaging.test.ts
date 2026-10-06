import { execFile } from "node:child_process";
import { mkdtemp, mkdir, readFile, readlink, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { promisify } from "node:util";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

const exec = promisify(execFile);
let directory: string;
beforeEach(async () => { directory = await mkdtemp(join(tmpdir(), "potluck-package-test-")); });
afterEach(async () => { await rm(directory, { recursive: true, force: true }); });

describe("standalone installer", () => {
  async function fixture(version = '1.2.3', arch = process.arch) {
    const source = join(directory, 'source ' + version);
    await mkdir(join(source, 'bin'), { recursive: true });
    await writeFile(join(source, 'bin/potluck'), '#!/bin/sh\nprintf test');
    await writeFile(join(source, 'manifest.json'), JSON.stringify({ schemaVersion: 1, platform: process.platform, arch, version }));
    return source;
  }
  async function install(source: string, prefix: string) {
    // Exercise the exact installer module with real filesystem writes, no models/services.
    const module = resolve('packaging/install.mjs');
    const runner = join(directory, 'invoke.mjs');
    await writeFile(runner, 'const {install}=await import(process.argv[2]); await install(process.argv[3],process.argv[4]);');
    return exec(process.execPath, [runner, module, source, prefix]);
  }
  it("installs into paths with spaces, upgrades the stable link, and retains the old version", async () => {
    const prefix = join(directory, 'local prefix');
    await install(await fixture(), prefix);
    const first = await readlink(join(prefix, 'lib/potluck/current'));
    expect(await readFile(join(prefix, 'bin/potluck'), 'utf8')).toContain('printf test');
    await install(await fixture('1.2.4'), prefix);
    expect(await readlink(join(prefix, 'lib/potluck/current'))).not.toBe(first);
    expect(await readFile(join(first, 'manifest.json'), 'utf8')).toContain('1.2.3');
  });
  it("refuses a wrong architecture and preserves another installer's executable", async () => {
    const prefix = join(directory, 'prefix');
    await expect(install(await fixture('1.2.3', 'wrong' as NodeJS.Architecture), prefix)).rejects.toThrow('not compatible');
    await mkdir(join(prefix, 'bin'), { recursive: true });
    await writeFile(join(prefix, 'bin/potluck'), 'keep this');
    await expect(install(await fixture('1.2.4'), prefix)).rejects.toThrow('different potluck');
    expect(await readFile(join(prefix, 'bin/potluck'), 'utf8')).toBe('keep this');
  });
  it("rejects a current link that escapes its install directory", async () => {
    const prefix = join(directory, 'prefix');
    await mkdir(join(prefix, 'lib/potluck'), { recursive: true });
    await symlink(directory, join(prefix, 'lib/potluck/current'));
    await expect(install(await fixture(), prefix)).rejects.toThrow('unsafe');
  });
});

describe("Homebrew release metadata", () => {
  it("keeps independent platform versions next to their verified archives", async () => {
    const mac = join(directory, 'mac.json');
    const linux = join(directory, 'linux.json');
    const output = join(directory, 'potluck.rb');
    for (const [path, platform, arch, version] of [[mac, 'darwin', 'arm64', '0.1.6'], [linux, 'linux', 'x64', '0.1.7']]) {
      await writeFile(path!, JSON.stringify({ schemaVersion: 1, version, platform, arch,
        url: `https://releases.trypotluck.ai/cli/${version}/potluck-${platform}.tar.gz`, sha256: 'a'.repeat(64) }));
    }
    await exec(process.execPath, ['scripts/generate-homebrew.mjs', output, mac, linux]);
    const formula = await readFile(output, 'utf8');
    expect(formula).toMatch(/on_macos do[\s\S]*?version "0.1.6"[\s\S]*?on_linux do[\s\S]*?version "0.1.7"/);
  });
  it("refuses fake checksums and inconsistent architecture metadata", async () => {
    const artifact = join(directory, 'artifact.json');
    await writeFile(artifact, JSON.stringify({ schemaVersion: 1, version: '1.0.0', platform: 'darwin', arch: 'arm64', url: 'https://releases.trypotluck.ai/cli/test.tar.gz', sha256: 'TODO' }));
    await expect(exec(process.execPath, ['scripts/generate-homebrew.mjs', join(directory, 'potluck.rb'), artifact])).rejects.toThrow('Invalid');
  });
});
