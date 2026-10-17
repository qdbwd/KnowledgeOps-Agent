import { test, expect } from '@playwright/test';
import { writeFile } from 'node:fs/promises';

const evidenceDir = '/workspace/artifacts/verification/optimization';

// 使用真实页面和 API，后端由 browser_server 创建隔离 schema，模型关闭。
test('tool policy preserves edits on conflict, distinguishes empty selection, and refreshes the effective preview', async ({
  page,
}) => {
  const errors: string[] = [];

  const baseRevision = (await (await page.request.get('/api/tools/policy')).json()).revision;
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto('/tools');
  await expect(page.getByRole('heading', { name: '工具与能力', level: 1 })).toBeVisible();
  const form = page.getByRole('form', { name: '工具策略配置' });
  const global = form.getByRole('group', { name: '全局工具开关' });
  const preview = page.getByRole('region', { name: '有效工具预览' });
  const source = preview
    .getByRole('listitem')
    .filter({ has: page.getByText('read_source', { exact: true }) });
  await expect(source.getByText('可用', { exact: true })).toBeVisible();
  await global.getByRole('checkbox', { name: /read_source/ }).uncheck();
  const savedHttp = page.waitForResponse(
    (r) => r.url().endsWith('/api/tools/policy') && r.request().method() === 'PATCH',
  );
  await form.getByRole('button', { name: '保存工具配置' }).click();
  const disabled = await (await savedHttp).json();
  expect(disabled).toMatchObject({
    revision: baseRevision + 1,
    enabled: { 'builtin:read_source': false },
  });
  await expect(source.getByText('全局已关闭', { exact: true })).toBeVisible();
  await page.reload();
  await expect(global.getByRole('checkbox', { name: /read_source/ })).not.toBeChecked();

  // 模拟另一个窗口先保存，当前编辑不能静默覆盖它。
  await global.getByRole('checkbox', { name: /read_file/ }).uncheck();
  const competing = await page.request.patch('/api/tools/policy', {
    data: { expected_revision: baseRevision + 1, enabled: { 'builtin:read_source': true } },
  });
  expect(competing.status()).toBe(200);
  await form.getByRole('button', { name: '保存工具配置' }).click();
  await expect(form.getByRole('alert')).toContainText('修订已变化');
  await expect(global.getByRole('checkbox', { name: /read_file/ })).not.toBeChecked();
  await form.getByRole('button', { name: '放弃本地修改并刷新' }).click();
  await expect(form.getByText(`当前配置修订 ${baseRevision + 2}`)).toBeVisible();
  await expect(global.getByRole('checkbox', { name: /read_file/ })).toBeChecked();

  await form.getByRole('checkbox', { name: '继承默认本地工具选择' }).uncheck();
  await form.getByRole('button', { name: '保存工具配置' }).click();
  await expect(form.getByText(`当前配置修订 ${baseRevision + 3}`)).toBeVisible();
  await expect(source.getByText('主Agent没有选择此工具或所属工具组')).toBeVisible();
  expect(await (await page.request.get('/api/tools/policy')).json()).toMatchObject({
    main_tools: [],
    main_tool_groups: [],
    resolved_main_tools: [],
  });
  await form.getByRole('checkbox', { name: '材料读取', exact: true }).check();
  await form.getByRole('button', { name: '保存工具配置' }).click();
  await expect(form.getByText(`当前配置修订 ${baseRevision + 4}`)).toBeVisible();
  await expect(source.getByText('可用', { exact: true })).toBeVisible();
  const grouped = await (await page.request.get('/api/tools/policy')).json();
  expect(grouped.resolved_main_tools).toEqual([
    'builtin:list_materials',
    'builtin:list_evidence',
    'builtin:inspect_material',
    'builtin:read_source',
  ]);
  await page.getByRole('button', { name: '查看配置历史' }).click();
  await expect(page.getByRole('heading', { name: '修订 0', exact: true })).toBeVisible();
  await expect(
    page.getByRole('heading', { name: `修订 ${baseRevision + 4}`, exact: true }),
  ).toBeVisible();
  await page.screenshot({ path: `${evidenceDir}/tool-ui-desktop.png`, fullPage: false });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(preview).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await preview.scrollIntoViewIfNeeded();
  await page.screenshot({ path: `${evidenceDir}/tool-ui-mobile.png`, fullPage: false });
  await form.getByRole('checkbox', { name: '继承默认本地工具选择' }).check();
  await form.getByRole('button', { name: '保存工具配置' }).click();
  await expect(form.getByText(`当前配置修订 ${baseRevision + 5}`)).toBeVisible();
  expect(await (await page.request.get('/api/tools/policy')).json()).toMatchObject({
    main_tools: null,
    main_tool_groups: null,
    resolved_main_tools: null,
  });
  const settings = await (await page.request.get('/api/settings')).json();
  expect(settings.model_calls_enabled).toBe(false);
  expect(errors).toEqual([]);
  await writeFile(
    `${evidenceDir}/tool-ui-policy-browser.json`,
    JSON.stringify(
      { disabled, grouped, errors, model_calls_enabled: settings.model_calls_enabled },
      null,
      2,
    ),
  );
});

test('saved subagent groups survive prompt edits and share global restrictions in the preview', async ({
  page,
}) => {
  await page.goto('/tools');
  await page.getByRole('link', { name: '管理子Agent', exact: true }).click();
  const form = page.getByRole('form', { name: '创建子Agent', exact: true });
  await form.getByLabel('稳定名称', { exact: true }).fill('browser-group-reviewer');
  await form.getByLabel('显示名称', { exact: true }).fill('工具组核对员');
  await form.getByLabel('用途说明', { exact: true }).fill('只读核对材料');
  await form.getByLabel('角色提示', { exact: true }).fill('核对给定材料。');
  await form.getByRole('checkbox', { name: '材料读取', exact: true }).check();
  await form.getByRole('checkbox', { name: /^继承只读 MCP/ }).uncheck();
  await form.getByRole('checkbox', { name: '创建后启用', exact: true }).check();
  const createdHttp = page.waitForResponse(
    (r) => r.url().endsWith('/api/subagents') && r.request().method() === 'POST',
  );
  await form.getByRole('button', { name: '创建子Agent', exact: true }).click();
  const created = await (await createdHttp).json();
  expect(created).toMatchObject({
    tools: [],
    tool_groups: ['materials_read'],
    resolved_tools: ['list_materials', 'list_evidence', 'inspect_material', 'read_source'],
  });
  const card = page.getByRole('region', { name: '工具组核对员', exact: true });
  await card.getByRole('button', { name: '编辑配置', exact: true }).click();
  await card
    .getByRole('textbox', { name: '角色提示', exact: true })
    .fill('只修改提示，保留工具选择。');
  const editedHttp = page.waitForResponse(
    (r) => r.url().endsWith(`/api/subagents/${created.id}`) && r.request().method() === 'PATCH',
  );
  await card.getByRole('button', { name: '保存配置', exact: true }).click();
  const response = await editedHttp;
  expect(response.request().postDataJSON()).not.toHaveProperty('tool_groups');
  expect(response.request().postDataJSON()).not.toHaveProperty('tools');
  const edited = await response.json();
  expect(edited.resolved_tools).toEqual(created.resolved_tools);
  await card.getByRole('link', { name: '查看有效工具' }).click();
  await expect(page).toHaveURL(new RegExp(`role_id=${created.id}$`));
  await page.reload();
  const preview = page.getByRole('region', { name: '有效工具预览' });
  const source = preview
    .getByRole('listitem')
    .filter({ has: page.getByText('read_source', { exact: true }) });
  await expect(source.getByText('可用', { exact: true })).toBeVisible();
  const global = page.getByRole('group', { name: '全局工具开关' });
  await global.getByRole('checkbox', { name: /read_source/ }).uncheck();
  await page.getByRole('button', { name: '保存工具配置' }).click();
  await expect(source.getByText('全局已关闭')).toBeVisible();
  await global.getByRole('checkbox', { name: /read_source/ }).check();
  await page.getByRole('button', { name: '保存工具配置' }).click();
  await expect(source.getByText('可用', { exact: true })).toBeVisible();
  await page.getByRole('link', { name: '管理子Agent', exact: true }).click();
  await card.getByRole('button', { name: '停用', exact: true }).click();
  await expect(card.getByText(/已停用 · 修订 3/)).toBeVisible();
  await card.getByRole('link', { name: '查看有效工具' }).click();
  await expect(source.getByText('该角色已停用或删除')).toBeVisible();
  expect(
    (
      await (await page.request.get(`/api/tools/effective?role_id=${created.id}`)).json()
    ).tools.every((tool: { available: boolean }) => !tool.available),
  ).toBe(true);
  await page.getByRole('link', { name: '管理子Agent', exact: true }).click();
  await card.getByRole('button', { name: '删除', exact: true }).click();
  await card.getByRole('button', { name: '确认删除此角色' }).click();
  await expect(card).toHaveCount(0);
  const settings = await (await page.request.get('/api/settings')).json();
  expect(settings.model_calls_enabled).toBe(false);
  await writeFile(
    `${evidenceDir}/tool-ui-group-browser.json`,
    JSON.stringify({ created, edited, model_calls_enabled: settings.model_calls_enabled }, null, 2),
  );
});
