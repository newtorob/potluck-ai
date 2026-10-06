#!/usr/bin/env node
/** Build a native archive from compiled CLI + a freshly built/signed runtime. No uploads. */
import { cp, mkdir, mkdtemp, readdir, readFile, realpath, rm, stat, writeFile } from 'node:fs/promises';
import { createReadStream } from 'node:fs';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { dirname, join, relative, resolve, sep } from 'node:path';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../../..');
const args = process.argv.slice(2);
const options = {};
for (let i = 0; i < args.length; i += 2) {
  if (!['--runtime', '--out', '--version', '--base-url', '--provenance'].includes(args[i]) || !args[i + 1] || args[i + 1].startsWith('--')) throw new Error('Usage: package-release.mjs --runtime <bundle-directory> --out <directory> [--version <version>] [--base-url <https-release-directory>] [--provenance <source-metadata.json>]');
  options[args[i].slice(2)] = args[i + 1];
}
if (!['darwin', 'linux'].includes(process.platform) || !['arm64', 'x64'].includes(process.arch)) throw new Error('Build on a supported native Mac/Linux host.');
if (!options.runtime || !options.out) throw new Error('--runtime and --out are required. Build the native runtime first.');
const cli = join(root, 'apps/cli');
const pkg = JSON.parse(await readFile(join(cli, 'package.json'), 'utf8'));
const version = options.version || pkg.version;
if (!/^\d+\.\d+\.\d+(?:-[a-zA-Z0-9.-]+)?$/.test(version)) throw new Error('Invalid version.');
// Exported source trees can carry metadata captured on the original checkout.
// Do not label a remote build with a newly initialized Git repository's revision.
const provenance = options.provenance ? JSON.parse(await readFile(options.provenance, 'utf8')) : {
  source: execFileSync('git', ['rev-parse', 'HEAD'], { cwd: root, encoding: 'utf8' }).trim(),
  dirty: !!execFileSync('git', ['status', '--porcelain'], { cwd: root, encoding: 'utf8' }).trim(),
};
if (!/^[a-f0-9]{40,64}$/.test(provenance.source) || typeof provenance.dirty !== 'boolean') throw new Error('Invalid source provenance.');
const runtime = await realpath(options.runtime);
const executable = join(runtime, 'potluck-local');
if (!(await stat(executable)).isFile() || !(await stat(join(runtime, '_internal'))).isDirectory()) throw new Error('Expected a PyInstaller onedir runtime bundle.');
if (process.platform === 'linux' && process.arch === 'x64') {
  execFileSync(process.env.PYTHON_BIN || 'python3', [
    join(root, 'apps/desktop/python/scripts/vendor_gpu_backend.py'),
    '--check', '--root', join(runtime, '_internal/llama_cpp'),
  ], { stdio: 'inherit' });
}
const splitRoot = join(runtime, '_internal/split-engine');
const splitPin = JSON.parse(await readFile(join(root, 'apps/desktop/python/potluck_local/data/split-engine.json'), 'utf8'));
let splitEngine;
try {
  splitEngine = JSON.parse(await readFile(join(splitRoot, 'manifest.json'), 'utf8'));
  if (['schemaVersion', 'protocol', 'commit'].some(key => splitEngine[key] !== splitPin[key])
      || !(process.platform === 'darwin' ? ['metal'] : ['cpu', 'cuda']).includes(splitEngine.backend)) throw new Error();
  for (const name of ['llama-server', 'rpc-server']) {
    const binary = join(splitRoot, name);
    const info = await stat(binary);
    const native = execFileSync('file', ['-b', binary], { encoding: 'utf8' });
    if (!info.isFile() || !(info.mode & 0o111)
        || !(process.platform === 'darwin' ? /Mach-O/.test(native) : /ELF/.test(native))
        || !(process.arch === 'arm64' ? /arm64|aarch64/.test(native) : /x86[_-]64/.test(native))) throw new Error();
  }
  await stat(join(splitRoot, 'LICENSE'));
} catch {
  throw new Error('Runtime is missing its pinned native split engine. Run scripts/build-split-engine.py, then rebuild the Python bundle before packaging.');
}
// Reject mislabelled native builds and runtime links that escape into the build machine.
const nativeType = execFileSync('file', ['-b', executable], { encoding: 'utf8' });
if (!(process.platform === 'darwin' ? /Mach-O/.test(nativeType) : /ELF/.test(nativeType))
    || !(process.arch === 'arm64' ? /arm64|aarch64/.test(nativeType) : /x86[_-]64/.test(nativeType))) throw new Error('Runtime architecture does not match this build host.');
async function auditLinks(directory) {
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const full = join(directory, entry.name);
    if (entry.isSymbolicLink()) {
      const target = await realpath(full);
      if (target !== runtime && !target.startsWith(runtime + sep)) throw new Error('Runtime contains an external symlink: ' + relative(runtime, full));
    } else if (entry.isDirectory()) await auditLinks(full);
  }
}
await auditLinks(runtime);
execFileSync('pnpm', ['build'], { cwd: cli, stdio: 'inherit' });
const out = resolve(options.out);
await mkdir(out, { recursive: true });
const stage = await mkdtemp(join(tmpdir(), 'potluck-package-'));
const name = `potluck-cli-${version}-${process.platform}-${process.arch}`;
const bundle = join(stage, name);
try {
  await mkdir(join(bundle, 'bin'), { recursive: true });
  await mkdir(join(bundle, 'libexec/cli'), { recursive: true });
  await cp(join(cli, 'dist'), join(bundle, 'libexec/cli/dist'), { recursive: true, filter: p => !p.endsWith('.map') });
  await writeFile(join(bundle, 'libexec/cli/package.json'), JSON.stringify({ name: pkg.name, version, type: 'module', engines: pkg.engines }, null, 2) + '\n');
  await cp(runtime, join(bundle, 'libexec/runtime'), { recursive: true, verbatimSymlinks: true });
  await cp(join(cli, 'packaging/DOWNLOAD.md'), join(bundle, 'README.md'));
  await cp(join(cli, 'packaging/install.mjs'), join(bundle, 'install.mjs'));
  await writeFile(join(bundle, 'bin/potluck'), `#!/bin/sh\nset -eu\n# Resolve both ~/.local/bin and Homebrew symlinks before finding the bundle.\nself=$0\nwhile [ -L "$self" ]; do\n  dir=$(CDPATH= cd -P -- "$(dirname -- "$self")" && pwd)\n  target=$(readlink "$self")\n  case "$target" in /*) self=$target ;; *) self=$dir/$target ;; esac\ndone\nroot=$(CDPATH= cd -P -- "$(dirname -- "$self")/.." && pwd)\nexec "\${POTLUCK_NODE_BIN:-node}" "$root/libexec/cli/dist/index.js" "$@"\n`, { mode: 0o755 });
  const manifest = { schemaVersion: 1, version, platform: process.platform, arch: process.arch, node: '>=20', source: provenance.source, dirty: provenance.dirty,
    runtimeSha256: await digest(executable), splitEngine, builtAt: new Date().toISOString() };
  await writeFile(join(bundle, 'manifest.json'), JSON.stringify(manifest, null, 2) + '\n');
  const archive = join(out, name + '.tar.gz');
  // tar keeps relative PyInstaller links and signed Mach-O bytes intact.
  execFileSync('tar', ['-czf', archive, '-C', stage, name], { env: { ...process.env, COPYFILE_DISABLE: '1' } });
  const sha256 = await digest(archive);
  await writeFile(archive + '.sha256', sha256 + '  ' + name + '.tar.gz\n');
  const base = new URL(options['base-url'] || `https://releases.trypotluck.ai/cli/${version}/`);
  if (base.protocol !== 'https:' || base.username || base.password || base.search || base.hash || !base.pathname.endsWith('/')) throw new Error('Release base URL must be an HTTPS directory URL.');
  const artifact = { ...manifest, archive: name + '.tar.gz', sha256, url: new URL(name + '.tar.gz', base).href };
  await writeFile(archive + '.json', JSON.stringify(artifact, null, 2) + '\n');
  console.log(JSON.stringify(artifact, null, 2));
} finally { await rm(stage, { recursive: true, force: true }); }

async function digest(file) {
  const hash = createHash('sha256');
  for await (const chunk of createReadStream(file)) hash.update(chunk);
  return hash.digest('hex');
}
