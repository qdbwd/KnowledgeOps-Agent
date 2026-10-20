import { expect, test } from '@playwright/test';

test('删除预览说明临时资产与保留资产，完成后实际释放无依赖材料', async ({
  page,
  request,
}, info) => {
  test.setTimeout(120000);
  const seeded = await (await request.post('/api/__offline/lifecycle-seed?temporary=true')).json();
  await page.goto(`/chat/${seeded.id}`);
  await page.getByRole('button', { name: '删除会话', exact: true }).click();
  await expect(
    page.getByText('将回收：1 个材料版本、1 份临时草稿。', { exact: true }),
  ).toBeVisible();
  await expect(page.getByText(/继续保留：1 个材料版本、1 份草稿/)).toBeVisible();
  await page.screenshot({ path: info.outputPath('deletion-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: info.outputPath('deletion-mobile.png'), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  );
  await page.getByRole('button', { name: '确认删除会话' }).click();
  await expect(page).toHaveURL('/');
  expect(await (await request.post(`/api/__offline/lifecycle/${seeded.id}`)).json()).toMatchObject({
    status: 'completed',
  });
  await expect(page.getByRole('dialog')).toHaveCount(0);
  expect((await request.get(`/api/sources/${seeded.temporary_source_id}`)).status()).toBe(404);
  expect((await request.get(`/api/changesets/${seeded.temporary_draft_id}`)).status()).toBe(404);
});

test('chat deletion survives navigation and preserves independent source and draft', async ({
  page,
}, info) => {
  test.setTimeout(120000);
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  const seeded = await page.request.post('/api/__offline/lifecycle-seed');
  expect(seeded.ok()).toBe(true);
  const chat = await seeded.json();
  const sources = await (await page.request.get('/api/sources')).json();
  const changes = await (await page.request.get('/api/changesets')).json();
  await page.goto(`/chat/${chat.id}`);
  await page.getByRole('button', { name: '删除会话', exact: true }).click();
  await expect(page.getByText(/以下内容会保留/)).toBeVisible();
  await page.screenshot({ path: info.outputPath('deletion-preview.png'), fullPage: true });
  await page.getByRole('button', { name: '确认删除会话' }).click();
  await expect(page).toHaveURL('/');
  expect((await page.request.get(`/api/conversations/${chat.id}/messages`)).status()).toBe(404);
  await page.reload();
  await expect(page).toHaveURL('/');
  const completed = await (await page.request.post(`/api/__offline/lifecycle/${chat.id}`)).json();
  expect(completed).toMatchObject({ status: 'completed' });
  await expect(page.getByRole('dialog')).toHaveCount(0);
  expect(await (await page.request.get('/api/sources')).json()).toEqual(sources);
  expect(await (await page.request.get('/api/changesets')).json()).toEqual(changes);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: info.outputPath('deletion-mobile.png'), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  );
  expect(errors).toEqual([]);
});

test('unfinished task blocks deletion without cancelling it', async ({ page }) => {
  const chat = await (
    await page.request.post('/api/conversations', { data: { title: '待执行保护' } })
  ).json();
  const run = await (
    await page.request.post(`/api/conversations/${chat.id}/messages`, {
      data: { content: '等待执行的任务', request_key: crypto.randomUUID() },
    })
  ).json();
  await page.goto(`/chat/${chat.id}`);
  await page.getByRole('button', { name: '删除会话', exact: true }).click();
  await expect(page.getByText('当前不能删除', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '确认删除会话' })).toBeDisabled();
  expect((await (await page.request.get(`/api/runs/${run.id}`)).json()).status).toBe('queued');
});
