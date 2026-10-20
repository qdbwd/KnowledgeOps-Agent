import { expect, test } from '@playwright/test';

test('candidate credentials, failed update and removal preserve the active connection', async ({
  page,
}, info) => {
  test.setTimeout(120000);
  const name = `MCP管理-${Date.now()}`;
  await page.goto('/mcp');
  await page.getByRole('button', { name: '导入 MCP 配置', exact: true }).click();
  let dialog = page.getByRole('dialog', { name: '导入 MCP 配置', exact: true });
  await dialog.getByLabel('配置 JSON', { exact: true }).fill(
    JSON.stringify({
      mcpServers: {
        [name]: {
          command: '/workspace/.venv/bin/python',
          args: ['/workspace/tests/fixtures/mcp_probe.py'],
          env: { DEMO_MCP_TOKEN: 'browser-private-value' },
        },
      },
    }),
  );
  await dialog.getByRole('button', { name: '检查配置' }).click();
  await expect(dialog.getByLabel('配置检查结果')).not.toContainText('browser-private-value');
  await dialog.getByRole('button', { name: '确认导入候选' }).click();
  const candidate = page.getByRole('region', { name: `候选：${name}`, exact: true });
  await candidate.getByRole('button', { name: '环境与凭据' }).click();
  dialog = page.getByRole('dialog', { name: '连接环境与凭据' });
  await expect(dialog.getByText('已配置 · 项目环境文件', { exact: true })).toBeVisible();
  await expect(dialog.locator('input[type=password]')).toHaveValue('');
  await dialog.locator('input[type=password]').fill('browser-rotated-value');
  await dialog.getByRole('button', { name: '保存环境值' }).click();
  await expect(dialog.getByRole('status').filter({ hasText: '已保存' })).toBeVisible();
  await expect(dialog.locator('input[type=password]')).toHaveValue('');
  await dialog.getByRole('button', { name: '关闭', exact: true }).click();
  await candidate.getByRole('button', { name: '测试连接并发现工具' }).click();
  await expect(candidate.getByLabel(/调用许可/)).toHaveValue('deny', { timeout: 45000 });
  await page.screenshot({ path: info.outputPath('mcp-review-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: info.outputPath('mcp-review-mobile.png'), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await candidate.getByLabel(/调用许可/).selectOption('read');
  await candidate.getByRole('checkbox', { name: '应用后启用连接' }).check();
  await candidate.getByRole('button', { name: '应用此候选配置' }).click();
  const active = page.getByRole('region', { name: `连接：${name}`, exact: true });
  await expect(active.getByRole('button', { name: '停用连接', exact: true })).toBeVisible();
  const exported = await page.request.get(
    (await active.getByRole('link', { name: '导出非秘密配置' }).getAttribute('href'))!,
  );
  expect(await exported.text()).not.toContain('browser-rotated-value');
  await active.getByText('配置与管理', { exact: true }).click();
  await active.getByRole('button', { name: '准备更新候选' }).click();
  dialog = page.getByRole('dialog', { name: `更新 MCP：${name}`, exact: true });
  await expect(dialog.getByLabel('配置 JSON', { exact: true })).toHaveValue(
    /\/workspace\/\.venv\/bin\/python/,
  );
  await dialog.getByLabel('配置 JSON', { exact: true }).fill(
    JSON.stringify({
      name,
      transport: 'http',
      url: 'http://127.0.0.1:9/mcp',
      timeout_seconds: 2,
    }),
  );
  await dialog.getByRole('button', { name: '检查配置' }).click();
  await dialog.getByRole('button', { name: '确认导入候选' }).click();
  await candidate.getByRole('button', { name: '测试连接并发现工具' }).click();
  await expect(candidate.getByText(/连接测试失败/)).toBeVisible({ timeout: 15000 });
  await expect(active.getByRole('button', { name: '停用连接', exact: true })).toBeVisible();
  await candidate.getByRole('button', { name: '移除候选' }).click();
  await expect(candidate).toHaveCount(0);
  await page.reload();
  await expect(active).toBeVisible();
});

test('explicit uv preparation is visible and its exported configuration is portable', async ({
  page,
}, info) => {
  test.setTimeout(240000);
  const name = `时区服务-${Date.now()}`;
  await page.goto('/mcp');
  await page.getByRole('button', { name: '导入 MCP 配置', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '导入 MCP 配置', exact: true });
  await dialog
    .getByLabel('配置 JSON', { exact: true })
    .fill(JSON.stringify({ name, command: 'uvx', args: ['mcp-server-time'] }));
  await dialog.getByRole('button', { name: '检查配置' }).click();
  await expect(dialog.getByLabel('配置检查结果')).toContainText('需明确准备 uv 依赖');
  await dialog.getByRole('button', { name: '确认导入候选' }).click();
  const card = page.getByRole('region', { name: `候选：${name}`, exact: true });
  await expect(card.getByRole('status')).toContainText('等待准备依赖');
  await card.getByRole('button', { name: '准备依赖', exact: true }).click();
  await expect(card.getByRole('status')).toContainText('准备已排队');
  const operations = await (await page.request.get('/api/mcp/operations')).json();
  const operation = operations.find(
    (row: { parameters: { name: string } }) => row.parameters.name === name,
  );
  // 由测试入口推进生产消费者；Redis重投/去重由真实broker集成用例验证。
  const prepared = await page.request.post(`/api/__offline/mcp-preparation/${operation.id}`, {
    timeout: 180000,
  });
  expect(prepared.ok()).toBe(true);
  expect((await prepared.json()).status).toBe('prepared');
  await expect(card.getByRole('status')).toContainText('等待测试连接', { timeout: 10000 });
  await card.getByRole('button', { name: '测试连接并发现工具' }).click();
  await expect(card.getByRole('status')).toContainText('等待审查工具许可', { timeout: 45000 });
  await card
    .getByRole('combobox', { name: 'get_current_time 调用许可', exact: true })
    .selectOption('read');
  await page.screenshot({ path: info.outputPath('mcp-prepared.png'), fullPage: true });
  await card.getByRole('checkbox', { name: '应用后启用连接' }).check();
  await card.getByRole('button', { name: '应用此候选配置' }).click();
  const active = page.getByRole('region', { name: `连接：${name}`, exact: true });
  const exported = await page.request.get(
    (await active.getByRole('link', { name: '导出非秘密配置' }).getAttribute('href'))!,
  );
  const body = await exported.json();
  expect(body.config.command).toBe('uvx');
  expect(body.config.installation_id).toBeUndefined();
  expect(body.config.cwd).toBeUndefined();
  await active.getByRole('button', { name: '停用连接', exact: true }).click();
  await expect(active.getByRole('button', { name: '启用连接', exact: true })).toBeVisible();
  await active.getByText('配置与管理', { exact: true }).click();
  await active.getByRole('button', { name: '删除连接配置' }).click();
  const removal = page.getByRole('dialog', { name: `移除 MCP：${name}`, exact: true });
  await expect(removal).toContainText('专属数据目录');
  await removal.getByRole('button', { name: '取消', exact: true }).click();
  await expect(active).toBeVisible();
  await active.getByRole('button', { name: '删除连接配置' }).click();
  await removal.getByRole('button', { name: '确认移除连接' }).click();
  await expect(active).toHaveCount(0);
  // 测试服务不自动维护；按生产批次推进，前序场景的清理不能冒充本连接已完成。
  await expect
    .poll(
      async () => {
        expect((await page.request.post('/api/__offline/mcp-cleanup')).ok()).toBe(true);
        const response = await page.request.get('/api/mcp/cleanup');
        expect(response.ok()).toBe(true);
        const rows = await response.json();
        return rows.find(
          (row: { target: string; status: string }) => row.target === operation.target,
        )?.status;
      },
      { timeout: 15000 },
    )
    .toBe('completed');
  await page.getByRole('button', { name: '刷新', exact: true }).click();
  await page.getByText('后台清理与失败重试', { exact: true }).click();
  const cleanup = page.getByRole('region', { name: `清理：${name}`, exact: true });
  await expect(cleanup).toContainText('本次可回收内容已清理');
  await expect(cleanup).toContainText('已回收安装 1 个');
  await page.screenshot({ path: info.outputPath('mcp-removed.png'), fullPage: true });
});
