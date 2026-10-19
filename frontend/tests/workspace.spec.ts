import { test, expect } from '@playwright/test';

test('context usage comes from the conversation request and remains readable on narrow screens', async ({
  page,
}) => {
  const runs = await (await page.request.get('/api/runs')).json();
  const run = runs.find((item: { task: string }) => item.task === '整理共享预算规则，先预览');
  await page.goto(`/chat/${run.conversation_id}`);
  await expect(page.locator('summary[aria-label="上下文 50%"]')).toBeVisible();
  await page.locator('summary[aria-label="上下文 50%"]').click();
  await expect(page.getByText(/最近一次模型输入：131,072 \/ 262,144/)).toBeVisible();
  await expect(page.getByText(/235,929 tokens 时尝试压缩/)).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  const box = await page.locator('.context-usage-details').boundingBox();
  expect(box).not.toBeNull();
  expect(box!.x).toBeGreaterThanOrEqual(0);
  expect(box!.x + box!.width).toBeLessThanOrEqual(390);
  await page.screenshot({
    path: '../artifacts/verification/optimization/context-usage-mobile.png',
  });
  const fresh = await (
    await page.request.post('/api/conversations', { data: { title: '独立上下文占比' } })
  ).json();
  await page.goto(`/chat/${fresh.id}`);
  await expect(page.locator('summary[aria-label="上下文 —"]')).toBeVisible();
  await page.locator('summary[aria-label="上下文 —"]').click();
  await expect(page.getByText('尚无模型输入记录。', { exact: true })).toBeVisible();
  await expect(page.getByText('上下文 50%', { exact: true })).toHaveCount(0);
});

test('preferences persist edits, revocation and revision history through reload', async ({
  page,
}) => {
  await page.goto('/preferences');
  await page.getByLabel('类别（可选）').selectOption('memory-category-preference');
  await page.getByLabel('新增长期记忆').fill('浏览器检查：解释新概念时先给一个具体例子。');
  const created = page.waitForResponse(
    (r) => r.url().endsWith('/api/memories') && r.request().method() === 'POST',
  );
  await page.getByRole('button', { name: '新增记忆', exact: true }).click();
  const memory = await (await created).json();
  const field = page.getByLabel(`记忆内容 ${memory.id}`, { exact: true });
  const card = page.locator('section.card').filter({ has: field });
  await expect(card.getByText('表达偏好 · 已生效', { exact: true })).toBeVisible();
  await field.fill('解释新概念时先给例子，再讨论边界。');
  await card.getByRole('button', { name: '保存修改', exact: true }).click();
  await expect(card.getByText('修订 2', { exact: true })).toBeVisible();
  await page.reload();
  await expect(field).toHaveValue('解释新概念时先给例子，再讨论边界。');
  await card.getByRole('button', { name: '停用', exact: true }).click();
  await expect(card.getByText('表达偏好 · 已停用', { exact: true })).toBeVisible();
  await card.getByRole('button', { name: '删除记忆', exact: true }).click();
  await expect(field).toHaveCount(0);
  await page.getByRole('checkbox', { name: '显示已删除记录' }).check();
  await expect(card.getByText('表达偏好 · 已删除', { exact: true })).toBeVisible();
  await expect(field).toBeDisabled();
});

test('new MCP discovery does not grant automatic permission', async ({
  page,
}) => {
  test.setTimeout(60000);
  await page.goto('/mcp');
  await page.getByLabel('服务名称', { exact: true }).fill('浏览器协议夹具');
  await page.getByLabel('非秘密配置 JSON', { exact: true }).fill(
    JSON.stringify({
      transport: 'stdio',
      command: '/workspace/.venv/bin/python',
      args: ['/workspace/tests/fixtures/mcp_probe.py'],
    }),
  );
  await page.getByRole('button', { name: '添加连接', exact: true }).click();
  await expect(page.getByRole('heading', { name: '浏览器协议夹具' })).toBeVisible();
  // 其他用例可以已经登记自己的连接；只管理本例新建的那一份。
  const card = page.locator('section.card').filter({
    has: page.getByRole('heading', { name: '浏览器协议夹具', exact: true }),
  });
  await card.getByRole('button', { name: '测试连接并发现工具' }).click();
  await expect(card.getByLabel('调用许可')).toHaveValue('deny', { timeout: 45000 });
  await card.getByLabel('调用许可').selectOption('read');
  await card.getByRole('button', { name: '保存工具许可' }).click();
  await expect(card.getByText(/修订 3/)).toBeVisible();
  await card.getByRole('button', { name: '启用连接', exact: true }).click();
  await expect(card.getByRole('button', { name: '停用连接', exact: true })).toBeVisible();
  await page.reload();
  await expect(card.getByLabel('调用许可')).toHaveValue('read');
  const exported = await page.request.get(
    (await card.getByRole('link', { name: '导出非秘密配置' }).getAttribute('href'))!,
  );
  const body = await exported.json();
  expect(body.config.command).toBe('/workspace/.venv/bin/python');
  expect(body.permissions).toBeUndefined();
  await card.getByRole('button', { name: '停用连接', exact: true }).click();
  await expect(card.getByRole('button', { name: '启用连接', exact: true })).toBeVisible();
});

test('long run history can reach the final event and export every page', async ({ page }) => {
  const runs = await (await page.request.get('/api/runs')).json();
  const run = runs.find((item: { task: string }) => item.task === '整理共享预算规则，先预览');
  await page.goto(`/runs/${run.id}`);
  await page.getByRole('button', { name: '诊断', exact: true }).click();
  await expect(page.getByText('事件 1–200 / 1004')).toBeVisible();
  for (let after = 200; after <= 1000; after += 200) {
    await page.getByRole('button', { name: '下一页事件' }).click();
    await expect(
      page.getByText(`事件 ${after + 1}–${Math.min(after + 200, 1004)} / 1004`),
    ).toBeVisible();
  }
  await expect(page.getByRole('button', { name: '下一页事件' })).toBeDisabled();
  await expect(page.getByText('#1004 · finished')).toBeVisible();
  await page.getByRole('button', { name: '上一页事件' }).click();
  await expect(page.getByText('事件 801–1000 / 1004')).toBeVisible();
  const exported = await (await page.request.get(`/api/runs/${run.id}/export`)).json();
  expect(exported.events).toHaveLength(1004);
  expect(exported.events.at(-1).kind).toBe('finished');
});

test('development reports and cumulative usage are absent from the product', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('link', { name: '评估报告', exact: true })).toHaveCount(0);
  await expect(page.getByText(/累计模型请求|无累计上限/)).toHaveCount(0);
  const settings = await (await page.request.get('/api/settings')).json();
  expect(settings).not.toHaveProperty('usage');
  expect((await page.request.get('/api/evaluations')).status()).toBe(404);
  await page.goto('/evaluations');
  await expect(page.getByText('页面不存在。', { exact: false })).toBeVisible();
});

test('PDF upload becomes a durable material job and invalid web URLs retain feedback', async ({
  page,
}) => {
  await page.goto('about:blank');
  await page.setContent('<p>Browser fixture: persistent materials, budget 80.</p>');
  const pdf = await page.pdf({ format: 'A4' });
  await page.goto('/sources');
  await page
    .getByLabel('上传材料文件')
    .setInputFiles({ name: 'browser.pdf', mimeType: 'application/pdf', buffer: pdf });
  await expect(page).toHaveURL(/\/runs\/[a-f0-9-]+$/);
  await expect(page.getByText('排队中', { exact: true })).toBeVisible();
  await expect(page.getByText('材料下载与解析不调用大模型', { exact: false })).toBeVisible();
  await page.reload();
  await page.getByRole('button', { name: '停止本次任务' }).click();
  await expect(page.getByText('已取消', { exact: true })).toBeVisible();
  await expect(page.getByText('模型请求总上限', { exact: true })).toHaveCount(0);
  await page.goto('/sources');
  await page.getByLabel('网页地址').fill('file:///etc/passwd');
  await page.getByRole('button', { name: '开始导入网页' }).click();
  await expect(page.getByRole('alert')).toContainText('public HTTP');
  await expect(page.getByLabel('网页地址')).toHaveValue('file:///etc/passwd');
});

test('import material, preview its text, and retain validation errors', async ({ page }) => {
  await page.goto('/sources');
  await page.getByLabel('材料标题').fill('浏览器导入材料');
  await page.getByLabel('材料内容').fill('这份材料来自浏览器端导入，可定位到实际片段。');
  await page.getByRole('button', { name: '保存材料' }).click();
  await expect(page).toHaveURL(/\/sources\/[a-f0-9-]+$/);
  await expect(page.locator('.document-reader')).toContainText('可定位到实际片段');
  await page.reload();
  await expect(page.getByRole('heading', { name: '浏览器导入材料' })).toBeVisible();
  await page.goto('/sources');
  await page
    .getByLabel('上传材料文件')
    .setInputFiles({ name: 'bad.txt', mimeType: 'text/plain', buffer: Buffer.from([255]) });
  await expect(page.getByRole('alert')).toContainText('UTF-8');
});

test('review, publish, read evidence, edit, and reopen a historical version', async ({ page }) => {
  await page.goto('/changes');
  await page.getByRole('link', { name: /Agent 共享预算规则/ }).click();
  await expect(
    page.locator('.markdown').getByText('主任务和子任务不能分别领取整套额度。', { exact: false }),
  ).toBeVisible();
  await page.getByRole('button', { name: '确认并发布' }).click();
  await expect(page.getByText('已发布', { exact: true })).toBeVisible();
  await page.goto('/knowledge');
  await page.getByRole('link', { name: /Agent 共享预算规则/ }).click();
  const articleUrl = page.url();
  await page.getByRole('link', { name: '预算规则原始材料' }).click();
  await expect(page).toHaveURL(/\/sources\/[^?]+\?version=/);
  await expect(page.locator('.document-reader')).toContainText('重试也计入');
  await page.goto(articleUrl);
  await page.getByRole('button', { name: '编辑', exact: true }).click();
  const editor = page.getByLabel('Markdown 正文');
  await editor.fill((await editor.inputValue()) + '\n\n补充：预算由程序强制检查。');
  await page.getByRole('button', { name: '保存新版本' }).click();
  await expect(page.getByText('补充：预算由程序强制检查。', { exact: true })).toBeVisible();
  await page.getByLabel('文章历史版本').selectOption({ label: 'v1' });
  await expect(page.getByText('正在查看历史版本。', { exact: false })).toBeVisible();
  await expect(page.getByText('补充：预算由程序强制检查。', { exact: true })).toHaveCount(0);
  await page.screenshot({
    path: '../artifacts/verification/browser-knowledge.png',
    fullPage: true,
  });
  await page.getByRole('button', { name: '恢复为此版本' }).click();
  await expect(page).toHaveURL(/\/changes\/[a-f0-9-]+$/);
  await expect(page.locator('.markdown')).not.toContainText('补充：预算由程序强制检查。');
  await page.getByRole('button', { name: '确认并发布' }).click();
  await expect(page.getByText('已发布', { exact: true })).toBeVisible();
  await page.goto(articleUrl);
  await expect(page.getByLabel('文章历史版本').locator('option:checked')).toHaveText('v3');
  await page.getByRole('button', { name: '归档文章' }).click();
  await page.getByRole('button', { name: '确认并发布' }).click();
  await expect(page.getByText('已发布', { exact: true })).toBeVisible();
  await page.goto('/knowledge');
  await expect(page.getByRole('link', { name: /Agent 共享预算规则/ })).toHaveCount(0);
  await page.getByRole('checkbox', { name: '含归档' }).check();
  await page.getByRole('link', { name: /Agent 共享预算规则/ }).click();
  await page.getByRole('button', { name: '恢复为此版本' }).click();
  await page.getByRole('button', { name: '确认并发布' }).click();
  await expect(page.getByText('已发布', { exact: true })).toBeVisible();
  await page.goto(articleUrl);
  await expect(page.getByLabel('文章历史版本').locator('option:checked')).toHaveText('v5');
});

test('queued chat survives refresh and can be cancelled and explicitly resumed', async ({
  page,
}) => {
  await page.goto('/');
  await page.getByLabel('发送给 KnowledgeOps 的消息').fill('请梳理知识库已有主题');
  await page.getByRole('button', { name: '发送', exact: true }).click();
  await expect(page).toHaveURL(/\/chat\//);
  await expect(page.locator('.message.user')).toContainText('请梳理知识库已有主题');
  await page.reload();
  await expect(page.locator('.message.user')).toHaveCount(1);
  await page.getByRole('button', { name: '取消排队', exact: true }).click();
  await expect(page.getByText('已取消', { exact: true })).toBeVisible();
  await page.getByRole('link', { name: '任务详情 →' }).click();
  await page.getByRole('button', { name: '确认恢复任务' }).click();
  await expect(page.getByText('排队中', { exact: true })).toBeVisible();
  await expect(page.getByText('确认恢复任务', { exact: true })).toHaveCount(0);
});

test('preserved context is visible and can be paged without losing the run link', async ({
  page,
}) => {
  // 本例自己建立跨页条件，不能假定其他场景结束后种子仍位于最新20条里。
  const padding = await (
    await page.request.post('/api/conversations', {
      data: { title: '运行列表分页夹具' },
    })
  ).json();
  for (let index = 0; index < 21; index++) {
    const response = await page.request.post(`/api/conversations/${padding.id}/messages`, {
      data: { content: `分页填充 ${index}`, request_key: crypto.randomUUID() },
    });
    expect(response.ok()).toBe(true);
  }
  await page.goto('/runs');
  const target = page.getByRole('link', { name: /整理共享预算规则/ });
  const more = page.getByRole('button', { name: '加载更多运行', exact: true });
  await expect(more).toBeVisible();
  await expect(target).toHaveCount(0);
  for (let index = 0; index < 5 && !(await target.count()); index++) {
    const before = await page.locator('a.list-card').count();
    await more.click();
    await expect.poll(() => page.locator('a.list-card').count()).toBeGreaterThan(before);
  }
  await target.click();
  await page.getByRole('button', { name: '诊断', exact: true }).click();
  await page.getByRole('link', { name: /context_original/ }).click();
  await expect(page.locator('.source-text')).toContainText('分页原文证据');
  await page.getByRole('button', { name: '下一段' }).click();
  await expect(page.getByText(/字符 6000/)).toBeVisible();
  await expect(page.getByRole('button', { name: '下一段' })).toBeDisabled();
  await page.getByRole('link', { name: '返回运行详情', exact: false }).click();
  await expect(page.getByRole('heading', { name: '任务详情', exact: true })).toBeVisible();
});

test('empty search, failed request, and narrow viewport have usable states', async ({ page }) => {
  await page.goto('/knowledge');
  // 使用单个不在材料中的标识符，避免中文 OR 词法检索命中“的”等共有词。
  await page.getByLabel('检索知识库').fill('ZXQJ92746NONEXISTENT');
  await page.getByRole('button', { name: '检索', exact: true }).click();
  await expect(page.getByRole('heading', { name: '没有找到匹配的知识' })).toBeVisible();
  await page.route('**/api/knowledge?**', (route) => route.abort());
  await page.getByLabel('检索知识库').fill('模拟网络不可用');
  await page.getByRole('button', { name: '检索', exact: true }).click();
  await expect(page.getByRole('alert')).toBeVisible();
  await expect(page.getByRole('heading', { name: '没有找到匹配的知识' })).toHaveCount(0);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  await expect(page.getByRole('button', { name: '发送', exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.screenshot({ path: '../artifacts/verification/browser-mobile.png', fullPage: true });
});
