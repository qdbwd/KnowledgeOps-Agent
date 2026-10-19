import { expect, test } from '@playwright/test';

test('file approval shows content, survives refresh, and writes once after the real decision', async ({ page }, testInfo) => {
  test.setTimeout(120000);
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  const conversation = await (await page.request.post('/api/conversations', { data: { title: '文件执行确认' } })).json();
  const run = await (await page.request.post(`/api/conversations/${conversation.id}/messages`, {
    data: { content: '保存一份工作区笔记', request_key: crypto.randomUUID() },
  })).json();
  const advance = async () => (await page.request.post(`/api/__offline/file-approval/${run.id}`)).json();
  expect(await advance()).toMatchObject({ status: 'interrupted', outcome: { kind: 'waiting_approval' }, tool_calls: 0 });
  await page.goto(`/chat/${conversation.id}`);
  await expect(page.getByText('新建文件 · 等待批准', { exact: true })).toBeVisible();
  await expect(page.getByText('/work/approval-note.md', { exact: true })).toBeVisible();
  await expect(page.getByText('# 文件审批示例\n批准后才写入。', { exact: true })).toBeVisible();
  await expect(page.getByText('减少重复询问', { exact: true })).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath('file-approval-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.reload();
  await page.getByText('新建文件 · 等待批准', { exact: true }).scrollIntoViewIfNeeded();
  await expect(page.getByText('新建文件 · 等待批准', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '允许一次', exact: true })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('file-approval-mobile.png'), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.getByRole('button', { name: '允许一次', exact: true }).click();
  await expect.poll(async () => (await (await page.request.get(`/api/runs/${run.id}`)).json()).status).toBe('queued');
  expect(await advance()).toMatchObject({ status: 'completed', tool_calls: 1 });
  const output = await page.request.get('/api/workspace/download?path=work/approval-note.md');
  expect(output.ok()).toBeTruthy();
  expect(await output.text()).toBe('# 文件审批示例\n批准后才写入。');
  expect(await advance()).toBeNull();
  await expect(page.getByText('已收到文件写入回执。', { exact: true })).toBeVisible();
  expect(errors).toEqual([]);
});
