#!/usr/bin/env node
// Render a tap formula only from real archive metadata, never placeholder checksums.
import { readFile, writeFile } from 'node:fs/promises';
const args = process.argv.slice(2);
if (args.length < 2) throw new Error('Usage: generate-homebrew.mjs <potluck.rb> <archive.tar.gz.json> [...]');
const [output, ...files] = args;
const artifacts = await Promise.all(files.map(async f => JSON.parse(await readFile(f, 'utf8'))));
const version = artifacts[0].version;
const seen = new Set();
for (const a of artifacts) {
  const url = new URL(a.url);
  if (a.schemaVersion !== 1 || a.version !== version || !/^\d+\.\d+\.\d+(?:-[a-zA-Z0-9.-]+)?$/.test(version)
      || !['darwin', 'linux'].includes(a.platform) || !['arm64', 'x64'].includes(a.arch)
      || !/^[a-f0-9]{64}$/.test(a.sha256) || url.protocol !== 'https:' || url.username || url.password || /[\r\n"\\#]/.test(a.url)) throw new Error('Invalid or inconsistent release metadata.');
  const key = a.platform + '-' + a.arch;
  if (seen.has(key)) throw new Error('Duplicate architecture.');
  seen.add(key);
}
const platforms = ['darwin', 'linux'].filter(p => artifacts.some(a => a.platform === p));
const sections = platforms.map(p => {
  const targets = artifacts.filter(a => a.platform === p);
  const requiredArch = targets.length === 1 ? `    depends_on arch: :${targets[0].arch === 'arm64' ? 'arm64' : 'x86_64'}\n` : '';
  const requiredOS = p === 'darwin' ? '    depends_on macos: :sequoia\n' : '';
  return `  on_${p === 'darwin' ? 'macos' : 'linux'} do\n${requiredArch}${requiredOS}${targets.map(a => `    on_${a.arch === 'arm64' ? 'arm' : 'intel'} do\n      url "${a.url}"\n      sha256 "${a.sha256}"\n    end`).join('\n')}\n  end`;
}).join('\n\n');
const platformDependency = platforms.length === 1 ? `  depends_on :${platforms[0] === 'darwin' ? 'macos' : 'linux'}\n` : '';
const archDependency = artifacts.every(a => a.arch === artifacts[0].arch) ? `  depends_on arch: :${artifacts[0].arch === 'arm64' ? 'arm64' : 'x86_64'}\n` : '';
await writeFile(output, `class Potluck < Formula\n  desc "Local AI and machine management from your terminal"\n  homepage "https://trypotluck.ai"\n  version "${version}"\n\n${sections}\n\n  # Preserve the bundled runtime libraries and their relative install names.\n  preserve_rpath\n  skip_clean "libexec"\n  depends_on "node"\n${platformDependency}${archDependency}\n  def install\n    libexec.install Dir["*"]\n    (bin/"potluck").write_env_script libexec/"bin/potluck",\n      POTLUCK_NODE_BIN: Formula["node"].opt_bin/"node",\n      POTLUCK_HOMEBREW: "1",\n      POTLUCK_BREW_BIN: HOMEBREW_PREFIX/"bin/brew"\n  end\n\n  service do\n    run [opt_bin/"potluck", "serve"]\n    keep_alive successful_exit: false\n    restart_delay 5\n    throttle_interval 5\n    stop_timeout 60\n    working_dir Dir.home\n  end\n\n  def caveats\n    <<~EOS\n      Start setup with potluck setup.\n      Optional background runtime: brew services start potluck.\n      Stop a runtime started by potluck up with potluck down before switching.\n      The mesh daemon is installed separately; potluck doctor checks its readiness.\n      This package does not enable gateway access, contribution, or network sharing.\n    EOS\n  end\n\n  test do\n    assert_match version.to_s, shell_output("#{bin}/potluck --version")\n    assert_match "potluck service", shell_output("#{bin}/potluck --help")\n  end\nend\n`);
console.log(output);
