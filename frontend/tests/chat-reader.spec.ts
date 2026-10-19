import { test, expect } from '@playwright/test';

test('chat keeps a compact composer visible while reading older messages', async ({ page }) => {
  const chats = await (await page.request.get('/api/conversations')).json();
  const id = chats[0].id;
  await page.route(`**/api/conversations/${id}/context-usage`, (route) => route.fulfill({ json: {
    percentage: 13.9, input_tokens: 36438, working_window: 262144, model_window: 1048576,
    estimated: false, output_reserve: 8192, framing_reserve: 4096, summary_trigger_tokens: 235929,
  } }));
  await page.route(`**/api/conversations/${id}/messages`, (route) => route.fulfill({ json:
    Array.from({ length: 60 }, (_, index) => ({ id: `scroll-${index}`, run_id: 'history',
      role: index % 2 ? 'assistant' : 'user', content: `第 ${index + 1} 条历史消息。\n\n${'需要完整保留的讨论内容。'.repeat(15)}`,
      created_at: '2026-10-17T08:00:00Z' })),
  }));
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(`/chat/${id}`);
  await expect(page.getByText('第 1 条历史消息。', { exact: false })).toBeAttached();
  const composer = page.locator('.composer');
  const box = (await composer.boundingBox())!;
  expect(box.y + box.height).toBeLessThanOrEqual(900);
  expect(box.y).toBeGreaterThan(400);
  await expect(page.getByRole('checkbox', { name: '允许辅助模型' })).not.toBeVisible();
  await page.getByRole('button', { name: '更多设置与材料' }).click();
  await expect(page.getByRole('checkbox', { name: '允许辅助模型' })).toBeVisible();
  await page.keyboard.press('Escape');
  await page.locator('.chat-scroll').evaluate((element) => { element.scrollTop = 0; });
  await expect(page.getByRole('button', { name: '回到最新消息' })).toBeVisible();
  await page.getByRole('button', { name: '回到最新消息' }).click();
  await expect(page.getByRole('button', { name: '回到最新消息' })).not.toBeVisible();
  const controls = [composer.locator('.context-usage summary'), composer.locator('.composer-model'),
    composer.getByRole('combobox', { name: '思考强度' }), composer.getByRole('button', { name: '发送', exact: true })];
  const positions = await Promise.all(controls.map((control) => control.boundingBox()));
  for (let index = 1; index < positions.length; index++) {
    expect(positions[index]!.x).toBeGreaterThan(positions[index - 1]!.x);
    expect(Math.abs(positions[index]!.y + positions[index]!.height / 2 - positions[0]!.y - positions[0]!.height / 2)).toBeLessThan(2);
  }
  await expect(composer.locator('.context-ring-value')).toHaveAttribute('stroke-dasharray', '13.9 100');
  await page.screenshot({ path: '../artifacts/verification/composer-ring-desktop.png' });
  await composer.locator('.context-usage summary').click();
  await expect(composer.getByRole('button', { name: '立即压缩上下文' })).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  const mobile = (await composer.boundingBox())!;
  expect(mobile.y + mobile.height).toBeLessThanOrEqual(844);
  const usage = (await composer.locator('.context-usage-details').boundingBox())!;
  expect(usage.x).toBeGreaterThanOrEqual(0);
  expect(usage.x + usage.width).toBeLessThanOrEqual(390);
  await page.screenshot({ path: '../artifacts/verification/composer-ring-mobile.png' });
});

test('source documents open in full and removal preserves pinned versions', async ({ page }) => {
  const body = '# 阅读器完整标题\n\n' + '原文中间部分。\n\n'.repeat(500) + '最后一段仍可阅读。';
  const response = await page.request.post('/api/sources/text', { data: { title: '全文阅读与删除验收', content: body } });
  expect(response.status()).toBe(201);
  const source = await response.json();
  // 明确的存活引用才保护旧版本；打开过一次阅读器本身不构成永久保留。
  await page.request.post('/api/sources/text', { data: { title: '完整阅读引用者', content: `[固定正文](/sources/${source.id}?version=${source.version_id})` } });
  await page.request.post('/api/__offline/assets-ready');
  await page.goto('/sources');
  const website = page.getByRole('link', { name: '网页阅读示例 ↗' });
  await expect(website).toHaveAttribute('href', 'https://example.org/reader');
  await expect(website).toHaveAttribute('target', '_blank');
  await page.getByRole('link', { name: '全文阅读与删除验收', exact: true }).click();
  await expect(page.getByRole('heading', { name: '阅读器完整标题' })).toBeVisible();
  await expect(page.getByText('最后一段仍可阅读。', { exact: true })).toBeAttached();
  await page.screenshot({ path: '../artifacts/verification/source-full-reader.png' });
  await page.goto('/sources');
  await page.getByRole('button', { name: '删除材料 全文阅读与删除验收' }).click();
  const deletion = page.waitForResponse((res) => res.request().method() === 'POST' && res.url().endsWith(`/source/${source.id}/deletion`));
  await page.getByRole('button', { name: '确认删除材料' }).click();
  const job = await (await deletion).json();
  expect((await (await page.request.post(`/api/__offline/asset-cleanup/${job.id}`)).json()).status).toBe('completed');
  await expect(page.getByRole('link', { name: '全文阅读与删除验收', exact: true })).toHaveCount(0);
  await page.goto(`/sources/${source.id}?version=${source.version_id}`);
  await expect(page.getByText('最后一段仍可阅读。', { exact: true })).toBeAttached();
  await expect(page.getByText(/此材料已从材料库移除/)).toBeVisible();
});

test('workspace viewer renders Markdown and locates source code lines', async ({ page }) => {
  await page.goto('/files?path=outputs%2Freader-note.md');
  await expect(page.getByRole('heading', { name: '完整文档预览' })).toBeVisible();
  await expect(page.getByRole('link', { name: '下载文件' })).toHaveAttribute('href', '/api/workspace/download?path=outputs%2Freader-note.md');
  await page.goto('/files?path=outputs%2Freader-example.py#L3');
  await expect(page.locator('.focused-line')).toContainText('return value');
  await expect(page.locator('[data-line="1"]')).toContainText('def example():');
  await page.screenshot({ path: '../artifacts/verification/code-full-reader.png' });
});
