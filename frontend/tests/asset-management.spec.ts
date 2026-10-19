import { expect, test } from '@playwright/test';

test('source deletion previews dependencies and persists progress across refresh', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  const seeded = await (await page.request.post('/api/__offline/asset-seed')).json();
  await page.goto('/sources');
  await page.getByRole('button', { name: `删除材料 ${seeded.source.title}` }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByRole('button', { name: '确认删除材料' })).toBeEnabled();
  await expect(dialog.locator('dd').first()).toHaveText('1');
  await expect(dialog.locator('dd').last()).toHaveText('0');
  await page.screenshot({ path: '../artifacts/verification/increment-asset-delete-desktop.png' });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.screenshot({ path: '../artifacts/verification/increment-asset-delete-mobile.png' });
  const response = page.waitForResponse((res) => res.request().method() === 'POST' && res.url().endsWith(`/source/${seeded.source.id}/deletion`));
  await dialog.getByRole('button', { name: '确认删除材料' }).click();
  const job = await (await response).json();
  await page.reload();
  await expect(page.getByRole('region', { name: '最近的删除' }).getByText(seeded.source.title, { exact: true })).toBeVisible();
  expect((await (await page.request.post(`/api/__offline/asset-cleanup/${job.id}`)).json()).status).toBe('completed');
  await expect(page.getByRole('region', { name: '最近的删除' }).getByText('已完成 · 回收 1 个版本 · 保留 0 个仍被使用的版本').first()).toBeVisible();
  expect((await page.request.get(`/api/source-versions/${seeded.source.version_id}/content`)).status()).toBe(404);

  await page.getByRole('button', { name: `删除材料 ${seeded.protected.title}` }).click();
  await expect(dialog.locator('dd').last()).toHaveText('1');
  await expect(dialog.getByText(/对应内容保持可读/)).toBeVisible();
  const protectedResponse = page.waitForResponse((res) => res.request().method() === 'POST' && res.url().endsWith(`/source/${seeded.protected.id}/deletion`));
  await dialog.getByRole('button', { name: '确认删除材料' }).click();
  const retained = await (await protectedResponse).json();
  expect((await (await page.request.post(`/api/__offline/asset-cleanup/${retained.id}`)).json()).status).toBe('completed');
  await page.goto(`/sources/${seeded.protected.id}?version=${seeded.protected.version_id}`);
  await expect(page.getByText(/被引用正文/)).toBeVisible();
  expect(errors).toEqual([]);
});

test('delivery can be removed without deleting its working file', async ({ page }) => {
  const seeded = await (await page.request.post('/api/__offline/asset-seed')).json();
  await page.goto('/sources');
  await page.getByRole('button', { name: `删除已交付文件 ${seeded.delivery.name}` }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByText(/工作区中的原工作文件另行管理/)).toBeVisible();
  const response = page.waitForResponse((res) => res.request().method() === 'POST' && res.url().endsWith(`/delivery/${seeded.delivery.id}/deletion`));
  await dialog.getByRole('button', { name: '确认删除文件' }).click();
  const job = await (await response).json();
  expect((await (await page.request.post(`/api/__offline/asset-cleanup/${job.id}`)).json()).status).toBe('completed');
  await page.reload();
  await expect(page.getByRole('link', { name: seeded.delivery.name, exact: true })).toHaveCount(0);
  expect((await page.request.get(seeded.delivery.url)).status()).toBe(404);
  const working = await page.request.get(`/api/workspace/preview?path=${encodeURIComponent(seeded.delivery.path)}`);
  expect(working.status()).toBe(200);
  expect((await working.json()).text).toContain('# 本次交付');
});
