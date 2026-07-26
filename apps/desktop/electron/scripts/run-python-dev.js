#!/usr/bin/env node
'use strict';

/**
 * scripts/run-python-dev.js
 *
 * Starts the Python sidecar from source for local development.
 * Used by the "dev:python" npm script so that `pnpm dev` works on all
 * three platforms without a shell-specific activation command.
 *
 * Picks the correct Python interpreter from the virtualenv:
 *   *nix / macOS : ../python/.venv/bin/python
 *   Windows      : ../python/.venv/Scripts/python.exe
 *
 * Forwards all stdio so that concurrently can prefix and colour the output.
 * Exits with the same code as the child process.
 */

const { spawn } = require('child_process');
const path = require('path');

// __dirname is apps/desktop/electron/scripts/
// We navigate up two levels to reach apps/desktop/ then into python/.venv
const PYTHON_DIR = path.resolve(__dirname, '..', '..', 'python');

const PYTHON_BIN =
  process.platform === 'win32'
    ? path.join(PYTHON_DIR, '.venv', 'Scripts', 'python.exe')
    : path.join(PYTHON_DIR, '.venv', 'bin', 'python');

const ARGS = ['-m', 'potluck_local.app', '--port', '8321'];

console.log(`[dev:python] Spawning: ${PYTHON_BIN} ${ARGS.join(' ')}`);

const child = spawn(PYTHON_BIN, ARGS, {
  cwd: PYTHON_DIR,
  stdio: 'inherit', // pipe stdin/stdout/stderr straight through to the terminal
  env: {
    ...process.env,
    // Ensure uvicorn's colour logging and auto-reload work correctly.
    PYTHONUNBUFFERED: '1',
    POTLUCK_PORT: '8321',
  },
});

child.on('error', (err) => {
  if (err.code === 'ENOENT') {
    console.error(
      `[dev:python] ERROR: Python interpreter not found at:\n  ${PYTHON_BIN}\n\n` +
        `Create the virtualenv first:\n` +
        `  cd apps/desktop/python && python3 -m venv .venv && source .venv/bin/activate\n` +
        `  pip install -e ".[dev]"\n`
    );
  } else {
    console.error(`[dev:python] Spawn error: ${err.message}`);
  }
  process.exit(1);
});

child.on('exit', (code, signal) => {
  if (signal) {
    // Killed by a signal (e.g. SIGINT from concurrently --kill-others).
    process.exit(0);
  }
  process.exit(code ?? 0);
});

// Forward shutdown signals to the sidecar. Prefer SIGTERM over SIGINT:
// uvicorn's SIGINT path raises KeyboardInterrupt and can skip lifespan
// cleanup, leaving Metal/llama.cpp to GGML_ASSERT on process exit (macOS
// then shows the "python quit unexpectedly / Reopen" dialog).
process.on('SIGINT', () => child.kill('SIGTERM'));
process.on('SIGTERM', () => child.kill('SIGTERM'));
