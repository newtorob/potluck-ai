'use strict';
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');

// Read only. The authenticated local engine is the sole session-file writer.
// undefined = not migrated; null token = explicit logout, never fall back.
function readMemberSession(cloudOrigin, dataDir = process.env.POTLUCK_DATA_DIR || path.join(os.homedir(), '.potluck')) {
  if (process.platform === 'win32') return undefined;
  const directory = path.join(dataDir, 'auth');
  try {
    const dir = fs.lstatSync(directory);
    if (!dir.isDirectory() || dir.uid !== process.getuid() || (dir.mode & 0o077)) throw new Error('Unsafe member directory');
    const fd = fs.openSync(path.join(directory, 'member-session.json'), fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK);
    try {
      const info = fs.fstatSync(fd);
      if (!info.isFile() || info.uid !== process.getuid() || (info.mode & 0o077) || info.size > 16384) throw new Error('Unsafe member file');
      const value = JSON.parse(fs.readFileSync(fd, 'utf8'));
      if (value.schemaVersion !== 1 || value.cloudOrigin !== cloudOrigin.replace(/\/$/, '')
          || !(value.token === null || (typeof value.token === 'string' && value.token.length > 0 && value.token.length <= 4096))) {
        throw new Error('Incompatible member state');
      }
      if (value.token && value.expiresAt) {
        const expiry = Date.parse(value.expiresAt);
        if (!Number.isFinite(expiry)) throw new Error('Invalid member expiry');
        if (expiry <= Date.now()) return { ...value, token: null };
      }
      return value;
    } finally { fs.closeSync(fd); }
  } catch (err) {
    if (err.code === 'ENOENT') return undefined;
    throw new Error('Cannot read the private member session. Check permissions and cloud configuration.');
  }
}

module.exports = { readMemberSession };
