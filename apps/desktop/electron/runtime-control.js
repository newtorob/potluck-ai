'use strict';

// Matches the Python runtime-control v1 record. Nothing in this module is sent
// to the renderer: the bearer stays in the main process.
const fs = require('node:fs/promises');
const { constants } = require('node:fs');
const path = require('node:path');
const os = require('node:os');

async function discoverRuntime({ dataDir = process.env.POTLUCK_DATA_DIR || path.join(os.homedir(), '.potluck'), port = 8321 } = {}) {
  if (process.platform === 'win32') return null;
  const directory = path.join(dataDir, 'run');
  let record;
  try {
    const dir = await fs.lstat(directory);
    if (!dir.isDirectory() || dir.uid !== process.getuid()) {
      throw new Error('Runtime directory must be a directory owned by the current user.');
    }
    if (dir.mode & 0o077) {
      // Left by an older Potluck. A runtime makes this directory private
      // before it writes a record, so nothing in a loose one is trustworthy:
      // tighten it the way the runtime does and start a fresh runtime.
      await fs.chmod(directory, 0o700);
      return null;
    }
    const file = await fs.open(path.join(directory, 'runtime.json'), constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
    try {
      const stat = await file.stat();
      if (!stat.isFile() || stat.uid !== process.getuid() || (stat.mode & 0o077) || stat.size > 8192) {
        throw new Error('Runtime record must be a private regular file owned by the current user.');
      }
      record = JSON.parse(await file.readFile('utf8'));
    } finally { await file.close(); }
  } catch (err) {
    if (err.code === 'ENOENT') return null;
    // Never include JSON parse errors: malformed records can contain secrets.
    throw new Error('Cannot read private runtime credentials. Check the runtime directory permissions.');
  }
  if (!record || record.schemaVersion !== 1 || !/^[a-f0-9-]{36}$/.test(record.instanceId)
      || !Number.isInteger(record.pid) || record.pid < 1 || !['cli', 'desktop', 'standalone'].includes(record.owner)
      || typeof record.token !== 'string' || !/^[a-f0-9]{64}$/.test(record.token)
      || record.url !== `http://127.0.0.1:${port}`) {
    throw new Error('Incompatible shared runtime. The desktop app requires port 8321 and control protocol v1.');
  }
  let response;
  try {
    response = await fetch(record.url + '/control', {
      headers: { Authorization: `Bearer ${record.token}` },
      redirect: 'error', signal: AbortSignal.timeout(1000),
    });
  } catch (err) {
    if (err.cause?.code === 'ECONNREFUSED') return null;
    throw new Error('Shared runtime is not responding. Check potluck logs before restarting.');
  }
  if (!response.ok) throw new Error('Shared runtime rejected its control credential.');
  const info = await response.json();
  if (info.schemaVersion !== 1 || info.instanceId !== record.instanceId || info.pid !== record.pid
      || info.owner !== record.owner || info.url !== record.url) throw new Error('Shared runtime identity changed.');
  return record;
}

module.exports = { discoverRuntime };
