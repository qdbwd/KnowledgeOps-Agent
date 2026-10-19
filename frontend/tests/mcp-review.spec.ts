import { expect, test } from '@playwright/test';

test('model-denied MCP waits, refresh preserves reasons, human once decision uses the original receipt', async ({ page }, testInfo) => {
  test.setTimeout(180000);
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  const connection = await (await page.request.post('/api/mcp/connections', { data: {
    name: '审批协议验证', config: { transport: 'stdio', command: '/workspace/.venv/bin/python', args: ['/workspace/tests/fixtures/mcp_probe.py'] },
  } })).json();
  const discovered = await (await page.request.post(`/api/mcp/connections/${connection.id}/discover`, { data: { expected_revision: 1 } })).json();
  expect((await page.request.patch(`/api/mcp/connections/${connection.id}`, { data: {
    expected_revision: discovered.revision, enabled: true, permissions: { get_note: 'confirm' },
  } })).ok()).toBeTruthy();
  const conv = await (await page.request.post('/api/conversations', { data: { title: 'MCP审查待确认' } })).json();
  await page.goto(`/chat/${conv.id}`);
  const mode = page.getByRole('combobox', { name: '执行权限' });
  await expect(mode.getByRole('option', { name: '自动执行', exact: true })).toHaveCount(0);
  await mode.selectOption('auto_review');
  await expect.poll(async () => (await (await page.request.get(`/api/conversations/${conv.id}/runtime-settings`)).json()).permission_mode).toBe('auto_review');
  const run = await (await page.request.post(`/api/conversations/${conv.id}/messages`, { data: {
    content: '通过已配置的服务核对指定内容', request_key: crypto.randomUUID(),
  } })).json();
  const advance = async () => (await page.request.post(`/api/__offline/mcp-review/${run.id}`)).json();
  const waiting = await advance();
  expect(waiting).toMatchObject({ status: 'interrupted', outcome: { kind: 'waiting_approval' } });
  const before = await (await page.request.get(`/api/runs/${run.id}/mcp`)).json();
  expect(before).toHaveLength(1);
  expect(before[0]).toMatchObject({ status: 'pending', decision: { actor: 'model', review: { result: { verdict: 'deny' } } } });
  await page.reload();
  await expect(page.getByText(/审查未批准，尚未调用服务/)).toBeVisible();
  await expect(mode).toHaveValue('auto_review');
  await page.screenshot({ path: testInfo.outputPath('mcp-review-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole('button', { name: '允许这次调用' }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath('mcp-review-mobile.png'), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.getByRole('button', { name: '允许这次调用' }).click();
  await expect.poll(async () => (await (await page.request.get(`/api/runs/${run.id}`)).json()).status).toBe('queued');
  expect(await advance()).toMatchObject({ status: 'completed' });
  const after = await (await page.request.get(`/api/runs/${run.id}/mcp`)).json();
  expect(after).toHaveLength(1);
  expect(after[0]).toMatchObject({ id: before[0].id, input_hash: before[0].input_hash, status: 'succeeded', decision: { actor: 'user' } });
  expect(after[0].result.source_version_id).toBeTruthy();
  expect(await advance()).toBeNull();
  expect(errors).toEqual([]);
});
