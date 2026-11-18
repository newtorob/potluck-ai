'use strict';

const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');

async function digest(file) {
  const hash = crypto.createHash('sha256');
  for await (const chunk of fs.createReadStream(file)) hash.update(chunk);
  return hash.digest('hex');
}

function files(root, prefix = '') {
  return fs.readdirSync(path.join(root, prefix), { withFileTypes: true }).flatMap((entry) => {
    const relative = path.join(prefix, entry.name);
    return entry.isDirectory() ? files(root, relative) : [relative];
  }).sort();
}

async function verifyResources(staged, packaged) {
  const expected = files(staged);
  const executables = [
    path.join('potluck-local', 'potluck-local'),
    path.join('potluck-mcp', 'potluck-mcp'),
    path.join('potluck-local', '_internal', 'split-engine', 'llama-server'),
    path.join('potluck-local', '_internal', 'split-engine', 'rpc-server'),
  ];
  if (executables.some((relative) => !expected.includes(relative))
      || JSON.stringify(files(packaged)) !== JSON.stringify(expected)) {
    throw new Error('Packaged pilot Python resources are missing or incomplete.');
  }
  for (const relative of expected) {
    const target = path.join(packaged, relative);
    // Relative internal library links are valid; dependencies outside the
    // portable bundle (or absolute build-machine links) are not.
    if ((fs.lstatSync(target).isSymbolicLink() && path.isAbsolute(fs.readlinkSync(target)))
        || !fs.realpathSync(target).startsWith(fs.realpathSync(packaged) + path.sep)) {
      throw new Error(`Packaged pilot resource links outside its bundle: ${relative}`);
    }
    if (!fs.statSync(target).isFile()
        || await digest(path.join(staged, relative)) !== await digest(path.join(packaged, relative))) {
      throw new Error(`Packaged pilot resource differs from its staged file: ${relative}`);
    }
  }
  for (const relative of executables) {
    fs.accessSync(path.join(packaged, relative), fs.constants.X_OK);
  }
}

module.exports = async function checkPilotResources(context) {
  if (process.env.POTLUCK_COMPANY_BRAIN_PILOT_BUILD !== '1') return;
  const resources = context.electronPlatformName === 'darwin'
    ? path.join(context.appOutDir, `${context.packager.appInfo.productFilename}.app`, 'Contents', 'Resources')
    : path.join(context.appOutDir, 'resources');
  await verifyResources(path.resolve(__dirname, '..', 'resources/python'), path.join(resources, 'python'));
};
module.exports.verifyResources = verifyResources;
