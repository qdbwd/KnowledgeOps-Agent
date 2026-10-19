import { expect, test } from '@playwright/test';

test('会话删除后旧内部链接打开完整固定正文，并可明确保存到材料库', async ({ page, request }) => {
  test.setTimeout(120000);
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  const seeded = await (await request.post('/api/__offline/material-alias-seed')).json();
  await page.goto(`/chat/${seeded.conversation_id}/deletion`);
  await page.getByRole('button', { name: '确认删除会话' }).click();
  await expect(page.getByRole('heading', { name: '正在清理会话' })).toBeVisible();
  expect(await (await request.post(`/api/__offline/lifecycle/${seeded.conversation_id}`)).json())
    .toMatchObject({ status: 'completed' });
  await page.goto(seeded.url);
  await expect(page).toHaveURL(/\/sources\/[^?]+\?version=/);
  await expect(page.getByRole('heading', { name: '引用文本 · 研究输出全文' })).toBeVisible();
  await expect(page.getByText('完整引用正文', { exact: true })).toBeVisible();
  await expect(page.getByText('全文最后一行也能核对。', { exact: true })).toBeAttached();
  // 阅读器将正文按行展示，行号是独立链接，不能把它们误计入原文。
  const lines = await page.locator('.document-code [data-line] > span').allTextContents();
  expect(lines.join('\n')).toBe('完整引用正文\n' + '中间内容保持原样。\n'.repeat(800) + '全文最后一行也能核对。');
  expect(lines.join('\n').length).toBe(seeded.length);
  await expect(page.getByRole('button', { name: '下一段' })).toHaveCount(0);
  await page.screenshot({ path: '../artifacts/verification/increment-material-alias-desktop.png' });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: '../artifacts/verification/increment-material-alias-mobile.png' });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.getByRole('button', { name: '保存到材料库', exact: true }).click();
  await expect(page.getByText('已独立保存在材料库。')).toBeVisible();
  await page.goto('/sources');
  await expect(page.getByRole('link', { name: '引用文本 · 研究输出全文', exact: true })).toBeVisible();
  expect(errors).toEqual([]);
});
