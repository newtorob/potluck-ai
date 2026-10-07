import { test, expect, type Page } from '@playwright/test';
import { mkdir } from 'node:fs/promises';
import { mockBackends, ADMIN } from './mock';

// Trusted circles from both sides, in the real app in a browser: the host
// choosing who can use the machine, and the requester seeing what the circle
// can run. Every backend request is intercepted; nothing reaches a real
// engine or coordinator. Screenshots land in OUTPUT for eyeballing.
const OUTPUT = process.env.POTLUCK_UI_REVIEW_DIR ?? '/tmp/potluck-ui-review-circles';
const COORDINATOR = 'https://coordinator.example.test';
const CIRCLE_ID = '11111111-1111-4111-8111-111111111111';

interface Scenario {
  audience: 'mine' | 'circles' | null;
  contributor: Record<string, unknown>;
  circleModels: { slug: string; machines: number }[];
}

async function desktop(page: Page, scenario: Scenario) {
  const appOrigin = new URL(test.info().project.use.baseURL as string).origin;
  const patches: unknown[] = [];
  await page.route('**/*', (route) => {
    const url = new URL(route.request().url());
    if (url.origin === appOrigin) return route.continue();
    return route.fulfill({ status: 503, json: { detail: 'Unavailable in UI test' } });
  });
  await mockBackends(page, { me: ADMIN });
  await page.addInitScript(() => {
    localStorage.setItem('potluck.autoload_on_startup', 'false');
    localStorage.setItem('potluck.contribution_receipts_enabled', 'false');
    localStorage.setItem('potluck.routing_scope', 'circle');
    (window as unknown as { potluck: unknown }).potluck = {
      platform: 'darwin', arch: 'arm64', getAppVersion: async () => '0.1.9',
      mesh: { request: async (_method: string, path: string) => ({ status: 200, body: path === '/status' ? {
        mesh_status: 'running', node_id: 'self', household_id: 'home', mesh_ip: '100.64.0.1',
        coordinator_connection: 'connected', coordinator_url: 'https://coordinator.example.test',
        peers_online_count: 0, last_error: null, vpn_conflict: false, vpn_detail: '', wg_port_listening: true,
      } : { peers: [] } }) },
    };
  });
  const settings = {
    runtime: { temperature: 0.7, max_tokens: 2048, context_size: 8192, gpu_layers: -1, threads: 8 },
    privacy: { chat_history_enabled: false, telemetry_enabled: false, crash_reports_enabled: false, memory_enabled: true },
    network: { contributor_mode: true, contributor_audience: scenario.audience, max_concurrent_jobs: 1, coordinator_url: COORDINATOR },
    app: { theme: 'dark', font_size: 15 }, memory: { enabled: true, auto_extract: false, auto_retrieve: true, extraction_frequency: 5, max_memories: 1000, max_retrieval: 5, min_confidence_for_retrieval: 0.5, retention_days: 0, categories_enabled: [] },
    gateway: { enabled: false, api_key: '', pool_routing: false }, vault: { folder: null, last_scan_at: null, file_count: 0, chunk_count: 0 }, ui: {}, active_project: null, models_path: '', active_model_id: null,
  };
  await page.route(/:8321\//, (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === '/settings' && request.method() === 'PATCH') {
      const patch = request.postDataJSON() as { network?: Record<string, unknown> };
      patches.push(patch);
      Object.assign(settings.network, patch.network ?? {});
    }
    const data: Record<string, unknown> = {
      '/models': [], '/health': { status: 'ok', model_loaded: true, active_model_id: 'qwen3-4b-instruct-2507-q4', n_ctx_loaded: 8192 },
      '/hardware': { cpu_name: 'Apple M2', ram_total_gb: 16, ram_available_gb: 9, gpu_name: 'Apple M2', gpu_vram_gb: 16, recommended_tier: 'medium' },
      '/settings': settings, '/memory/projects': [], '/conversations': [],
      '/contributor/status': scenario.contributor,
      '/contributor/device-key': { public_key: 'ExampleDevicePublicKeyAAAAAAAAAAAAAAAAAAAAA' },
    };
    return route.fulfill({ json: data[path] ?? [], headers: { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*', 'access-control-allow-methods': '*' } });
  });
  await page.route(`${COORDINATOR}/**`, (route) => {
    const path = new URL(route.request().url()).pathname;
    const cors = { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*', 'access-control-allow-methods': '*' };
    if (route.request().method() === 'OPTIONS') return route.fulfill({ status: 204, headers: cors });
    if (path === '/me/circles') {
      return route.fulfill({ headers: cors, json: { circles: [{ id: CIRCLE_ID, name: 'Friends', created_at: '2026-10-07T12:00:00Z',
        members: [{ member_pubkey: 'ExampleDevicePublicKeyAAAAAAAAAAAAAAAAAAAAA', added_at: '2026-10-07T12:01:00Z' }] }] } });
    }
    if (path === `/me/circles/${CIRCLE_ID}/availability`) {
      return route.fulfill({ headers: cors, json: { circle_id: CIRCLE_ID, models: scenario.circleModels } });
    }
    if (path === '/me/device-keys') return route.fulfill({ status: 201, headers: cors, json: {} });
    return route.fulfill({ status: 503, headers: cors, json: { detail: 'Unavailable in UI test' } });
  });
  await page.setViewportSize({ width: 1152, height: 768 });
  await mkdir(OUTPUT, { recursive: true });
  return patches;
}

async function capture(page: Page, name: string) {
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: `${OUTPUT}/${name}.png`, animations: 'disabled' });
}

const connected = {
  enabled: true, running: true, state: 'connected', peer_id: 'p', connected_since: null, last_heartbeat_at: null,
  heartbeats_sent: 4, reconnects: 0, jobs_served: 3, jobs_in_flight: 0, last_error: null, last_error_detail: null,
  coordinator_host: 'coordinator.example.test', binary_hash_source: 'file', max_concurrent_jobs: 1,
  has_member_session: true, audiences: ['circle'],
};

test('a host chooses who can use the machine and sees who it is shared with', async ({ page }) => {
  const patches = await desktop(page, { audience: null, contributor: { ...connected, state: 'disabled', audiences: null }, circleModels: [] });
  await page.goto('/#/network');
  const group = page.getByRole('group', { name: 'Who can use this machine' });
  await expect(group).toBeVisible();
  // Nothing chosen in a release build: only the member's own machines, and no
  // connection line because the engine has not joined the coordinator.
  await expect(group.getByRole('radio', { name: /Only my own machines/ })).toBeChecked();
  await expect(group.getByRole('radio', { name: /Everyone on the network/ })).toHaveCount(0);
  await expect(page.getByText(/Sharing with your trusted circles/)).toHaveCount(0);
  await capture(page, 'network-sharing-mine');

  await group.getByRole('radio', { name: /My trusted circles too/ }).check();
  await expect(group.getByRole('radio', { name: /My trusted circles too/ })).toBeChecked();
  expect(patches).toContainEqual({ network: { contributor_audience: 'circles' } });
});

test('a machine shared with circles says so under the toggle', async ({ page }) => {
  await desktop(page, { audience: 'circles', contributor: connected, circleModels: [] });
  await page.goto('/#/network');
  const group = page.getByRole('group', { name: 'Who can use this machine' });
  await expect(group.getByRole('radio', { name: /My trusted circles too/ })).toBeChecked();
  await expect(group).toContainText('It is never offered open pool work.');
  await expect(page.getByRole('status').filter({ hasText: 'Sharing with your trusted circles' })).toContainText('3 jobs served');
  await capture(page, 'network-sharing-circles');
});

test('a host is told when the network cannot yet limit sharing to circles', async ({ page }) => {
  await desktop(page, { audience: 'circles', circleModels: [],
    contributor: { ...connected, state: 'rejected', last_error: 'coordinator_outdated', audiences: ['circle'] } });
  await page.goto('/#/network');
  await expect(page.getByRole('status').filter({ hasText: 'nothing is being shared' })).toBeVisible();
  await capture(page, 'network-sharing-outdated');
});

test('a requester sees the models the circle can run and asks for one of them', async ({ page }) => {
  await desktop(page, { audience: 'mine', contributor: connected,
    circleModels: [{ slug: 'qwen3-4b-instruct-2507-q4', machines: 2 }, { slug: 'gemma-3-12b-it-q4', machines: 1 }] });
  await page.goto('/#/chat');
  const picker = page.getByRole('combobox', { name: 'Model this circle will answer with' });
  await expect(picker).toBeVisible();
  await expect(picker).toHaveValue('qwen3-4b-instruct-2507-q4');
  await expect(picker.getByRole('option')).toHaveText(['qwen3-4b-instruct-2507-q4 (2 machines)', 'gemma-3-12b-it-q4']);
  await picker.selectOption('gemma-3-12b-it-q4');
  await expect(picker).toHaveValue('gemma-3-12b-it-q4');
  await capture(page, 'chat-circle-models');
});

test('a requester is told when no circle machine is sharing', async ({ page }) => {
  await desktop(page, { audience: 'mine', contributor: connected, circleModels: [] });
  await page.goto('/#/chat');
  await expect(page.getByText('No machine in this circle is sharing a model right now.')).toBeVisible();
  await capture(page, 'chat-circle-empty');
});
