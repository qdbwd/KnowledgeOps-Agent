import { test, expect } from '@playwright/test';

test('saved web body has a separate full reader and no HTML download on desktop and mobile', async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/sources');
  const row = page
    .locator('.source-list-row')
    .filter({ has: page.getByRole('link', { name: '网页阅读示例 ↗' }) });
  await expect(row.getByRole('link', { name: '网页阅读示例 ↗' })).toHaveAttribute(
    'href',
    'https://example.org/reader',
  );
  await row.getByRole('link', { name: '查看已保存正文' }).click();
  await expect(page.getByRole('heading', { name: '保存的网页正文' })).toBeVisible();
  await expect(page.getByRole('table')).toContainText('可以读取');
  await expect(page.locator('.document-reader pre')).toContainText('graph.invoke(inputs, config)');
  await expect(page.getByRole('link', { name: '查看原件' })).toHaveCount(0);
  await expect(page.getByRole('link', { name: '原始网页' })).toHaveAttribute('target', '_blank');
  const sourceId = page.url().split('/').at(-1)!;
  const source = await (await page.request.get(`/api/sources/${sourceId}`)).json();
  expect(source.original_available).toBe(false);
  expect(
    (await page.request.get(`/api/source-versions/${source.version_id}/original`)).status(),
  ).toBe(410);
  await page.screenshot({ path: '../artifacts/verification/increment-web-body-desktop.png' });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole('heading', { name: '保存的网页正文' })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.screenshot({ path: '../artifacts/verification/increment-web-body-mobile.png' });
  await page.reload();
  await expect(page.getByRole('table')).toContainText('可以读取');
});
