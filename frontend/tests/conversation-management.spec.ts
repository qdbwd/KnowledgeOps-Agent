import { expect, test } from '@playwright/test';

test('materials stay out of recent chats until a message, and right-click rename persists', async ({
  page,
  request,
}, info) => {
  await page.goto('/');
  const message = page.getByRole('textbox', { name: '发送给 KnowledgeOps 的消息' });
  const title = `新的实际聊天-${Date.now()}`;
  await message.fill(title);
  await page.getByRole('button', { name: '更多设置与材料', exact: true }).click();
  await page.getByRole('button', { name: '导入会话材料 →' }).click();
  await expect(page).toHaveURL(/\/sources\?conversation_id=/);
  const id = new URL(page.url()).searchParams.get('conversation_id')!;
  expect(
    (await (await request.get('/api/conversations')).json()).some(
      (row: { id: string }) => row.id === id,
    ),
  ).toBe(false);
  await page.getByLabel('材料标题', { exact: true }).fill('尚未聊天的材料');
  await page.getByLabel('材料内容', { exact: true }).fill('浏览器验证的文本，不调用模型。');
  await page.getByRole('button', { name: '保存材料', exact: true }).click();
  await expect(page).toHaveURL(/\/sources\/.+\?conversation_id=/);
  expect(
    (await (await request.get('/api/conversations')).json()).some(
      (row: { id: string }) => row.id === id,
    ),
  ).toBe(false);
  await page.goto(`/chat/${id}`);
  await expect(message).toHaveValue(title);
  await page.getByRole('button', { name: '发送', exact: true }).click();
  const recent = page.locator('.recent');
  await expect(recent.getByRole('link', { name: title, exact: true })).toBeVisible();
  await recent.getByRole('link', { name: title, exact: true }).click({ button: 'right' });
  await expect(page.getByRole('menuitem')).toHaveCount(1);
  await page.getByRole('menuitem', { name: '重命名' }).click();
  await page.getByLabel('会话名称').fill('');
  await expect(page.getByRole('button', { name: '保存名称' })).toBeDisabled();
  await page.getByLabel('会话名称').fill('手动命名的会话');
  await page.getByRole('button', { name: '保存名称' }).click();
  await expect(recent.getByRole('link', { name: '手动命名的会话', exact: true })).toBeVisible();
  await page.reload();
  await expect(recent.getByRole('link', { name: '手动命名的会话', exact: true })).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.locator('.mobile-conversations summary').click();
  await page
    .locator('.mobile-conversations')
    .getByRole('button', { name: '会话“手动命名的会话”的选项' })
    .click();
  await expect(page.getByRole('menuitem', { name: '重命名' })).toBeVisible();
  await page.screenshot({ path: info.outputPath('rename-menu-mobile.png'), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test('knowledge owns review navigation and old review links still work', async ({
  page,
  request,
}, info) => {
  const seeded = await (await request.post('/api/__offline/task-details-seed')).json();
  const summary = await (await request.get(`/api/runs/${seeded.run_id}/summary`)).json();
  const draft = summary.drafts[0];
  await page.goto('/knowledge');
  await page.getByRole('link', { name: '草稿与审阅', exact: true }).click();
  await expect(page).toHaveURL('/knowledge/reviews');
  await expect(page.getByRole('combobox', { name: '审阅状态' })).toHaveValue('pending');
  await page.getByRole('link').filter({ hasText: draft.title }).first().click();
  await expect(page).toHaveURL(`/knowledge/reviews/${draft.id}`);
  await expect(page.getByRole('heading', { name: '审阅知识变更' })).toBeVisible();
  await page.screenshot({ path: info.outputPath('knowledge-review.png'), fullPage: true });
  await page.goto(`/changes/${draft.id}`);
  await expect(page).toHaveURL(`/knowledge/reviews/${draft.id}`);
  await expect(
    page.getByRole('navigation', { name: '主要功能' }).getByRole('link', { name: '运行记录' }),
  ).toBeVisible();
  await expect(
    page.getByRole('navigation', { name: '主要功能' }).getByRole('link', { name: '变更与运行' }),
  ).toHaveCount(0);
});
