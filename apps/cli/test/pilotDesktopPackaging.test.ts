import { createRequire } from 'node:module';
import { mkdtemp, mkdir, rm, writeFile, chmod, symlink } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
const require = createRequire(import.meta.url);
const check = require(resolve('../desktop/electron/scripts/check-pilot-resources.js')) as {
  verifyResources: (staged: string, packaged: string) => Promise<void>;
};
let root: string;
beforeEach(async () => { root = await mkdtemp(join(tmpdir(), 'potluck-pilot-resources-')); });
afterEach(async () => { await rm(root, { recursive: true, force: true }); });
async function fixture(name: string) {
  const dir = join(root, name);
  for (const [relative, body] of [
    ['potluck-local/potluck-local', 'frozen local binary'],
    ['potluck-mcp/potluck-mcp', 'frozen MCP binary'],
    ['potluck-local/_internal/split-engine/llama-server', 'native server'],
    ['potluck-local/_internal/split-engine/rpc-server', 'native RPC server'],
    ['potluck-local/_internal/transformers/models/auto/module.py', 'source needed at runtime'],
  ]) {
    const file = join(dir, relative!); await mkdir(resolve(file, '..'), { recursive: true }); await writeFile(file, body!); await chmod(file, 0o755);
  }
  return dir;
}
async function framework(dir: string) {
  const root = join(dir, 'potluck-local/_internal/Python.framework');
  await mkdir(join(root, 'Versions/3.11/Resources'), { recursive: true });
  await writeFile(join(root, 'Versions/3.11/Resources/Info.plist'), 'framework metadata');
  await symlink('3.11', join(root, 'Versions/Current'));
  await symlink('Versions/Current/Resources', join(root, 'Resources'));
  return root;
}
describe('packaged pilot resource integrity', () => {
  it('accepts exact external resources including loose Python sources', async () => {
    await expect(check.verifyResources(await fixture('stage'), await fixture('package'))).resolves.toBeUndefined();
  });
  it('rejects resources placed only inside app.asar instead of the external path', async () => {
    const stage = await fixture('stage'); const packaged = join(root, 'resources/python'); await mkdir(packaged, { recursive: true });
    await writeFile(join(root, 'resources/app.asar'), 'contains misplaced Python');
    await expect(check.verifyResources(stage, packaged)).rejects.toThrow('missing or incomplete');
  });
  it('rejects a changed runtime resource even with the same filenames', async () => {
    const stage = await fixture('stage'); const packaged = await fixture('package');
    await writeFile(join(packaged, 'potluck-mcp/potluck-mcp'), 'different bytes');
    await expect(check.verifyResources(stage, packaged)).rejects.toThrow('differs');
  });
  it('rejects an executable whose copy lost execute permission', async () => {
    const stage = await fixture('stage'); const packaged = await fixture('package');
    await chmod(join(packaged, 'potluck-mcp/potluck-mcp'), 0o644);
    await expect(check.verifyResources(stage, packaged)).rejects.toThrow();
  });
  it('rejects identical bytes reached through a link outside the portable bundle', async () => {
    const stage = await fixture('stage'); const packaged = await fixture('package');
    const target = join(packaged, 'potluck-mcp/potluck-mcp'); await rm(target);
    await symlink(join(stage, 'potluck-mcp/potluck-mcp'), target);
    await expect(check.verifyResources(stage, packaged)).rejects.toThrow('links outside');
  });
  it('rejects an empty staged tree so empty-to-empty cannot pass', async () => {
    const a = join(root, 'a'); const b = join(root, 'b'); await mkdir(a); await mkdir(b);
    await expect(check.verifyResources(a, b)).rejects.toThrow('missing or incomplete');
  });
  it('accepts matching internal framework directory aliases without skipping target bytes', async () => {
    const stage = await fixture('stage'); const packaged = await fixture('package');
    await framework(stage); const target = await framework(packaged);
    await expect(check.verifyResources(stage, packaged)).resolves.toBeUndefined();
    await writeFile(join(target, 'Versions/3.11/Resources/Info.plist'), 'changed framework metadata');
    await expect(check.verifyResources(stage, packaged)).rejects.toThrow('differs');
  });
  it('rejects a framework directory alias escaping the portable bundle', async () => {
    const stage = await fixture('stage'); const packaged = await fixture('package');
    const original = await framework(stage); const target = await framework(packaged);
    await rm(join(target, 'Resources'));
    await symlink(join(original, 'Versions/3.11/Resources'), join(target, 'Resources'));
    await expect(check.verifyResources(stage, packaged)).rejects.toThrow('links outside');
  });
});
