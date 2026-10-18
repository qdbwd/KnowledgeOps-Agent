import { expect, test } from '@playwright/test';

test.use({ actionTimeout: 15000 });

test('approval survives refresh and continues the original command once; modes and rules persist', async ({
  page,
}, testInfo) => {
  test.setTimeout(120000);
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  const conv = await (
    await page.request.post('/api/conversations', { data: { title: '命令审批闭环' } })
  ).json();
  const run = await (
    await page.request.post(`/api/conversations/${conv.id}/messages`, {
      data: { content: '在工作区保存示例文件', request_key: crypto.randomUUID() },
    })
  ).json();
  const result = await (await page.request.post(`/api/__offline/execution/${run.id}`)).json();
  expect(result).toMatchObject({
    status: 'interrupted',
    outcome: { kind: 'waiting_approval' },
    tool_calls: 0,
  });
  await page.goto(`/chat/${conv.id}`);
  await expect(page.getByRole('button', { name: '允许一次' })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('button', { name: '允许一次' })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('approval-desktop.png'), fullPage: true });
  await page.getByRole('button', { name: '允许一次' }).click();
  await expect
    .poll(async () => (await (await page.request.get(`/api/runs/${run.id}`)).json()).status)
    .toBe('queued');
  const done = await (await page.request.post(`/api/__offline/execution/${run.id}`)).json();
  expect(done.status).toBe('completed');
  expect(done.tool_calls).toBe(1);
  expect(await (await page.request.post(`/api/__offline/execution/${run.id}`)).json()).toBeNull();
  expect(
    await (await page.request.get('/api/workspace/download?path=work/harness-marker.txt')).text(),
  ).toBe('marker');
  await page.reload();
  await page.getByRole('combobox', { name: '执行权限' }).selectOption('auto');
  await expect
    .poll(
      async () =>
        (await (await page.request.get(`/api/conversations/${conv.id}/runtime-settings`)).json())
          .permission_mode,
    )
    .toBe('auto');
  await page.getByRole('combobox', { name: '思考强度' }).selectOption('low');
  await expect
    .poll(
      async () =>
        (await (await page.request.get(`/api/conversations/${conv.id}/runtime-settings`)).json())
          .reasoning_effort,
    )
    .toBe('low');
  await page.reload();
  await expect(page.getByRole('combobox', { name: '执行权限' })).toHaveValue('auto');
  await expect(page.getByRole('combobox', { name: '思考强度' })).toHaveValue('low');
  await page.getByRole('button', { name: '更多设置与材料' }).click();
  await page.getByRole('link', { name: '命令规则', exact: true }).click();
  await page.getByText('手动添加规则', { exact: true }).click();
  await page.getByRole('combobox', { name: '作用域' }).selectOption('session');
  await page.getByRole('button', { name: '保存命令规则' }).click();
  const rule = page.locator('article').filter({ hasText: '["git","status"]' });
  await expect(rule).toContainText('本会话');
  await rule.getByRole('combobox').selectOption('deny');
  await expect(rule.getByRole('combobox')).toHaveValue('deny');
  await rule.getByRole('button', { name: '停用规则' }).click();
  await expect(rule.getByRole('button', { name: '启用规则' })).toBeVisible();
  await page.reload();
  await expect(rule.getByRole('button', { name: '启用规则' })).toBeVisible();
  await rule.getByRole('button', { name: '删除规则' }).click();
  await expect(rule).toHaveCount(0);
  expect(errors).toEqual([]);
});

test('unknown command result requires explicit resolution and skip never executes it twice', async ({
  page,
}, testInfo) => {
  test.setTimeout(120000);
  const conv = await (
    await page.request.post('/api/conversations', { data: { title: '未知回执核对' } })
  ).json();
  await page.request.patch(`/api/conversations/${conv.id}/runtime-settings`, {
    data: { expected_revision: 0, permission_mode: 'auto' },
  });
  const run = await (
    await page.request.post(`/api/conversations/${conv.id}/messages`, {
      data: { content: '执行命令并保留回执', request_key: crypto.randomUUID() },
    })
  ).json();
  const before = await (
    await page.request.get('/api/workspace/download?path=work/harness-marker.txt')
  ).text();
  const unknown = await (
    await page.request.post(`/api/__offline/execution/${run.id}?lose_receipt=true`)
  ).json();
  expect(unknown.outcome.kind).toBe('execution_unknown');
  await page.goto(`/chat/${conv.id}`);
  await expect(page.getByText(/执行结果未知 · 第/)).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: testInfo.outputPath('unknown-mobile.png'), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.getByRole('button', { name: '已核对，跳过并继续' }).click();
  await expect
    .poll(async () => (await (await page.request.get(`/api/runs/${run.id}`)).json()).status)
    .toBe('queued');
  const done = await (await page.request.post(`/api/__offline/execution/${run.id}`)).json();
  expect(done.status).toBe('completed');
  expect(done.tool_calls).toBe(1);
  const after = await (
    await page.request.get('/api/workspace/download?path=work/harness-marker.txt')
  ).text();
  expect(after).toBe(before + 'marker');
  const receipt = await (await page.request.get(`/api/runs/${run.id}/approvals`)).json();
  expect(receipt.commands[0].status).toBe('skipped');
});
