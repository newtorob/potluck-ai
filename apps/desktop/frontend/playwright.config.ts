import { defineConfig, devices } from '@playwright/test';

// Keep browser checks separate from the normal development port, which may
// already be serving another project on a developer's machine.
const port = Number(process.env.POTLUCK_E2E_PORT ?? 5175);
if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error('Invalid POTLUCK_E2E_PORT');
const baseURL = `http://127.0.0.1:${port}`;

// End-to-end screen tests: drive the real app in Chromium against the Vite dev
// server, with the cloud API stubbed and the sidecar left to degrade gracefully
// (the app is built to run in a plain browser). Each test asserts behaviour and
// captures a full-page PNG into e2e/screens/ for eyeballing.
export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: [['list'], ['html', { open: 'never' }]],
  // Visual-regression defaults: freeze animations to their final frame and allow
  // a tiny tolerance for anti-aliasing noise so pixel diffs catch real changes
  // without flaking. Baselines live in *-snapshots/ and are macOS+chromium
  // specific (Playwright encodes the platform in the filename).
  expect: {
    toHaveScreenshot: {
      animations: 'disabled',
      scale: 'css',
      maxDiffPixelRatio: 0.01,
    },
  },
  use: {
    baseURL,
    viewport: { width: 1280, height: 800 },
    screenshot: 'only-on-failure',
    trace: 'on-first-retry',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: {
    command: `pnpm dev --port ${port} --strictPort`,
    url: baseURL,
    reuseExistingServer: true,
    timeout: 120_000,
  },
});
