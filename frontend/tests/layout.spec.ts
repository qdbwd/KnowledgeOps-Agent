import { test, expect } from '@playwright/test';

test('wide chat is centred in the space beside the sidebar; mobile retains navigation', async ({
  page,
}) => {
  for (const width of [1440, 2048, 2560]) {
    await page.setViewportSize({ width, height: 1000 });
    await page.goto('/');
    await expect(page.getByRole('heading', { name: '从一个问题，或一份材料开始。' })).toBeVisible();
    const sidebar = (await page.locator('.sidebar').boundingBox())!;
    const composer = (await page.locator('.composer').boundingBox())!;
    expect(Math.abs(composer.x + composer.width / 2 - (width + sidebar.width) / 2)).toBeLessThan(2);
    await expect(page.getByRole('button', { name: '发送', exact: true })).toBeDisabled();
  }
  await page.screenshot({ path: '../artifacts/verification/layout-wide.png', fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.locator('.new-chat')).toBeVisible();
  await expect(page.locator('.sidebar-bottom')).toHaveCount(0);
  await expect(page.locator('.composer-model')).toBeVisible();
  await page.locator('.mobile-conversations summary').click();
  await expect(page.locator('.mobile-conversations').getByRole('link').first()).toBeVisible();
  await page.locator('.mobile-conversations summary').click();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.screenshot({ path: '../artifacts/verification/layout-mobile.png', fullPage: true });
});

test('skills are discoverable from the capabilities page without model calls', async ({ page }) => {
  await page.goto('/tools');
  await page.getByText(/^已发现的任务技能（\d+）$/).click();
  await expect(
    page.getByRole('checkbox', { name: '启用技能 code-documentation', exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole('checkbox', { name: '启用技能 academic-paper-review', exact: true }),
  ).toBeVisible();
});
