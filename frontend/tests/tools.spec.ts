import { test, expect, type Page } from '@playwright/test';
import { mkdir, writeFile } from 'node:fs/promises';

const evidenceDir = '/workspace/artifacts/verification/capability-management';
test.beforeAll(async () => {
  await mkdir(evidenceDir, { recursive: true });
});

type PolicySnapshot = {
  revision: number;
  enabled: Record<string, boolean>;
  skill_enabled: Record<string, boolean>;
  main_tools: string[] | null;
  main_tool_groups: string[] | null;
};
let previousPolicy: PolicySnapshot;
test.beforeEach(async ({ request }) => {
  previousPolicy = await (await request.get('/api/tools/policy')).json();
  // 整套浏览器共享隔离服务；显式建立本例前提，不依赖前一用例的全局配置。
  const response = await request.patch('/api/tools/policy', { data: {
    expected_revision: previousPolicy.revision,
    enabled: { 'builtin:read_source': true, 'middleware:read_file': true },
    skill_enabled: { 'code-documentation': true },
    main_tools: null, main_tool_groups: null,
  } });
  expect(response.ok()).toBe(true);
});
test.afterEach(async ({ request }) => {
  const current = await (await request.get('/api/tools/policy')).json();
  expect((await request.patch('/api/tools/policy', { data: {
    expected_revision: current.revision,
    enabled: previousPolicy.enabled, skill_enabled: previousPolicy.skill_enabled,
    main_tools: previousPolicy.main_tools, main_tool_groups: previousPolicy.main_tool_groups,
  } })).ok()).toBe(true);
});

async function select(page: Page, button: string, option: string, checked: boolean) {
  await page.getByRole('button', { name: button, exact: true }).click();
  await page
    .getByRole('dialog')
    .last()
    .getByRole('checkbox', { name: option, exact: true })
    .setChecked(checked);
  await page.getByRole('dialog').last().getByRole('button', { name: '完成', exact: true }).click();
}
async function preview(page: Page, role = false) {
  await page.getByRole('button', { name: '查看有效工具', exact: true }).click();
  return page.getByRole('dialog', {
    name: role ? '角色的有效工具' : '主 Agent 的有效工具',
    exact: true,
  });
}
async function closePreview(page: Page, role = false) {
  await page
    .getByRole('button', {
      name: role ? '关闭角色的有效工具' : '关闭主 Agent 的有效工具',
      exact: true,
    })
    .click();
}

// 真正API、临时PG和浏览器，配置结果不由测试伪造。
test('compact tool settings retain conflict edits, empty selections, groups, and saved effective reasons', async ({
  page,
}) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  const base = (await (await page.request.get('/api/tools/policy')).json()).revision;
  const reads: string[] = [];
  page.on('request', (request) => {
    if (request.url().includes('/api/tools/effective')) reads.push(request.url());
  });
  await page.goto('/tools');
  await expect(page.getByRole('heading', { name: '工具与能力', level: 1 })).toBeVisible();
  const form = page.getByRole('form', { name: '工具策略配置' });
  await expect(form.getByRole('button', { name: '管理全部工具' })).toBeEnabled();
  expect(reads).toEqual([]);
  await expect(page.getByRole('region', { name: '有效工具预览' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: '查看配置历史' })).toHaveCount(0);
  await select(page, '管理全部工具', '读取材料正文', false);
  await form.getByRole('button', { name: '保存工具配置' }).click();
  await expect(form).toContainText(`当前配置修订 ${base + 1}`);
  let view = await preview(page);
  let source = view
    .getByRole('listitem')
    .filter({ has: page.getByText('读取材料正文', { exact: true }) });
  await expect(source).toContainText('全局已关闭');
  await closePreview(page);
  await page.reload();
  await page.getByRole('button', { name: '管理全部工具' }).click();
  await expect(page.getByRole('checkbox', { name: '读取材料正文', exact: true })).not.toBeChecked();
  await page.getByRole('button', { name: '完成', exact: true }).click();

  await select(page, '管理全部工具', '读取文件与技能', false);
  expect(
    (
      await page.request.patch('/api/tools/policy', {
        data: { expected_revision: base + 1, enabled: { 'builtin:read_source': true } },
      })
    ).status(),
  ).toBe(200);
  await form.getByRole('button', { name: '保存工具配置' }).click();
  await expect(form.getByRole('alert')).toContainText('修订已变化');
  await page.getByRole('button', { name: '管理全部工具' }).click();
  await expect(
    page.getByRole('checkbox', { name: '读取文件与技能', exact: true }),
  ).not.toBeChecked();
  await page.getByRole('button', { name: '完成', exact: true }).click();
  await form.getByRole('button', { name: '放弃本地修改并刷新' }).click();
  await expect(form).toContainText(`当前配置修订 ${base + 2}`);
  await page.getByRole('button', { name: '管理全部工具' }).click();
  await expect(page.getByRole('checkbox', { name: '读取文件与技能', exact: true })).toBeChecked();
  await page.getByRole('button', { name: '完成', exact: true }).click();
  await form.getByRole('checkbox', { name: '继承默认本地工具选择' }).uncheck();
  await form.getByRole('button', { name: '保存工具配置' }).click();
  await expect(form).toContainText(`当前配置修订 ${base + 3}`);
  expect(await (await page.request.get('/api/tools/policy')).json()).toMatchObject({
    main_tools: [],
    main_tool_groups: [],
    resolved_main_tools: [],
  });
  view = await preview(page);
  source = view
    .getByRole('listitem')
    .filter({ has: page.getByText('读取材料正文', { exact: true }) });
  await expect(source).toContainText('主Agent没有选择此工具或所属工具组');
  await closePreview(page);
  await page.getByRole('button', { name: '选择工具组' }).click();
  await page.getByRole('button', { name: '查看材料读取详情' }).click();
  const group = page.getByRole('dialog', { name: '材料读取', exact: true });
  await expect(group.getByRole('listitem')).toHaveCount(4);
  await expect(group).toContainText('读取材料正文');
  await group.getByRole('button', { name: '关闭材料读取' }).click();
  await page.getByRole('checkbox', { name: '材料读取', exact: true }).check();
  await page.getByRole('button', { name: '完成', exact: true }).click();
  await form.getByRole('button', { name: '保存工具配置' }).click();
  await expect(form).toContainText(`当前配置修订 ${base + 4}`);
  const grouped = await (await page.request.get('/api/tools/policy')).json();
  expect(grouped.resolved_main_tools).toEqual([
    'builtin:list_materials',
    'builtin:list_evidence',
    'builtin:inspect_material',
    'builtin:read_source',
  ]);
  view = await preview(page);
  await expect(
    view
      .getByRole('listitem')
      .filter({ has: page.getByText('读取材料正文', { exact: true }) })
      .getByText('可用', { exact: true }),
  ).toBeVisible();
  await closePreview(page);
  await page.screenshot({ path: `${evidenceDir}/tools-desktop.png`, fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await form.scrollIntoViewIfNeeded();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.screenshot({ path: `${evidenceDir}/tools-mobile.png`, fullPage: true });
  await form.getByRole('checkbox', { name: '继承默认本地工具选择' }).check();
  await form.getByRole('button', { name: '保存工具配置' }).click();
  await expect(form).toContainText(`当前配置修订 ${base + 5}`);
  expect(await (await page.request.get('/api/tools/policy')).json()).toMatchObject({
    main_tools: null,
    main_tool_groups: null,
    resolved_main_tools: null,
  });
  expect((await (await page.request.get('/api/settings')).json()).model_calls_enabled).toBe(false);
  expect(errors).toEqual([]);
  await writeFile(`${evidenceDir}/policy.json`, JSON.stringify({ grouped, errors }, null, 2));
});

test('role selections use the same picker and old preview URLs open the precise role', async ({
  page,
}) => {
  await page.goto('/subagents');
  await page.getByRole('link', { name: '新建子Agent', exact: true }).click();
  const form = page.getByRole('form', { name: '创建子Agent', exact: true });
  await form.getByLabel('稳定名称', { exact: true }).fill('browser-group-reviewer');
  await form.getByLabel('显示名称', { exact: true }).fill('工具组核对员');
  await form.getByLabel('用途说明', { exact: true }).fill('只读核对材料');
  await form.getByLabel('角色提示', { exact: true }).fill('核对给定材料。');
  await select(page, '选择工具组', '材料读取', true);
  await select(page, '选择工具组', '继承只读 MCP', false);
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
  await card.getByRole('link', { name: '编辑配置', exact: true }).click();
  const editor = page.getByRole('form', { name: '编辑 工具组核对员' });
  await editor
    .getByRole('textbox', { name: '角色提示', exact: true })
    .fill('只修改提示，保留工具选择。');
  const editedHttp = page.waitForResponse(
    (r) => r.url().endsWith(`/api/subagents/${created.id}`) && r.request().method() === 'PATCH',
  );
  await editor.getByRole('button', { name: '保存配置', exact: true }).click();
  const response = await editedHttp;
  expect(response.request().postDataJSON()).not.toHaveProperty('tool_groups');
  expect(response.request().postDataJSON()).not.toHaveProperty('tools');
  expect((await response.json()).resolved_tools).toEqual(created.resolved_tools);
  await page.goto(`/tools?role_id=${created.id}`);
  await expect(page).toHaveURL(new RegExp(`/subagents\\?effective=${created.id}$`));
  let view = page.getByRole('dialog', { name: '角色的有效工具', exact: true });
  await expect(
    view
      .getByRole('listitem')
      .filter({ has: page.getByText('读取材料正文', { exact: true }) })
      .getByText('可用', { exact: true }),
  ).toBeVisible();
  await closePreview(page, true);
  const policy = await (await page.request.get('/api/tools/policy')).json();
  await page.request.patch('/api/tools/policy', {
    data: { expected_revision: policy.revision, enabled: { 'builtin:read_source': false } },
  });
  await page.reload();
  view = page.getByRole('dialog', { name: '角色的有效工具', exact: true });
  await expect(
    view.getByRole('listitem').filter({ has: page.getByText('读取材料正文', { exact: true }) }),
  ).toContainText('全局已关闭');
  await closePreview(page, true);
  await card.getByRole('button', { name: '停用', exact: true }).click();
  await expect(card).toContainText('已停用 · 修订 3');
  await card.getByRole('button', { name: '查看有效工具', exact: true }).click();
  await expect(page.getByRole('dialog', { name: '角色的有效工具' })).toContainText(
    '该角色已停用或删除',
  );
  await closePreview(page, true);
  expect(
    (
      await (await page.request.get(`/api/tools/effective?role_id=${created.id}`)).json()
    ).tools.every((tool: { available: boolean }) => !tool.available),
  ).toBe(true);
  await card.getByRole('button', { name: '删除', exact: true }).click();
  await page
    .getByRole('dialog', { name: '删除工具组核对员？' })
    .getByRole('button', { name: '确认删除此角色' })
    .click();
  await expect(card).toHaveCount(0);
  await page.request.patch('/api/tools/policy', {
    data: { expected_revision: policy.revision + 1, enabled: { 'builtin:read_source': true } },
  });
});

test('skill full text and source resources are lazy, switches persist, and tool schema is readable', async ({
  page,
}) => {
  const requests: string[] = [];
  page.on('request', (request) => requests.push(request.url()));
  await page.goto('/tools');
  await page.getByRole('button', { name: '管理全部 Skills' }).click();
  const picker = page.getByRole('dialog', { name: 'Skills', exact: true });
  await picker.getByRole('searchbox').fill('code-documentation');
  expect(requests.some((url) => url.includes('/api/skills/code-documentation'))).toBe(false);
  await picker.getByRole('button', { name: '查看代码文档详情' }).click();
  const detail = page.getByRole('dialog', { name: '代码文档', exact: true });
  await expect(detail.getByRole('heading', { name: 'Code Documentation Skill' })).toBeVisible();
  await expect(detail.getByRole('heading', { name: '配套资源' })).toBeVisible();
  await page.screenshot({ path: `${evidenceDir}/skill-detail-desktop.png`, fullPage: false });
  await detail
    .getByRole('listitem')
    .filter({ hasText: 'SKILL.md' })
    .getByRole('button', { name: '查看内容' })
    .click();
  const source = page.getByRole('dialog', { name: 'SKILL.md', exact: true });
  await expect(source.locator('pre')).toContainText('name: code-documentation');
  await page.keyboard.press('Escape');
  await expect(source).toHaveCount(0);
  await page.keyboard.press('Escape');
  await expect(detail).toHaveCount(0);
  await expect(picker.getByRole('button', { name: '查看代码文档详情' })).toBeFocused();
  await picker.getByRole('checkbox', { name: '代码文档', exact: true }).uncheck();
  await picker.getByRole('button', { name: '完成', exact: true }).click();
  const saved = page.waitForResponse(
    (r) => r.url().endsWith('/api/tools/policy') && r.request().method() === 'PATCH',
  );
  await page.getByRole('button', { name: '保存工具配置' }).click();
  expect((await (await saved).json()).skill_enabled['code-documentation']).toBe(false);
  await page.reload();
  await page.getByRole('button', { name: '管理全部 Skills' }).click();
  await picker.getByRole('searchbox').fill('code-documentation');
  await expect(picker.getByRole('checkbox', { name: '代码文档', exact: true })).not.toBeChecked();
  await picker.getByRole('searchbox').fill('image-generation');
  await expect(
    picker.getByRole('checkbox', { name: 'image-generation', exact: true }),
  ).toHaveCount(0);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: `${evidenceDir}/skill-removed-mobile.png`, fullPage: false });
  await picker.getByRole('searchbox').fill('code-documentation');
  await picker.getByRole('checkbox', { name: '代码文档', exact: true }).check();
  await picker.getByRole('button', { name: '完成', exact: true }).click();
  await page.getByRole('button', { name: '保存工具配置' }).click();
  await expect(page.getByRole('button', { name: '保存工具配置' })).toBeDisabled();
  await page.getByRole('button', { name: '管理全部工具' }).click();
  const tools = page.getByRole('dialog', { name: '全局工具开关', exact: true });
  await tools.getByRole('searchbox').fill('read_source');
  await tools.getByRole('button', { name: '查看读取材料正文详情' }).click();
  const tool = page.getByRole('dialog', { name: '读取材料正文', exact: true });
  await expect(tool.getByRole('heading', { name: '参数定义' })).toBeVisible();
  await expect(tool.locator('pre')).toContainText('segment_id');
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.screenshot({ path: `${evidenceDir}/tool-detail-mobile.png`, fullPage: false });
  expect((await (await page.request.get('/api/settings')).json()).model_calls_enabled).toBe(false);
});
