import { test, expect, type Page } from '@playwright/test';
import { mkdir } from 'node:fs/promises';
import { mockBackends, ADMIN } from './mock';

const output = '/tmp/potluck-ui-review-20260915';
const models = [
  { slug: 'qwen-4b', name: 'Qwen 4B', tier: 'small', installed: true, min_ram_gb: 8, min_vram_gb: 4, size_bytes: 2.3e9, is_featured: false },
  { slug: 'gemma-4b', name: 'Gemma 4B', tier: 'small', installed: false, min_ram_gb: 8, min_vram_gb: 4, size_bytes: 2.3e9, is_featured: true },
  { slug: 'qwen-32b', name: 'Qwen 32B', tier: 'large', installed: false, min_ram_gb: 32, min_vram_gb: 24, size_bytes: 19e9, is_featured: true },
].map((m) => ({ ...m, description: 'A local model for writing, coding, and everyday questions.', context_window: 32768, recommended_tier: m.tier }));

async function desktop(page: Page) {
  const appOrigin = new URL(test.info().project.use.baseURL as string).origin;
  // Every backend request is intercepted. The tests never touch the dogfood
  // sidecar, stored memories, or a production coordinator, even on mutations.
  await page.route('**/*', (route) => {
    const url = new URL(route.request().url());
    if (url.origin === appOrigin) return route.continue();
    return route.fulfill({ status: 503, json: { detail: 'Unavailable in UI test' } });
  });
  await mockBackends(page, { me: ADMIN });
  await page.addInitScript(() => {
    localStorage.setItem('potluck.autoload_on_startup', 'false');
    localStorage.setItem('potluck.contribution_receipts_enabled', 'false');
    (window as unknown as { potluck: unknown }).potluck = {
      platform: 'darwin', arch: 'arm64', getAppVersion: async () => '0.1.5',
      mesh: { request: async (_method: string, path: string) => ({ status: 200, body: path === '/status' ? {
        mesh_status: 'running', node_id: 'self', household_id: 'home', mesh_ip: '100.64.0.1',
        coordinator_connection: 'connected', coordinator_url: 'https://coordinator.example.test',
        peers_online_count: 3, last_error: null, vpn_conflict: false, vpn_detail: '', wg_port_listening: true,
      } : { peers: ['Desktop', 'Home server', 'Laptop'].map((name, i) => ({
        node_id: `peer-${i}`, node_name: name, mesh_ip: `100.64.0.${i + 2}`, reachable: false,
        reachability_mode: null, last_seen_at: null, platform: 'linux',
      })) } }) },
    };
  });
  const settings = {
    runtime: { temperature: 0.7, max_tokens: 2048, context_size: 8192, gpu_layers: -1, threads: 8 },
    privacy: { chat_history_enabled: false, telemetry_enabled: false, crash_reports_enabled: false, memory_enabled: true },
    network: { contributor_mode: false, max_concurrent_jobs: 1, coordinator_url: 'https://coordinator.example.test' },
    app: { theme: 'dark', font_size: 15 }, memory: { enabled: true, auto_extract: false, auto_retrieve: true, extraction_frequency: 5, max_memories: 1000, max_retrieval: 5, min_confidence_for_retrieval: 0.5, retention_days: 0, categories_enabled: [] },
    gateway: { enabled: false, api_key: '', pool_routing: false }, vault: { folder: null, last_scan_at: null, file_count: 0, chunk_count: 0 }, ui: {}, active_project: null, models_path: '', active_model_id: null,
  };
  await page.route(/:8321\//, (route) => {
    const path = new URL(route.request().url()).pathname;
    const data: Record<string, unknown> = {
      '/models': models, '/health': { status: 'ok', model_loaded: true, active_model_id: 'qwen-4b', n_ctx_loaded: 8192 },
      '/hardware': { cpu_name: 'Apple M2', ram_total_gb: 16, ram_available_gb: 9, gpu_name: 'Apple M2', gpu_vram_gb: 16, recommended_tier: 'medium' },
      '/settings': settings, '/memory/projects': [], '/conversations': [],
    };
    return route.fulfill({ json: data[path] ?? [], headers: { 'access-control-allow-origin': '*' } });
  });
  await page.setViewportSize({ width: 1152, height: 768 });
  await mkdir(output, { recursive: true });
}

async function capture(page: Page, name: string) {
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({ path: `${output}/${name}.png`, animations: 'disabled' });
}

async function checkButtonContrast(page: Page) {
  const button = page.getByRole('button', { name: 'Install Gemma 4B' });
  for (const hover of [false, true]) {
    if (hover) await button.hover();
    await button.evaluate((element) => Promise.all(element.getAnimations().map((animation) => animation.finished)));
    const ratio = await button.evaluate((element) => {
      const style = getComputedStyle(element);
      const rgb = (value: string) => value.match(/[\d.]+/g)!.map(Number);
      const color = rgb(style.color);
      const backgroundAt = (node: Element | null): number[] => {
        if (!node) return [255, 255, 255];
        const rgba = rgb(getComputedStyle(node).backgroundColor);
        const alpha = rgba[3] ?? 1;
        if (alpha === 1) return rgba.slice(0, 3);
        const behind = backgroundAt(node.parentElement);
        return rgba.slice(0, 3).map((channel, i) => channel * alpha + behind[i]! * (1 - alpha));
      };
      const background = backgroundAt(element);
      const luminance = (channels: number[]) => channels.reduce((sum, channel, i) => {
        const v = channel / 255;
        return sum + (v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4) * [0.2126, 0.7152, 0.0722][i]!;
      }, 0);
      const [low, high] = [luminance(color.slice(0, 3)), luminance(background)].sort((a, b) => a - b);
      return (high! + 0.05) / (low! + 0.05);
    });
    expect(ratio).toBeGreaterThanOrEqual(4.5);
  }
}

test('models prioritize installed and suitable choices; all models remain available in both themes', async ({ page }) => {
  await desktop(page);
  await page.goto('/#/models');
  await expect(page.getByRole('region', { name: 'On this machine' })).toContainText('Qwen 4B');
  await expect(page.getByText('Suggested download')).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Qwen 32B' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Switch to Common Room (light)' })).toBeInViewport();
  await capture(page, 'models-dark');
  await checkButtonContrast(page);
  await page.getByRole('checkbox', { name: /Show all models/ }).check();
  await expect(page.getByRole('heading', { name: 'Qwen 32B' })).toBeVisible();
  await expect(page.getByText('Exceeds this machine’s listed memory capacity')).toBeVisible();
  await page.getByRole('button', { name: 'Switch to Common Room (light)' }).click();
  await expect(page.locator('html')).toHaveClass(/light/);
  await capture(page, 'models-light');
  await checkButtonContrast(page);
});

test('enrollment, connection, and readiness stay distinct across the household views', async ({ page }) => {
  await desktop(page);
  await page.goto('/#/household');
  await expect(page.getByText('3 enrolled · 0 connected')).toBeVisible();
  await expect(page.getByText(/Your other machines are enrolled but have no active connection/)).toBeVisible();
  await expect(page.getByText(/3 machines have no tunnel/).first()).toBeVisible();
  await capture(page, 'machines-dark');
  await page.getByRole('link', { name: 'Check readiness', exact: true }).click();
  await expect(page.getByText('Mesh port listening')).toBeVisible();
  await expect(page.getByText(/Enrollment alone does not confirm/).first()).toBeVisible();
  await capture(page, 'readiness-dark');
});

test('chat keeps scope visible, options keyboard accessible, and footer reachable at a short height', async ({ page }) => {
  await desktop(page);
  await page.setViewportSize({ width: 1000, height: 620 });
  await page.goto('/#/chat');
  await expect(page.getByText('Chat: This machine')).toBeVisible();
  await expect(page.getByLabel('Edit temperature in settings')).not.toBeVisible();
  await page.locator('summary').filter({ hasText: 'Options' }).click();
  await expect(page.getByLabel('Edit temperature in settings')).toBeVisible();
  await page.getByLabel('Edit temperature in settings').focus();
  await page.keyboard.press('Escape');
  await expect(page.getByLabel('Edit temperature in settings')).not.toBeVisible();
  await expect(page.getByRole('button', { name: 'Switch to Common Room (light)' })).toBeInViewport();
  await capture(page, 'chat-dark');
  await page.locator('summary').filter({ hasText: 'Options' }).click();
  await page.getByLabel('Edit temperature in settings').click();
  await expect(page.getByRole('tab', { name: 'Hardware' })).toHaveAttribute('aria-selected', 'true');
  await page.reload();
  await expect(page.getByRole('tab', { name: 'Hardware' })).toHaveAttribute('aria-selected', 'true');
});

test('contribution controls remain available when the network is unreachable', async ({ page }) => {
  await desktop(page);
  await page.goto('/#/network');
  await expect(page.getByRole('switch', { name: 'Share spare compute with your network' })).toBeVisible();
  await expect(page.getByText(/does not automatically pause/)).toBeVisible();
  await expect(page.getByRole('alert')).toContainText('Network status is unavailable');
  await expect(page.getByRole('link', { name: 'Trusted circles', exact: true }).last()).toBeVisible();
  await capture(page, 'network-dark');
});
