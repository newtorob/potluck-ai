import { test, expect } from '@playwright/test';
import { mockBackends, MEMBER, ADMIN, ADMIN_ROSTER } from './mock';

// Each test drives a real screen, asserts it does the right thing, and captures
// a full-page PNG into e2e/screens/ so the rendered result can be eyeballed.

test.describe('Membership', () => {
  test('signed-out shows the magic-link sign-in', async ({ page }) => {
    await mockBackends(page, { me: null });
    await page.goto('/membership');

    await expect(page.getByRole('heading', { name: 'Membership' })).toBeVisible();
    await expect(page.getByPlaceholder('you@example.com')).toBeVisible();

    await page.screenshot({ path: 'e2e/screens/membership-signed-out.png', fullPage: true });
  });

  test('signed-in with no plan offers the founding checkout, gated honestly', async ({ page }) => {
    await mockBackends(page, { me: MEMBER });
    await page.goto('/membership');

    const cta = page.getByRole('button', { name: /become a founding member/i });
    await expect(cta).toBeVisible();
    await page.screenshot({ path: 'e2e/screens/membership-founding-cta.png', fullPage: true });

    // Behaviour: checkout is not configured (503) -> honest fallback, no crash.
    await cta.click();
    await expect(page.getByRole('alert')).toContainText(/isn't available yet/i);
  });

  test('active plan offers manage-billing', async ({ page }) => {
    await mockBackends(page, {
      me: MEMBER,
      membership: { plan: 'monthly', status: 'active', isFoundingMember: false, currentPeriodEnd: null },
    });
    await page.goto('/membership');

    await expect(page.getByRole('button', { name: /manage billing/i })).toBeVisible();
    await expect(page.getByRole('button', { name: /become a founding member/i })).toHaveCount(0);

    await page.screenshot({ path: 'e2e/screens/membership-active.png', fullPage: true });
  });
});

test.describe('Admin', () => {
  test('an admin sees the member roster', async ({ page }) => {
    await mockBackends(page, { me: ADMIN, adminMembers: ADMIN_ROSTER });
    await page.goto('/admin');

    await expect(page.getByText(/Grant or revoke founding-member status/i)).toBeVisible();
    await expect(page.getByText('member-a@example.com')).toBeVisible();
    await expect(page.getByRole('button', { name: /grant founding/i }).first()).toBeVisible();

    await page.screenshot({ path: 'e2e/screens/admin-roster.png', fullPage: true });
  });

  test('a non-admin is blocked from /admin', async ({ page }) => {
    await mockBackends(page, { me: MEMBER });
    await page.goto('/admin');

    await expect(page.getByText('Admins only')).toBeVisible();

    await page.screenshot({ path: 'e2e/screens/admin-forbidden.png', fullPage: true });
  });
});
