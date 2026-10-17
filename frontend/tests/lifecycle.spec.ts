import { expect, test } from '@playwright/test';

test('chat deletion survives navigation and preserves independent source and draft', async ({ page }, info) => {
  test.setTimeout(120000);
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  const seeded = await page.request.post('/api/__offline/lifecycle-seed');
  expect(seeded.ok()).toBe(true);
  const chat = await seeded.json();
  const sources = await (await page.request.get('/api/sources')).json();
  const changes = await (await page.request.get('/api/changesets')).json();
  await page.goto(`/chat/${chat.id}`);
  await page.getByRole('link', { name: '删除会话', exact: true }).click();
  await expect(page.getByText(/以下内容会保留/)).toBeVisible();
  await page.screenshot({ path: info.outputPath('deletion-preview.png'), fullPage: true });
  await page.getByRole('button', { name: '确认删除会话' }).click();
  await expect(page.getByRole('heading', { name: '正在清理会话' })).toBeVisible();
  expect((await page.request.get(`/api/conversations/${chat.id}/messages`)).status()).toBe(404);
  await page.reload();
  await expect(page.getByRole('heading', { name: '正在清理会话' })).toBeVisible();
  const completed = await (await page.request.post(`/api/__offline/lifecycle/${chat.id}`)).json();
  expect(completed).toMatchObject({ status: 'completed' });
  await expect(page.getByRole('heading', { name: '会话已删除', exact: true })).toBeVisible();
  expect(await (await page.request.get('/api/sources')).json()).toEqual(sources);
  expect(await (await page.request.get('/api/changesets')).json()).toEqual(changes);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: info.outputPath('deletion-mobile.png'), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  expect(errors).toEqual([]);
});

test('unfinished task blocks deletion without cancelling it', async ({ page }) => {
  const chat = await (await page.request.post('/api/conversations', { data: { title: '待执行保护' } })).json();
  const run = await (await page.request.post(`/api/conversations/${chat.id}/messages`, {
    data: { content: '等待执行的任务', request_key: crypto.randomUUID() },
  })).json();
  await page.goto(`/chat/${chat.id}/deletion`);
  await expect(page.getByText('当前不能删除', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '确认删除会话' })).toBeDisabled();
  expect((await (await page.request.get(`/api/runs/${run.id}`)).json()).status).toBe('queued');
});
