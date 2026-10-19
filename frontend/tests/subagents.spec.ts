import { test, expect } from '@playwright/test';
import { mkdir, writeFile } from 'node:fs/promises';

const evidenceDir = '/workspace/artifacts/verification/subagent-management';
test.beforeAll(async () => {
  await mkdir(evidenceDir, { recursive: true });
});

test('subagent configuration persists revisions, conflict recovery and deleted history in the browser', async ({
  page,
}) => {
  const pageErrors: string[] = [];
  page.on('pageerror', (error) => pageErrors.push(error.message));
  const settings = await (await page.request.get('/api/settings')).json();
  expect(settings.model_calls_enabled).toBe(false);
  const before = await (await page.request.get('/api/subagents')).json();
  expect(before.some((row: { id: string }) => row.id === 'builtin-researcher')).toBe(false);
  await page.goto('/');
  await page.getByRole('link', { name: '子Agent', exact: true }).click();
  await expect(page.getByRole('form')).toHaveCount(0);
  await expect(page.getByRole('button', { name: '查看历史', exact: true })).toHaveCount(0);
  await page.getByRole('link', { name: '新建子Agent', exact: true }).click();
  await expect(page).toHaveURL(/\/subagents\/new$/);
  const form = page.getByRole('form', { name: '创建子Agent', exact: true });
  await form.getByLabel('稳定名称', { exact: true }).fill('browser-reviewer');
  await form.getByLabel('显示名称', { exact: true }).fill('浏览器夹具核对员');
  await form.getByLabel('用途说明', { exact: true }).fill('仅浏览器验收配置，非模型运行');
  await form.getByLabel('角色提示', { exact: true }).fill('核对已分配材料，不扩展结论。');
  await form.getByRole('button', { name: '选择工具', exact: true }).click();
  await expect(page.getByRole('checkbox', { name: '读取材料正文', exact: true })).not.toBeChecked();
  await page.getByRole('checkbox', { name: '读取材料正文', exact: true }).check();
  await page.getByRole('button', { name: '完成', exact: true }).click();
  await form.getByRole('button', { name: '选择Skills' }).click();
  await page.getByRole('checkbox', { name: '证据核对', exact: true }).check();
  await page.getByRole('button', { name: '完成', exact: true }).click();
  await form.getByRole('checkbox', { name: '创建后启用', exact: true }).check();
  await page.screenshot({ path: `${evidenceDir}/editor-desktop.png`, fullPage: true });
  const createdResponse = page.waitForResponse(
    (r) => r.url().endsWith('/api/subagents') && r.request().method() === 'POST',
  );
  await form.getByRole('button', { name: '创建子Agent', exact: true }).click();
  const createdHttp = await createdResponse;
  expect(createdHttp.status()).toBe(201);
  const created = await createdHttp.json();
  expect(created).toMatchObject({
    name: 'browser-reviewer',
    revision: 1,
    tools: ['read_source'],
    skills: ['evidence-check'],
    enabled: true,
  });
  const card = page.getByRole('region', { name: '浏览器夹具核对员', exact: true });
  await expect(card).toContainText('已启用 · 修订 1');
  await page.reload();
  await expect(card).toBeVisible();
  await card.getByRole('link', { name: '编辑配置', exact: true }).click();
  const editor = page.getByRole('form', { name: '编辑 浏览器夹具核对员', exact: true });
  await expect(editor.getByLabel('稳定名称')).toHaveAttribute('readonly', '');
  await editor.getByLabel('角色提示').fill('保存精确条件与引用，不扩展结论。');
  await editor.getByRole('button', { name: '保存配置', exact: true }).click();
  await expect(card).toContainText('已启用 · 修订 2');
  await card.getByRole('link', { name: '编辑配置', exact: true }).click();
  await editor.getByLabel('角色提示').fill('这段本地输入必须在冲突后保留');
  const competitorHttp = await page.request.patch(`/api/subagents/${created.id}`, {
    data: { expected_revision: 2, description: '另一个窗口保存的用途说明' },
  });
  expect(competitorHttp.status()).toBe(200);
  const competitor = await competitorHttp.json();
  const conflictResponse = page.waitForResponse(
    (r) => r.url().endsWith(`/api/subagents/${created.id}`) && r.status() === 409,
  );
  await editor.getByRole('button', { name: '保存配置', exact: true }).click();
  const conflict = await (await conflictResponse).json();
  await expect(page.getByRole('alert')).toContainText('修订已变化');
  await expect(editor.getByLabel('角色提示')).toHaveValue('这段本地输入必须在冲突后保留');
  await page.screenshot({ path: `${evidenceDir}/conflict-desktop.png`, fullPage: true });
  await page.getByRole('button', { name: '放弃本地修改并刷新' }).click();
  await page.getByRole('button', { name: '确认放弃并刷新' }).click();
  await expect(editor.getByLabel('角色提示')).toHaveValue('保存精确条件与引用，不扩展结论。');
  await expect(editor.getByLabel('用途说明')).toHaveValue('另一个窗口保存的用途说明');
  await editor.getByRole('button', { name: '取消', exact: true }).click();
  await card.getByRole('button', { name: '停用', exact: true }).click();
  await expect(card).toContainText('已停用 · 修订 4');
  await expect(page.getByRole('region', { name: '配置历史', exact: true })).toHaveCount(0);
  await page.goto(`/subagents?history=${created.id}`);
  const history = page.getByRole('region', { name: '配置历史', exact: true });
  await expect(history.locator('details')).toHaveCount(4);
  await expect(history.getByText('核对已分配材料，不扩展结论。', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '关闭历史' }).click();
  await card.getByRole('button', { name: '删除', exact: true }).click();
  const deletion = page.getByRole('dialog', { name: '删除浏览器夹具核对员？', exact: true });
  await expect(deletion).toContainText('确认删除「浏览器夹具核对员」（browser-reviewer）');
  await deletion.getByRole('button', { name: '取消删除', exact: true }).click();
  expect(
    await (await page.request.get(`/api/subagents/${created.id}/history`)).json(),
  ).toHaveLength(4);
  await card.getByRole('button', { name: '删除', exact: true }).click();
  await deletion.getByRole('button', { name: '确认删除此角色', exact: true }).click();
  await expect(card).toHaveCount(0);
  await expect(history).toHaveCount(0);
  await page.goto(`/subagents?history=${created.id}`);
  await page.reload();
  await expect(history.locator('details')).toHaveCount(5);
  await expect(history.locator('summary').last()).toContainText('已删除');
  const revisions = await (await page.request.get(`/api/subagents/${created.id}/history`)).json();
  expect(revisions.map((row: { revision: number }) => row.revision)).toEqual([1, 2, 3, 4, 5]);
  expect(revisions.at(-1)).toMatchObject({ deleted: true, enabled: false });
  expect(revisions[3].authorization_epoch).toBeGreaterThan(created.authorization_epoch);
  const remaining = await (await page.request.get('/api/subagents')).json();
  expect(remaining.map((row: { id: string }) => row.id).sort()).toEqual(
    before.map((row: { id: string }) => row.id).sort(),
  );
  expect(pageErrors).toEqual([]);
  await writeFile(
    `${evidenceDir}/crud.json`,
    JSON.stringify({ created, competitor, conflict, revisions, pageErrors }, null, 2),
  );
});
test('chat defaults auxiliary off and persists the user-controlled delegation choice without executing a model', async ({
  page,
}) => {
  await page.goto('/');
  await page.getByRole('button', { name: '更多设置与材料' }).click();
  const auxiliary = page.getByRole('checkbox', { name: '允许辅助模型', exact: true });
  const delegation = page.getByRole('checkbox', { name: '允许子Agent分工', exact: true });
  await expect(auxiliary).not.toBeChecked();
  await delegation.uncheck();
  await page.getByLabel('发送给 KnowledgeOps 的消息').fill('浏览器排队夹具：不执行模型。');
  const queuedResponse = page.waitForResponse(
    (response) =>
      /\/api\/conversations\/[^/]+\/messages$/.test(response.url()) &&
      response.request().method() === 'POST',
  );
  await page.getByRole('button', { name: '发送', exact: true }).click();
  const first = await (await queuedResponse).json();
  expect(first.status).toBe('queued');
  expect(first.input).toMatchObject({ auxiliary: false, delegation: false });
  await page.reload();
  await page.getByRole('button', { name: '更多设置与材料' }).click();
  await expect(page.getByRole('checkbox', { name: '允许辅助模型', exact: true })).not.toBeChecked();
  await expect(page.locator('.message.user')).toHaveCount(1);
  const settings = await (await page.request.get('/api/settings')).json();
  expect(settings.model_calls_enabled).toBe(false);
  const saved = await (await page.request.get(`/api/runs/${first.id}`)).json();
  expect(saved.status).toBe('queued');
  expect(saved.tool_calls).toBe(0);
  expect(saved.input).toMatchObject({ auxiliary: false, delegation: false });
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: '取消排队', exact: true }).click();
  await expect(page.getByText('已取消', { exact: true })).toBeVisible();
  const cancelled = await (await page.request.get(`/api/runs/${first.id}`)).json();
  expect(cancelled.status).toBe('cancelled');
  await writeFile(
    `${evidenceDir}/subagents-browser-chat.json`,
    JSON.stringify(
      {
        first,
        saved,
        cancelled,
        model_calls_enabled: settings.model_calls_enabled,
      },
      null,
      2,
    ),
  );
});

test('optional template has full editing controls and leaving protects drafts on desktop and mobile', async ({
  page,
}) => {
  const before = await (await page.request.get('/api/subagents')).json();
  await page.goto('/subagents');
  await page.getByRole('link', { name: '使用只读研究者模板', exact: true }).click();
  const form = page.getByRole('form', { name: '创建子Agent', exact: true });
  await expect(form.getByLabel('稳定名称')).toHaveValue('researcher');
  expect(await (await page.request.get('/api/subagents')).json()).toEqual(before);
  await form.getByLabel('显示名称').fill('我的研究者');
  await form.getByLabel('角色提示').fill('保留我自己的角色提示。');
  await page.getByRole('link', { name: '工具与能力', exact: true }).click();
  const guard = page.getByRole('dialog', { name: '离开编辑页面？', exact: true });
  await expect(guard).toBeVisible();
  await guard.getByRole('button', { name: '继续编辑', exact: true }).click();
  await expect(form.getByLabel('角色提示')).toHaveValue('保留我自己的角色提示。');
  await page.evaluate(() => window.history.back());
  await expect(guard).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(guard).toHaveCount(0);
  await expect(page).toHaveURL(/\/subagents\/new\?template=researcher$/);
  await expect(form.getByLabel('角色提示')).toHaveValue('保留我自己的角色提示。');
  // 浏览器真正的刷新离页提示；取消刷新后输入必须仍在。
  const beforeUnload = page.waitForEvent('dialog');
  // 从页面发起刷新，直接核对浏览器 beforeunload 的取消行为。
  await page.evaluate(() => {
    setTimeout(() => window.location.reload(), 0);
  });
  const nativeDialog = await beforeUnload;
  expect(nativeDialog.type()).toBe('beforeunload');
  await nativeDialog.dismiss();
  await expect(form.getByLabel('角色提示')).toHaveValue('保留我自己的角色提示。');
  await page.setViewportSize({ width: 390, height: 844 });
  await form.getByRole('button', { name: '选择Skills' }).click();
  const picker = page.getByRole('dialog', { name: 'Skills', exact: true });
  await picker.getByRole('searchbox').fill('evidence-check');
  await expect(picker.getByRole('checkbox', { name: '证据核对', exact: true })).toBeChecked();
  await page.screenshot({ path: `${evidenceDir}/selector-mobile.png`, fullPage: false });
  await page.keyboard.press('Escape');
  await expect(picker).toHaveCount(0);
  await expect(form.getByRole('button', { name: '选择Skills' })).toBeFocused();
  await form.getByRole('checkbox', { name: '创建后启用', exact: true }).check();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.screenshot({ path: `${evidenceDir}/editor-mobile.png`, fullPage: true });
  const createdResponse = page.waitForResponse(
    (r) => r.url().endsWith('/api/subagents') && r.request().method() === 'POST',
  );
  await form.getByRole('button', { name: '创建子Agent', exact: true }).click();
  const created = await (await createdResponse).json();
  expect(created).toMatchObject({ name: 'researcher', enabled: true, builtin: false });
  const card = page.getByRole('region', { name: '我的研究者', exact: true });
  await expect(card.getByRole('link', { name: '编辑配置' })).toBeVisible();
  await page.screenshot({ path: `${evidenceDir}/list-mobile.png`, fullPage: true });
  await card.getByRole('link', { name: '编辑配置' }).click();
  const editor = page.getByRole('form', { name: '编辑 我的研究者' });
  await editor.getByLabel('角色提示').fill('不保存的修改');
  await editor.getByRole('button', { name: '取消', exact: true }).click();
  await guard.getByRole('button', { name: '放弃修改并离开' }).click();
  const saved = await (await page.request.get(`/api/subagents/${created.id}`)).json();
  expect(saved.system_prompt).toBe('保留我自己的角色提示。');
  await card.getByRole('button', { name: '停用', exact: true }).click();
  await expect(card).toContainText('已停用');
  await card.getByRole('button', { name: '删除', exact: true }).click();
  await page
    .getByRole('dialog', { name: '删除我的研究者？' })
    .getByRole('button', { name: '确认删除此角色' })
    .click();
  await expect(card).toHaveCount(0);
  await page.setViewportSize({ width: 1440, height: 960 });
  await expect(page.getByRole('link', { name: '使用只读研究者模板', exact: true })).toBeVisible();
  await page.screenshot({ path: `${evidenceDir}/list-desktop.png`, fullPage: true });
  expect((await (await page.request.get('/api/settings')).json()).model_calls_enabled).toBe(false);
});
