import { test, expect, type Page } from '@playwright/test';
import { mockBackends, MEMBER } from './mock';

const models = [
  { slug: 'qwen3-1.7b-q4', name: 'Qwen 3 1.7B', tier: 'small', size_bytes: 1.28e9 },
  { slug: 'qwen3-8b-q4', name: 'Qwen 3 8B', tier: 'medium', size_bytes: 5.03e9 },
  { slug: 'ministral-8b-q4', name: 'Ministral 8B Instruct', tier: 'medium', size_bytes: 5.13e9 },
].map((m) => ({
  ...m,
  description: '',
  installed: false,
  min_ram_gb: 8,
  min_vram_gb: 4,
  context_window: 32768,
  recommended_tier: m.tier,
  is_featured: false,
}));

async function onboarding(page: Page, theme: 'dark' | 'light') {
  const origin = new URL(test.info().project.use.baseURL as string).origin;
  await page.route('**/*', (route) =>
    new URL(route.request().url()).origin === origin
      ? route.continue()
      : route.fulfill({ status: 503, json: { detail: 'Test backend only' } }),
  );
  await mockBackends(page, { me: MEMBER, onboarding: false, theme });
  const mutations: string[] = [];
  let mode: 'ready' | 'empty' | 'error' = 'ready';
  await page.route(/:8321\//, (route) => {
    const request = route.request(),
      url = new URL(request.url());
    const headers = {
      'access-control-allow-origin': '*',
      'access-control-allow-headers': '*',
      'access-control-allow-methods': '*',
    };
    if (request.method() === 'OPTIONS') return route.fulfill({ status: 204, headers });
    if (request.method() !== 'GET') mutations.push(url.pathname);
    if (url.pathname === '/models/setup-options') {
      if (mode === 'error')
        return route.fulfill({ status: 503, headers, json: { detail: 'Engine unavailable' } });
      const use = url.searchParams.get('use');
      const order = use === 'writing' ? [2, 1, 0] : [1, 2, 0];
      return route.fulfill({
        headers,
        json: {
          schemaVersion: 1,
          useCase: use,
          recommended:
            mode === 'empty'
              ? []
              : order.map((i) => ({
                  id: models[i]!.slug,
                  basis: 'estimate',
                  tokensPerSecond: null,
                })),
          models: models.map((m) => ({
            id: m.slug,
            name: m.name,
            fits: true,
            sizeBytes: m.size_bytes,
            installed: false,
            checked: null,
            useCases: m.slug.startsWith('ministral') ? ['writing'] : ['coding', 'reasoning'],
            recommendationReason: 'Within the estimated GPU memory budget. Speed is untested.',
          })),
        },
      });
    }
    const data: Record<string, unknown> = {
      '/models': models,
      '/health': {
        status: 'ok',
        model_loaded: false,
        active_model_id: null,
        model_restore: { enabled: false },
      },
      '/hardware': {
        os: 'Darwin',
        arch: 'arm64',
        cpu_name: 'Apple M2',
        cpu_cores_physical: 8,
        cpu_cores_logical: 8,
        ram_total_gb: 16,
        ram_available_gb: 12,
        gpu_name: 'Apple M2',
        gpu_vram_gb: 16,
        recommended_tier: 'small',
      },
      '/settings': {},
      '/memory/projects': [],
      '/conversations': [],
    };
    return route.fulfill({ headers, json: data[url.pathname] ?? [] });
  });
  await page.goto('/#/chat');
  await page.getByRole('button', { name: 'Get started' }).click();
  await page.getByRole('button', { name: 'Continue', exact: true }).click();
  await expect(page.getByRole('radio', { name: /Qwen 3 8B/ })).toBeChecked();
  return {
    mutations,
    setMode: (next: typeof mode) => {
      mode = next;
    },
  };
}

for (const theme of ['dark', 'light'] as const) {
  test(`model choices support keyboard selection and fit a small window in ${theme}`, async ({
    page,
  }) => {
    const { mutations } = await onboarding(page, theme);
    await page.getByRole('radio', { name: 'Coding', exact: true }).focus();
    await page.keyboard.press('Space');
    await expect(page.getByRole('radio', { name: /Qwen 3 8B/ })).toBeChecked();
    await page.keyboard.press('ArrowRight');
    await expect(page.getByRole('radio', { name: 'Writing', exact: true })).toBeChecked();
    await expect(page.getByRole('radio', { name: /Ministral/ })).toBeChecked();
    await page.getByRole('radio', { name: 'Coding', exact: true }).check();
    await page.evaluate(() => document.fonts.ready);
    await page.screenshot({ path: `/tmp/potluck-onboarding-${theme}.png`, animations: 'disabled' });
    await page.setViewportSize({ width: 390, height: 620 });
    const action = page.getByRole('button', { name: 'Download and load' });
    await expect(action).toBeInViewport();
    await expect(page.getByRole('radio', { name: /Qwen 3 1.7B/ })).toBeAttached();
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),
    ).toBe(true);
    await page.getByRole('radio', { name: /Qwen 3 1.7B/ }).check();
    await expect(action).toBeInViewport();
    await page.screenshot({
      path: `/tmp/potluck-onboarding-${theme}-narrow.png`,
      animations: 'disabled',
    });
    expect(mutations.filter((p) => p.startsWith('/models/'))).toEqual([]);
  });
}

test('empty and error states recover without downloading', async ({ page }) => {
  const { setMode, mutations } = await onboarding(page, 'dark');
  setMode('empty');
  await page.getByRole('radio', { name: 'Coding', exact: true }).check();
  await expect(page.getByText('No suitable model right now')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Download and load' })).toBeDisabled();
  setMode('error');
  await page.getByRole('button', { name: 'Check again' }).click();
  await expect(page.getByText('Could not get recommendations')).toBeVisible();
  setMode('ready');
  await page.getByRole('button', { name: 'Check again' }).click();
  await expect(page.getByRole('radio', { name: /Qwen 3 8B/ })).toBeChecked();
  await page.getByRole('button', { name: /Skip, I/ }).click();
  await expect(page.getByText('Choose your first model')).toHaveCount(0);
  expect(mutations.filter((p) => p.startsWith('/models/'))).toEqual([]);
});
