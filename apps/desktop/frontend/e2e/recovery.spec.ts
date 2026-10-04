import { expect, test } from '@playwright/test';
import { mockBackends, MEMBER } from './mock';

test('an interrupted household answer restarts on another computer without losing the partial', async ({ page }) => {
  const appOrigin = new URL(test.info().project.use.baseURL as string).origin;
  await page.route('**/*', (route) => new URL(route.request().url()).origin === appOrigin
    ? route.continue() : route.fulfill({ status: 503, json: { detail: 'Test backend only' } }));
  await mockBackends(page, { me: MEMBER });
  await page.addInitScript(() => {
    localStorage.setItem('potluck.autoload_on_startup', 'false');
    localStorage.setItem('potluck.contribution_receipts_enabled', 'false');
    localStorage.setItem('potluck.routing_scope', 'household');
    (window as unknown as { potluck: unknown }).potluck = {
      platform: 'darwin', arch: 'arm64', getAppVersion: async () => '0.1.5',
      mesh: { request: async (_method: string, path: string) => ({ status: 200, body: path === '/status' ? {
        mesh_status: 'running', node_id: 'self', household_id: 'home', mesh_ip: '100.64.0.1',
        coordinator_connection: 'connected', coordinator_url: 'https://coordinator.example.test',
        peers_online_count: 2, last_error: null, vpn_conflict: false, vpn_detail: '', wg_port_listening: true,
      } : { peers: [2, 3].map((i) => ({ node_id: `p${i}`, node_name: `Computer ${i}`,
        mesh_ip: `100.64.0.${i}`, reachable: true, reachability_mode: 'direct', last_seen_at: null,
        platform: 'linux', wg_public_key: '', last_endpoint: null })) } }) },
    };
  });
  const calls: { host: string; body: unknown }[] = [];
  const settings = {
    runtime: { temperature: 0.7, max_tokens: 2048, context_size: 8192, gpu_layers: -1, threads: 8 },
    privacy: { chat_history_enabled: false, telemetry_enabled: false, crash_reports_enabled: false, memory_enabled: true },
    network: { contributor_mode: false, max_concurrent_jobs: 1, coordinator_url: '' },
    app: { theme: 'dark', font_size: 15 }, memory: { enabled: true, auto_extract: false, auto_retrieve: true },
    gateway: { enabled: false, api_key: '', pool_routing: false }, vault: { folder: null }, ui: {}, active_project: null,
  };
  await page.route(/:8321\//, (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const headers = { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*',
      'access-control-allow-methods': '*' };
    if (request.method() === 'OPTIONS') return route.fulfill({ status: 204, headers });
    if (url.pathname === '/peer/capabilities') return route.fulfill({ headers, json: {
      loaded_model: 'same-model', available_for_inference: true, protocol_version: 4,
    } });
    if (url.pathname === '/peer/chat/completions') {
      calls.push({ host: url.hostname, body: request.postDataJSON() });
      const text = url.hostname === '100.64.0.2' ? 'The first answer was interrupted.' : 'The replacement answer is complete.';
      const body = `data: ${JSON.stringify({ choices: [{ delta: { content: text } }] })}\n\n`
        + (url.hostname === '100.64.0.3' ? 'data: [DONE]\n\n' : '');
      return route.fulfill({ headers, contentType: 'text/event-stream', body });
    }
    const data: Record<string, unknown> = {
      '/settings': settings, '/models': [], '/conversations': [], '/memory/projects': [],
      '/health': { status: 'ok', model_loaded: true, active_model_id: 'local-test', n_ctx_loaded: 8192 },
      '/hardware': { cpu_name: 'Test', ram_total_gb: 16, ram_available_gb: 8, recommended_tier: 'medium' },
    };
    return route.fulfill({ headers, json: data[url.pathname] ?? [] });
  });
  await page.goto('/#/chat');
  await page.getByRole('textbox').fill('Explain how recovery works.');
  await page.getByRole('button', { name: 'Send message', exact: true }).click();
  await expect(page.getByText('The first answer was interrupted.', { exact: true })).toBeVisible();
  const restart = page.getByRole('button', { name: 'Restart on another computer' });
  await expect(restart).toBeVisible();
  await page.screenshot({ path: '/tmp/potluck-recovery-interrupted.png' });
  await restart.click();
  await expect(page.getByText('The replacement answer is complete.', { exact: true })).toBeVisible();
  await expect(page.getByText('The first answer was interrupted.', { exact: true })).toBeVisible();
  await expect(page.getByText('Restarted answer · My Machines')).toBeVisible();
  expect(calls.map((c) => c.host)).toEqual(['100.64.0.2', '100.64.0.3']);
  expect(calls[0]?.body).toEqual(calls[1]?.body);
  await page.screenshot({ path: '/tmp/potluck-recovery-completed.png' });
});
