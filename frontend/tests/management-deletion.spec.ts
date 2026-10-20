import { expect, test } from '@playwright/test';

test('article deletion removes unused content and keeps referenced fixed versions readable', async ({
  page,
  request,
}, info) => {
  test.setTimeout(120000);
  const seeded = await (await request.post('/api/__offline/knowledge-deletion-seed')).json();
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto('/knowledge');
  await page.getByRole('button', { name: `删除文章：${seeded.free.title}` }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByRole('button', { name: '确认删除知识文章' })).toBeEnabled();
  await expect(dialog.locator('dd')).toHaveText(['1', '0']);
  await page.screenshot({ path: info.outputPath('article-deletion-desktop.png') });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: info.outputPath('article-deletion-mobile.png') });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  const accepted = page.waitForResponse(
    (r) =>
      r.request().method() === 'POST' && r.url().endsWith(`/knowledge/${seeded.free.id}/deletion`),
  );
  await dialog.getByRole('button', { name: '确认删除知识文章' }).click();
  const job = await (await accepted).json();
  await expect(dialog).toHaveCount(0);
  await expect(page).toHaveURL('/knowledge');
  await expect(page.getByRole('link', { name: seeded.free.title, exact: true })).toHaveCount(0);
  expect((await (await request.post(`/api/__offline/asset-cleanup/${job.id}`)).json()).status).toBe(
    'completed',
  );
  expect((await request.get(`/api/knowledge/${seeded.free.id}`)).status()).toBe(404);
  await page.goto(`/knowledge/reviews/${seeded.free.changeset_id}`);
  await expect(page.getByText(/无引用的审阅正文已清理/)).toBeVisible();
  await page.goto('/knowledge');

  await page.getByRole('button', { name: `删除文章：${seeded.kept.title}` }).click();
  await expect(dialog.locator('dd')).toHaveText(['0', '1']);
  const keptResponse = page.waitForResponse(
    (r) =>
      r.request().method() === 'POST' && r.url().endsWith(`/knowledge/${seeded.kept.id}/deletion`),
  );
  await dialog.getByRole('button', { name: '确认删除知识文章' }).click();
  const keptJob = await (await keptResponse).json();
  expect(
    (await (await request.post(`/api/__offline/asset-cleanup/${keptJob.id}`)).json()).status,
  ).toBe('completed');
  await page.goto(`/knowledge/${seeded.kept.id}?version=${seeded.kept.version_id}`);
  await expect(page.getByText(/此版本因仍被引用而保留/)).toBeVisible();
  await expect(page.getByRole('heading', { name: '被引用的完整正文' })).toBeVisible();
  await expect(page.getByRole('button', { name: '编辑', exact: true })).toBeDisabled();
  await expect(page.getByRole('button', { name: '恢复为此版本' })).toHaveCount(0);
  expect(errors).toEqual([]);
});

test('removing a settled run preserves chat and delivery without a cleanup result page', async ({
  page,
  request,
}, info) => {
  test.setTimeout(120000);
  const seeded = await (await request.post('/api/__offline/task-details-seed')).json();
  await request.post('/api/__offline/assets-ready');
  await page.goto(`/runs/${seeded.run_id}`);
  await page.getByRole('button', { name: /删除运行：/ }).click();
  const dialog = page.getByRole('dialog');
  await expect(dialog.getByRole('button', { name: '确认删除运行记录' })).toBeEnabled();
  await expect(dialog.getByText(/聊天消息及接续信息/)).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: info.outputPath('run-deletion-mobile.png') });
  const accepted = page.waitForResponse(
    (r) => r.request().method() === 'POST' && r.url().endsWith(`/run/${seeded.run_id}/deletion`),
  );
  await dialog.getByRole('button', { name: '确认删除运行记录' }).click();
  const job = await (await accepted).json();
  await expect(page).toHaveURL('/runs');
  await expect(page.locator(`a[href="/runs/${seeded.run_id}"]`)).toHaveCount(0);
  expect((await (await request.post(`/api/__offline/asset-cleanup/${job.id}`)).json()).status).toBe(
    'completed',
  );
  expect((await request.get(seeded.delivery.url)).status()).toBe(200);
  await page.goto(`/runs/${seeded.run_id}/diagnostics`);
  await expect(page.getByRole('heading', { name: '运行记录已删除' })).toBeVisible();
  await expect(page.getByRole('region', { name: '任务诊断', exact: true })).toHaveCount(0);
  await page.getByRole('link', { name: '回到对话' }).click();
  await expect(page.getByText('笔记及草稿已经保存。', { exact: true })).toBeVisible();
});
