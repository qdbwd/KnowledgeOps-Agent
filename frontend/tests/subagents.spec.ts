import { test, expect } from '@playwright/test';
import { writeFile } from 'node:fs/promises';

const evidenceDir = '/workspace/artifacts/verification/optimization';

test('subagent configuration persists revisions, conflict recovery and deleted history in the browser', async ({
  page,
}) => {
  const pageErrors: string[] = [];
  page.on('pageerror', (error) => pageErrors.push(error.message));
  const settings = await (await page.request.get('/api/settings')).json();
  expect(settings.model_calls_enabled).toBe(false);

  await page.goto('/');
  await page.getByRole('link', { name: '子Agent', exact: true }).click();
  // Built-in display name is owned by the API, so identify the immutable card by its stable name.
  const builtinName = await (await page.request.get('/api/subagents')).json();
  const builtinCard = page.getByRole('region', { name: builtinName[0].display_name, exact: true });
  await expect(builtinCard.getByText('researcher', { exact: true })).toBeVisible();
  await expect(builtinCard.getByRole('button', { name: '编辑配置' })).toHaveCount(0);
  const form = page.getByRole('form', { name: '创建子Agent', exact: true });
  await form.getByLabel('稳定名称', { exact: true }).fill('browser-reviewer');
  await form.getByLabel('显示名称', { exact: true }).fill('浏览器夹具核对员');
  await form.getByLabel('用途说明', { exact: true }).fill('仅浏览器验收配置，非模型运行');
  await form.getByLabel('角色提示', { exact: true }).fill('核对已分配材料，不扩展结论。');
  await expect(form.getByRole('checkbox', { name: /read_source / })).not.toBeChecked();
  await form.getByRole('checkbox', { name: /read_source / }).check();
  await form.getByRole('checkbox', { name: /evidence-check / }).check();
  await form.getByRole('checkbox', { name: '创建后启用', exact: true }).check();
  const createdResponse = page.waitForResponse(
    (response) =>
      response.url().endsWith('/api/subagents') && response.request().method() === 'POST',
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
  let card = page.getByRole('region', { name: '浏览器夹具核对员', exact: true });
  await expect(card.getByText(/已启用 · 修订 1/)).toBeVisible();
  await page.reload();
  await expect(card).toBeVisible();
  await card.getByRole('button', { name: '编辑配置', exact: true }).click();
  let editor = card.getByRole('form', { name: '编辑 浏览器夹具核对员', exact: true });
  await expect(editor.getByLabel('稳定名称')).toHaveAttribute('readonly', '');
  await editor.getByLabel('角色提示').fill('保存精确条件与引用，不扩展结论。');
  await editor.getByRole('button', { name: '保存配置', exact: true }).click();
  await expect(card.getByText(/已启用 · 修订 2/)).toBeVisible();
  await card.getByRole('button', { name: '编辑配置', exact: true }).click();
  editor = card.getByRole('form');
  await editor.getByLabel('角色提示').fill('这段本地输入必须在冲突后保留');
  const competitorHttp = await page.request.patch(`/api/subagents/${created.id}`, {
    data: { expected_revision: 2, description: '另一个窗口保存的用途说明' },
  });
  expect(competitorHttp.status()).toBe(200);
  const competitor = await competitorHttp.json();
  const conflictResponse = page.waitForResponse(
    (response) =>
      response.url().endsWith(`/api/subagents/${created.id}`) && response.status() === 409,
  );
  await editor.getByRole('button', { name: '保存配置', exact: true }).click();
  const conflict = await (await conflictResponse).json();
  await expect(card.getByRole('alert')).toContainText('修订已变化');
  await expect(editor.getByLabel('角色提示')).toHaveValue('这段本地输入必须在冲突后保留');
  await page.screenshot({ path: `${evidenceDir}/subagents-browser-conflict.png`, fullPage: true });
  await card.getByRole('button', { name: '放弃本地修改并刷新' }).click();
  await expect(card.getByText(/已启用 · 修订 3/)).toBeVisible();
  await card.getByRole('button', { name: '编辑配置', exact: true }).click();
  await expect(card.getByLabel('角色提示')).toHaveValue('保存精确条件与引用，不扩展结论。');
  await card.getByRole('button', { name: '停用', exact: true }).click();
  await expect(card.getByText(/已停用 · 修订 4/)).toBeVisible();
  await card.getByRole('button', { name: '查看历史', exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`history=${created.id}$`));
  await page.reload();
  let history = page.getByRole('region', { name: '配置历史', exact: true });
  await expect(history.getByRole('article')).toHaveCount(4);
  await expect(history.getByText('核对已分配材料，不扩展结论。', { exact: true })).toBeVisible();
  card = page.getByRole('region', { name: '浏览器夹具核对员', exact: true });
  await card.getByRole('button', { name: '删除', exact: true }).click();
  await expect(card.getByText(/确认删除「浏览器夹具核对员」（browser-reviewer）/)).toBeVisible();
  await card.getByRole('button', { name: '取消删除', exact: true }).click();
  expect(
    await (await page.request.get(`/api/subagents/${created.id}/history`)).json(),
  ).toHaveLength(4);
  await card.getByRole('button', { name: '删除', exact: true }).click();
  await card.getByRole('button', { name: '确认删除此角色', exact: true }).click();
  await expect(card).toHaveCount(0);
  await expect(history.getByRole('article')).toHaveCount(5);
  await page.reload();
  history = page.getByRole('region', { name: '配置历史', exact: true });
  await expect(history.getByText('browser-reviewer · 已删除', { exact: true })).toBeVisible();
  const revisions = await (await page.request.get(`/api/subagents/${created.id}/history`)).json();
  expect(revisions.map((row: { revision: number }) => row.revision)).toEqual([1, 2, 3, 4, 5]);
  expect(revisions.at(-1)).toMatchObject({ deleted: true, enabled: false });
  expect(revisions[3].authorization_epoch).toBeGreaterThan(created.authorization_epoch);
  expect((await (await page.request.get('/api/settings')).json()).model_calls_enabled).toBe(false);
  const remaining = await (await page.request.get('/api/subagents')).json();
  expect(remaining.map((row: { id: string }) => row.id).sort()).toEqual(
    builtinName.map((row: { id: string }) => row.id).sort(),
  );
  await page.screenshot({
    path: `${evidenceDir}/subagents-browser-deleted-history.png`,
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(history).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.screenshot({ path: `${evidenceDir}/subagents-browser-mobile.png`, fullPage: true });
  expect(pageErrors).toEqual([]);
  await writeFile(
    `${evidenceDir}/subagents-browser-crud.json`,
    JSON.stringify(
      {
        created,
        competitor,
        conflict,
        revisions,
        pageErrors,
        history_url: page.url(),
        viewport: '390x844',
      },
      null,
      2,
    ),
  );
});

test('chat defaults auxiliary off and persists the user-controlled delegation choice without executing a model', async ({
  page,
}) => {
  await page.goto('/');
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
  await expect(page.getByRole('checkbox', { name: '允许辅助模型', exact: true })).not.toBeChecked();
  await expect(page.locator('.message.user')).toHaveCount(1);
  const settings = await (await page.request.get('/api/settings')).json();
  expect(settings.model_calls_enabled).toBe(false);
  const saved = await (await page.request.get(`/api/runs/${first.id}`)).json();
  expect(saved.status).toBe('queued');
  expect(saved.tool_calls).toBe(0);
  expect(saved.input).toMatchObject({ auxiliary: false, delegation: false });
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
