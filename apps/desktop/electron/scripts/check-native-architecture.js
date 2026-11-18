'use strict';

const { execFileSync } = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');
const crypto = require('node:crypto');

// Electron can cross-package its own runtime, but PyInstaller cannot. Refuse
// an installer whose native sidecars cannot run on the advertised Mac CPU.
module.exports = async function checkNativeArchitecture(context) {
  if (process.env.POTLUCK_COMPANY_BRAIN_PILOT_BUILD === '1') {
    const root = path.resolve(__dirname, '..', 'resources/python/potluck-local/_internal');
    const marker = JSON.parse(fs.readFileSync(path.join(root, 'company-brain-pilot-build.json')));
    const pin = JSON.parse(fs.readFileSync(path.join(root, 'potluck_local/data/split-engine.json')));
    const platform = { linux: 'Linux', darwin: 'Darwin' }[context.electronPlatformName];
    const machine = { linux: 'x86_64', darwin: 'arm64' }[context.electronPlatformName];
    const arch = { linux: 1, darwin: 3 }[context.electronPlatformName];
    if (!platform || context.arch !== arch || marker.version !== 'company-brain-pilot-build-v1'
        || marker.platform !== platform || marker.machine !== machine
        || marker.engine_commit !== pin.commit || marker.model_weights_included !== false
        || marker.backend !== (platform === 'Linux' ? 'cpu' : 'metal')) {
      throw new Error('Company Brain pilot resources do not match the target platform.');
    }
    for (const name of ['manifest.json', 'LICENSE', 'llama-server', 'rpc-server']) {
      const file = path.join(root, 'split-engine', name);
      if (!fs.lstatSync(file).isFile() || crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex') !== marker.files[name]) {
        throw new Error(`Company Brain pilot resource changed after validation: ${name}`);
      }
    }
  }
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
