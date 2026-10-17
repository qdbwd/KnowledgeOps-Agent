import { expect, test } from '@playwright/test';

test('file uncertainty keeps current content and resumes only after a persisted user decision', async ({ page }, testInfo) => {
  test.setTimeout(120000);
  const conv = await (await page.request.post('/api/conversations', { data: { title: '文件结果核对' } })).json();
  const run = await (await page.request.post(`/api/conversations/${conv.id}/messages`, {
    data: { content: '编辑工作区说明文件', request_key: crypto.randomUUID() },
  })).json();
  const advance = async () => (await page.request.post(`/api/__offline/file/${run.id}`)).json();
  expect((await advance()).outcome.kind).toBe('execution_unknown');
  await page.goto(`/chat/${conv.id}`);
  await expect(page.getByText('文件操作待核对 · 结果未知', { exact: true })).toBeVisible();
  await page.getByText('查看当前文件片段', { exact: true }).click();
  await expect(page.getByText('old', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '重新核对（不改文件）', exact: true })).toBeDisabled();
  await page.screenshot({ path: testInfo.outputPath('file-review-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.reload();
  await expect(page.getByText('文件操作待核对 · 结果未知', { exact: true })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('file-review-mobile.png'), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.getByRole('button', { name: '保留现状，跳过并继续', exact: true }).click();
  await expect.poll(async () => (await (await page.request.get(`/api/runs/${run.id}`)).json()).status).toBe('queued');
  expect((await advance()).status).toBe('completed');
  await expect(page.getByText('已收到文件操作的实际回执，未确认成功的操作没有重试。', { exact: true })).toBeVisible();
  const records = (await (await page.request.get(`/api/runs/${run.id}/approvals`)).json()).files;
  expect(records).toEqual([]);
});

test('MCP forms and URL continuation survive pause and refresh without treating acknowledgment as service completion', async ({ page }, testInfo) => {
  test.setTimeout(120000);
  const originalConnections = await (await page.request.get('/api/mcp/connections')).json();
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  const conv = await (await page.request.post('/api/conversations', { data: { title: 'MCP 补问闭环' } })).json();
  const run = await (await page.request.post(`/api/conversations/${conv.id}/messages`, {
    data: { content: '请从服务核对资料', request_key: crypto.randomUUID() },
  })).json();
  const advance = async () => (await page.request.post(`/api/__offline/mcp-input/${run.id}`)).json();
  expect((await advance()).status).toBe('waiting_input');
  await page.goto(`/chat/${conv.id}`);
  await page.getByRole('spinbutton', { name: '条数（必填）' }).fill('2');
  await page.getByRole('button', { name: '暂停任务', exact: true }).click();
  await expect(page.getByText('回答会保存，任务保持暂停，仍需明确继续。', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '提交并继续', exact: true }).click();
  await expect(page.getByText('补充信息已保存；是否完成由服务返回结果确认。', { exact: true })).toBeVisible();
  await page.reload();
  expect((await (await page.request.get(`/api/runs/${run.id}`)).json()).status).toBe('paused');
  await page.getByRole('button', { name: '继续任务', exact: true }).click();
  await expect.poll(async () => (await (await page.request.get(`/api/runs/${run.id}`)).json()).status).toBe('queued');
  expect((await advance()).status).toBe('waiting_input');
  await page.reload();
  await expect(page.getByRole('link', { name: '打开服务页面完成操作 ↗' })).toHaveAttribute('href', 'https://example.com/verify');
  await page.screenshot({ path: testInfo.outputPath('mcp-input-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: testInfo.outputPath('mcp-input-mobile.png'), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.getByRole('checkbox', { name: '我已在服务页面操作，继续核对' }).check();
  await page.getByRole('button', { name: '提交并继续', exact: true }).click();
  await expect.poll(async () => (await (await page.request.get(`/api/runs/${run.id}`)).json()).status).toBe('queued');
  const finished = await advance();
  expect(finished.status).toBe('completed');
  // 新资源先搜索冻结的 MCP 目录，再执行服务；协议续接不额外计工具次数。
  expect(finished.tool_calls).toBe(2);
  // 测试未打开真实服务页面，服务返回的业务核对仍为 false，不能仅凭勾选宣称已确认。
  const calls = await (await page.request.get(`/api/runs/${run.id}/mcp`)).json();
  expect(calls[0].result.preview).toContain('"confirmed_by_server": false');
  await page.reload();
  await page.locator('.context-usage summary').click();
  await expect(page.getByText('必需任务背景', { exact: true })).toBeVisible();
  await expect(page.getByText('工具定义', { exact: true })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('context-budget-mobile.png'), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.screenshot({ path: testInfo.outputPath('context-budget-desktop.png'), fullPage: true });
  expect(errors).toEqual([]);
  // 此协议夹具只属于本场景，撤销它，避免后续空目录用例继承临时连接。
  const connections = await (await page.request.get('/api/mcp/connections')).json();
  for (const connection of connections) {
    if (!originalConnections.some((item: { id: string }) => item.id === connection.id)) {
      expect((await page.request.patch(`/api/mcp/connections/${connection.id}`, {
        data: { expected_revision: connection.revision, deleted: true },
      })).ok()).toBeTruthy();
    }
  }
});

test('async children can be guided and cancelled and results resume the parent without reload', async ({ page }, testInfo) => {
  test.setTimeout(120000);
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  const conv = await (await page.request.post('/api/conversations', { data: { title: '异步协作闭环' } })).json();
  const run = await (await page.request.post(`/api/conversations/${conv.id}/messages`, {
    data: { content: '并行核对两份资料并整合', request_key: crypto.randomUUID() },
  })).json();
  expect((await (await page.request.post(`/api/__offline/async/${run.id}`)).json()).status).toBe('waiting_children');
  const children = await (await page.request.get(`/api/runs/${run.id}/children`)).json();
  await page.goto(`/chat/${conv.id}`);
  await expect(page.getByRole('heading', { name: '子任务协作' })).toBeVisible();
  const first = page.locator('.child-run').filter({ hasText: children[0].task });
  await first.getByRole('textbox', { name: '给这个子任务补充要求' }).fill('明确说明知识的适用条件');
  await first.getByRole('button', { name: '发送补充' }).click();
  await page.reload();
  await first.getByText('已发送的补充要求', { exact: true }).click();
  await expect(first.getByText('明确说明知识的适用条件', { exact: true })).toBeVisible();
  await expect(first.getByText('你 · 等待安全点应用', { exact: true })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('async-children-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: testInfo.outputPath('async-children-mobile.png'), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  const second = page.locator('.child-run').filter({ hasText: children[1].task });
  await second.getByRole('button', { name: '取消这个子任务' }).click();
  await expect(second.getByText('已取消', { exact: true })).toBeVisible();
  expect((await (await page.request.get(`/api/runs/${children[0].task_id}`)).json()).status).toBe('queued');
  expect((await (await page.request.post(`/api/__offline/async/${children[0].task_id}`)).json()).status).toBe('completed');
  expect((await (await page.request.get(`/api/runs/${run.id}`)).json()).status).toBe('queued');
  expect((await (await page.request.post(`/api/__offline/async/${run.id}`)).json()).status).toBe('completed');
  // 不刷新页面，SSE 或队列状态刷新必须让主结果和子状态出现。
  await expect(page.getByText('已整合两份子任务结果，并保留各自适用条件。', { exact: true })).toBeVisible();
  await expect(first.getByText('已结束', { exact: true })).toBeVisible();
  expect(errors).toEqual([]);
});

test('question and paused response survive page refresh and continue the original run', async ({ page }, testInfo) => {
  test.setTimeout(120000);
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  const conv = await (await page.request.post('/api/conversations', { data: { title: '补问与暂停闭环' } })).json();
  const run = await (await page.request.post(`/api/conversations/${conv.id}/messages`, {
    data: { content: '整理一份说明，先问清楚读者', request_key: crypto.randomUUID() },
  })).json();
  const waiting = await (await page.request.post(`/api/__offline/question/${run.id}`)).json();
  expect(waiting.status).toBe('waiting_input');
  await page.goto(`/chat/${conv.id}`);
  await expect(page.getByText('说明面向初学者还是有经验的读者？', { exact: true })).toBeVisible();
  await page.reload();
  await page.getByRole('button', { name: '暂停任务', exact: true }).click();
  await expect(page.getByRole('button', { name: '继续任务', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '初学者', exact: true }).click();
  await page.getByRole('button', { name: '提交回答', exact: true }).click();
  await expect(page.getByText('你的回答：初学者', { exact: true })).toBeVisible();
  expect((await (await page.request.get(`/api/runs/${run.id}`)).json()).status).toBe('paused');
  await page.reload();
  await expect(page.getByText('你的回答：初学者', { exact: true })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('question-paused-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: testInfo.outputPath('question-paused-mobile.png'), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.getByRole('button', { name: '继续任务', exact: true }).click();
  await expect.poll(async () => (await (await page.request.get(`/api/runs/${run.id}`)).json()).status).toBe('queued');
  const done = await (await page.request.post(`/api/__offline/question/${run.id}`)).json();
  expect(done.status).toBe('completed');
  expect(done.tool_calls).toBe(1);
  await page.reload();
  await expect(page.getByText('已根据你的回答调整方案。', { exact: true })).toBeVisible();
  await expect(page.getByText('你的回答：初学者', { exact: true })).toBeVisible();
  expect(errors).toEqual([]);
});
