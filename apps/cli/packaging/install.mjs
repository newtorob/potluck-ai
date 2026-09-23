#!/usr/bin/env node
// Local archive installer. Never starts services, installs models, or changes accounts.
import { cp, lstat, mkdir, readFile, readlink, realpath, rename, rm, symlink } from 'node:fs/promises';
import { homedir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { randomBytes } from 'node:crypto';

export async function install(source, prefix) {
  if (process.getuid?.() === 0) throw new Error('Install as your own user, without sudo.');
  if (Number(process.versions.node.split('.')[0]) < 20) throw new Error('Node.js 20 or newer is required.');
  const manifest = JSON.parse(await readFile(join(source, 'manifest.json'), 'utf8'));
  if (manifest.schemaVersion !== 1 || manifest.platform !== process.platform || manifest.arch !== process.arch
      || !/^[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9.-]+)?$/.test(manifest.version)) throw new Error('This archive is not compatible with this computer.');
  prefix = resolve(prefix);
  const base = join(prefix, 'lib/potluck');
  const destination = join(base, manifest.version + '-' + manifest.platform + '-' + manifest.arch);
  const current = join(base, 'current');
  const executable = join(prefix, 'bin/potluck');
  const absent = async p => { try { return await lstat(p); } catch (e) { if (e.code === 'ENOENT') return null; throw e; } };
  for (const folder of [prefix, join(prefix, 'lib'), base, join(prefix, 'bin')]) {
    await mkdir(folder, { recursive: true, mode: 0o755 });
    const stat = await lstat(folder);
    if (!stat.isDirectory() || stat.uid !== process.getuid() || (stat.mode & 0o022)) throw new Error('Install directories must be owned by you and not writable by other users.');
  }
  const existingBin = await absent(executable);
  if (existingBin && (!existingBin.isSymbolicLink() || await readlink(executable) !== join(current, 'bin/potluck'))) throw new Error('A different potluck command already exists at ' + executable);
  const existingCurrent = await absent(current);
  if (existingCurrent && (!existingCurrent.isSymbolicLink() || dirname(resolve(base, await readlink(current))) !== base)) throw new Error('The current installation link is unsafe.');
  if (await absent(destination)) throw new Error('This version is already installed. Use a new release version to upgrade.');
  const staging = destination + '.tmp-' + randomBytes(6).toString('hex');
  const swap = current + '.tmp-' + randomBytes(6).toString('hex');
  try {
    await cp(source, staging, { recursive: true, verbatimSymlinks: true });
    await rename(staging, destination);
    await symlink(destination, swap);
    // Create the stable command first, so an unexpected failure cannot change an existing install.
    if (!existingBin) await symlink(join(current, 'bin/potluck'), executable);
    await rename(swap, current);
  } finally {
    await rm(staging, { recursive: true, force: true });
    await rm(swap, { force: true });
  }
  return { executable, destination };
}

if (process.argv[1] && await realpath(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const args = process.argv.slice(2);
    if (args.length && !(args.length === 2 && args[0] === '--prefix')) throw new Error('Usage: node install.mjs [--prefix <directory>]');
    const result = await install(dirname(fileURLToPath(import.meta.url)), args[1] || join(homedir(), '.local'));
    console.log(`Installed ${result.executable}\nAdd its bin directory to PATH, then run potluck setup.\nOptional login service: potluck service install. After upgrading, rerun service install to use the new version.`);
  } catch (error) { console.error(error.message); process.exitCode = 1; }
}
