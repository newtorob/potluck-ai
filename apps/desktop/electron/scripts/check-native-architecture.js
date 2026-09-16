'use strict';

const { execFileSync } = require('node:child_process');
const path = require('node:path');

// Electron can cross-package its own runtime, but PyInstaller cannot. Refuse
// an installer whose native sidecars cannot run on the advertised Mac CPU.
module.exports = async function checkNativeArchitecture(context) {
  if (context.electronPlatformName !== 'darwin') return;
  // electron-builder's Arch enum: x64 = 1, arm64 = 3, universal = 4.
  const required = { 1: ['x86_64'], 3: ['arm64'], 4: ['x86_64', 'arm64'] }[context.arch];
  if (!required) throw new Error(`Unsupported Mac architecture: ${context.arch}`);
  const resources = path.resolve(__dirname, '..', 'resources');
  for (const relative of [
    'python/potluck-local/potluck-local',
    'python/potluck-mcp/potluck-mcp',
    'mesh/potluck-mesh',
    'mesh-helper/mesh-helper',
  ]) {
    const binary = path.join(resources, relative);
    const actual = execFileSync('lipo', ['-archs', binary], { encoding: 'utf8' }).trim().split(/\s+/);
    if (!required.every((arch) => actual.includes(arch))) {
      throw new Error(`${relative} supports ${actual.join(', ')}, but this installer needs ${required.join(', ')}. Rebuild native sidecars for the target CPU.`);
    }
  }
};
