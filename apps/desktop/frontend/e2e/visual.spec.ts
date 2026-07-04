import { test, expect } from '@playwright/test';
import { mockBackends, MEMBER, ADMIN, ADMIN_ROSTER } from './mock';

// Pixel-level visual regression for the launch screens. Baselines are committed
// under visual.spec.ts-snapshots/; regenerate with `pnpm e2e:update` after an
// intentional UI change. Behavioural coverage lives in screens.spec.ts.

/**
 * Make the paint deterministic before snapping: wait for the anchor element and
 * fonts, then hard-disable animations/transitions and pin the "fade-in-up" entry
 * effect to its final state. (Playwright's animations:'disabled' fast-forwards
 * finite animations, but the tall entry-animated content can still settle a few
 * pixels off; forcing the end state removes that vertical jitter.)
 */
async function settle(page: import('@playwright/test').Page, anchor: () => Promise<void>) {
  await anchor();
  await page.addStyleTag({
    content: `
      *, *::before, *::after {
        animation: none !important;
        transition: none !important;
      }
      .animate-fade-in-up { opacity: 1 !important; transform: none !important; }
    `,
  });
  await page.evaluate(() => document.fonts.ready);
}

test.describe('visual regression', () => {
  test('membership - signed out', async ({ page }) => {
    await mockBackends(page, { me: null });
    await page.goto('/membership');
    await settle(page, async () => {
      await expect(page.getByPlaceholder('you@example.com')).toBeVisible();
    });
    await expect(page).toHaveScreenshot('membership-signed-out.png', { fullPage: true });
  });

  test('membership - founding CTA', async ({ page }) => {
    await mockBackends(page, { me: MEMBER });
    await page.goto('/membership');
    await settle(page, async () => {
      await expect(page.getByRole('button', { name: /become a founding member/i })).toBeVisible();
    });
    await expect(page).toHaveScreenshot('membership-founding-cta.png', { fullPage: true });
  });

  test('membership - active plan', async ({ page }) => {
    await mockBackends(page, {
      me: MEMBER,
      membership: { plan: 'monthly', status: 'active', isFoundingMember: false, currentPeriodEnd: null },
    });
    await page.goto('/membership');
    await settle(page, async () => {
      await expect(page.getByRole('button', { name: /manage billing/i })).toBeVisible();
    });
    await expect(page).toHaveScreenshot('membership-active.png', { fullPage: true });
  });

  test('admin - roster', async ({ page }) => {
    await mockBackends(page, { me: ADMIN, adminMembers: ADMIN_ROSTER });
    await page.goto('/admin');
    await settle(page, async () => {
      await expect(page.getByText('member-a@example.com')).toBeVisible();
    });
    await expect(page).toHaveScreenshot('admin-roster.png', { fullPage: true });
  });

  test('admin - forbidden', async ({ page }) => {
    await mockBackends(page, { me: MEMBER });
    await page.goto('/admin');
    await settle(page, async () => {
      await expect(page.getByText('Admins only')).toBeVisible();
    });
    await expect(page).toHaveScreenshot('admin-forbidden.png', { fullPage: true });
  });
});
