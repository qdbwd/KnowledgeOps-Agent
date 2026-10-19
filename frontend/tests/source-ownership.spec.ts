import { expect, test } from '@playwright/test';

test('新聊天导入先建立会话归属，返回保留未发送输入', async ({ page, request }) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto('/');
  await page.getByPlaceholder('提出问题，或说明希望如何整理知识…').fill('这段文字尚未发送');
  await page.getByRole('button', { name: '导入学习材料' }).click();
  await expect(page.getByRole('heading', { name: '会话材料', exact: true })).toBeVisible();
  const id = new URL(page.url()).searchParams.get('conversation_id')!;
  expect(id).toBeTruthy();
  expect(await (await request.get(`/api/conversations/${id}/messages`)).json()).toEqual([]);
  await page.getByLabel('材料标题', { exact: true }).fill('导入前尚未发送');
  await page.getByLabel('材料内容', { exact: true }).fill('# 新会话附件\n\n仅属于本会话。');
  await page.getByRole('button', { name: '保存材料', exact: true }).click();
  await expect(page.getByRole('heading', { name: '新会话附件', exact: true })).toBeVisible();
  await page.getByRole('link', { name: '本会话材料', exact: true }).click();
  await page.getByRole('link', { name: '返回聊天', exact: true }).click();
  await expect(page.getByPlaceholder('提出问题，或说明希望如何整理知识…')).toHaveValue('这段文字尚未发送');
  await page.getByRole('button', { name: '更多设置与材料', exact: true }).click();
  await page.getByRole('button', { name: '导入或管理会话材料 →' }).click();
  expect(new URL(page.url()).searchParams.get('conversation_id')).toBe(id);
  expect(errors).toEqual([]);
});

test('会话材料独立保存后跨会话可用，原正文身份保持', async ({ page, request }) => {
  const conversation = await (await request.post('/api/conversations', { data: { title: '归属验收' } })).json();
  const source = await (await request.post('/api/sources/text', { data: {
    title: '本次讨论附件', content: '# 会话正文\n\n这份内容可以独立保留。', conversation_id: conversation.id,
  } })).json();
  await page.goto('/sources');
  await expect(page.getByRole('heading', { name: '材料与来源', exact: true })).toBeVisible();
  await expect(page.getByText('本次讨论附件', { exact: true })).toHaveCount(0);
  await page.goto(`/chat/${conversation.id}`);
  await page.getByRole('button', { name: '更多设置与材料', exact: true }).click();
  await page.getByRole('button', { name: '导入或管理会话材料 →' }).click();
  await expect(page.getByRole('heading', { name: '会话材料', exact: true })).toBeVisible();
  await expect(page.getByRole('link', { name: '本次讨论附件', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '保存到材料库', exact: true }).click();
  await expect(page.getByText('已独立保留', { exact: true })).toBeVisible();
  await page.getByRole('link', { name: '查看材料库', exact: true }).click();
  await page.getByRole('link', { name: '本次讨论附件', exact: true }).click();
  await expect(page.getByRole('heading', { name: '会话正文', exact: true })).toBeVisible();
  expect((await (await request.get(`/api/sources/${source.id}`)).json()).version_id).toBe(source.version_id);
});
