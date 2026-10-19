import { expect, test } from '@playwright/test';

test('delivered result is visible from sources and stays fixed after workfile changes', async ({
  page,
}) => {
  await page.goto('/sources');
  await page.getByRole('link', { name: '已交付笔记.md', exact: true }).click();
  await expect(page).toHaveURL(/\/deliveries\/[a-f0-9-]+$/);
  await expect(page.getByRole('heading', { name: '交付时的正文' })).toBeVisible();
  await expect(page.getByText('后来修改的工作内容，不能出现在固定交付中。')).toHaveCount(0);
  const link = page.getByRole('link', { name: '下载文件' });
  const response = await page.request.get((await link.getAttribute('href'))!);
  expect(await response.text()).toBe('# 交付时的正文\n\n后续工作文件变化不影响这份结果。');
  await page.reload();
  await expect(page.getByRole('heading', { name: '交付时的正文' })).toBeVisible();
  await page.screenshot({ path: '../artifacts/verification/increment-delivery-desktop.png' });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.screenshot({ path: '../artifacts/verification/increment-delivery-mobile.png' });
});
