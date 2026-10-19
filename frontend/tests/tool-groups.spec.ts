import { expect, test } from '@playwright/test';
import { mkdir } from 'node:fs/promises';

const screenshots = '/workspace/artifacts/verification/tool-groups';

test('editable tool groups show real role impact, preserve explicit tools, and work in a narrow dialog', async ({
  page,
}) => {
  test.setTimeout(60000);
  await mkdir(screenshots, { recursive: true });
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('/tools');
  await page.getByRole('link', { name: '管理工具组' }).click();
  await expect(page.getByRole('heading', { name: '工具组', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '创建工具组', exact: true }).click();
  let editor = page.getByRole('dialog', { name: '创建工具组', exact: true });
  await editor.getByLabel('工具组名称', { exact: true }).fill('浏览器资料核对');
  await editor.getByLabel('用途说明', { exact: true }).fill('一起读取材料和知识正文。');
  await editor.getByRole('button', { name: '选择组内工具', exact: true }).click();
  let picker = page.getByRole('dialog', { name: '组内工具', exact: true });
  await picker.getByRole('searchbox').fill('read_source');
  await picker.getByRole('checkbox', { name: '读取材料正文', exact: true }).check();
  await picker.getByRole('button', { name: '查看读取材料正文详情' }).click();
  await expect(
    page
      .getByRole('dialog', { name: '读取材料正文', exact: true })
      .getByText('read_source', { exact: true }),
  ).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(picker.getByRole('checkbox', { name: '读取材料正文', exact: true })).toBeChecked();
  await picker.getByRole('button', { name: '完成', exact: true }).click();
  await editor.getByRole('button', { name: '保存工具组', exact: true }).click();
  const card = page.getByRole('article', { name: '浏览器资料核对', exact: true });
  await expect(card).toBeVisible();
  await page.screenshot({ path: `${screenshots}/groups-desktop.png`, fullPage: false });
  const groups = await (await page.request.get('/api/tools/groups')).json();
  const group = groups.find((row: { name: string }) => row.name === '浏览器资料核对');
  const roleResponse = await page.request.post('/api/subagents', {
    data: {
      name: 'group-browser-reader',
      display_name: '组配置阅读者',
      description: '验证工具组关联',
      system_prompt: '只读取已允许的材料。',
      tools: ['read_source'],
      tool_groups: [group.id],
      skills: [],
      enabled: true,
    },
  });
  expect(roleResponse.status()).toBe(201);
  const role = await roleResponse.json();
  const policy = await (await page.request.get('/api/tools/policy')).json();
  expect(
    (
      await page.request.patch('/api/tools/policy', {
        data: {
          expected_revision: policy.revision,
          main_tools: ['builtin:read_source'],
          main_tool_groups: [group.id],
        },
      })
    ).status(),
  ).toBe(200);

  await card.getByRole('button', { name: '编辑浏览器资料核对' }).click();
  editor = page.getByRole('dialog', { name: '编辑工具组', exact: true });
  await editor.getByRole('button', { name: '选择组内工具' }).click();
  picker = page.getByRole('dialog', { name: '组内工具', exact: true });
  await picker.getByRole('searchbox').fill('read_knowledge');
  await picker.getByRole('checkbox', { name: '读取知识正文', exact: true }).check();
  await picker.getByRole('button', { name: '完成', exact: true }).click();
  await expect(editor.getByRole('button', { name: '保存工具组', exact: true })).toBeDisabled();
  await editor.getByRole('button', { name: '预览影响', exact: true }).click();
  const impact = editor.getByRole('region', { name: '工具组修改影响' });
  await expect(impact.getByRole('heading', { name: '影响 2 个配置' })).toBeVisible();
  await expect(impact.getByText('组配置阅读者', { exact: true })).toBeVisible();
  await page.screenshot({ path: `${screenshots}/impact-desktop.png`, fullPage: false });
  // 另一窗口修改受影响角色。原预览不能替该新修订作决定。
  expect(
    (
      await page.request.patch(`/api/subagents/${role.id}`, {
        data: {
          expected_revision: role.revision,
          description: '另一窗口修改了说明',
        },
      })
    ).status(),
  ).toBe(200);
  await editor.getByRole('button', { name: '保存工具组', exact: true }).click();
  await expect(editor.getByRole('alert')).toContainText('影响已变化');
  await expect(editor.getByText('读取知识正文', { exact: true })).toBeVisible();
  await expect(editor.getByRole('button', { name: '保存工具组', exact: true })).toBeDisabled();
  await editor.getByRole('button', { name: '预览影响', exact: true }).click();
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(editor.getByRole('button', { name: '保存工具组', exact: true })).toBeInViewport();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  expect(await editor.evaluate((node) => node.scrollWidth)).toBeLessThanOrEqual(390);
  await page.screenshot({ path: `${screenshots}/impact-mobile.png`, fullPage: false });
  await editor.getByRole('button', { name: '保存工具组', exact: true }).click();
  await expect(editor).toHaveCount(0);
  expect(
    (await (await page.request.get('/api/subagents')).json()).find(
      (row: { id: string }) => row.id === role.id,
    ).resolved_tools,
  ).toEqual(['read_source', 'read_knowledge']);

  await card.getByRole('button', { name: '删除浏览器资料核对' }).click();
  editor = page.getByRole('dialog', { name: '删除工具组', exact: true });
  await editor.getByRole('button', { name: '预览影响', exact: true }).click();
  await expect(editor.getByText('移除：读取知识正文')).toHaveCount(2);
  await editor.getByRole('button', { name: '确认删除工具组', exact: true }).click();
  await expect(card).toHaveCount(0);
  const current = await (await page.request.get('/api/tools/policy')).json();
  expect(current.resolved_main_tools).toEqual(['builtin:read_source']);
  expect(current.main_tool_groups).toEqual([]);
  expect(
    (await (await page.request.get('/api/subagents')).json()).find(
      (row: { id: string }) => row.id === role.id,
    ).resolved_tools,
  ).toEqual(['read_source']);
  expect((await (await page.request.get('/api/settings')).json()).model_calls_enabled).toBe(false);
  expect(errors).toEqual([]);
});

test('closing an unsaved group keeps edits until explicitly discarded and keyboard stays inside modal', async ({
  page,
}) => {
  await page.goto('/tools/groups');
  await page.getByRole('button', { name: '创建工具组', exact: true }).click();
  const editor = page.getByRole('dialog', { name: '创建工具组', exact: true });
  await editor.getByLabel('工具组名称', { exact: true }).fill('尚未保存');
  await page.keyboard.press('Escape');
  await expect(editor.getByRole('alert')).toContainText('尚未保存的修改');
  await editor.getByRole('button', { name: '继续编辑', exact: true }).click();
  await expect(editor.getByLabel('工具组名称', { exact: true })).toHaveValue('尚未保存');
  for (let i = 0; i < 10; i++) {
    await page.keyboard.press('Tab');
    expect(await editor.evaluate((node) => node.contains(document.activeElement))).toBe(true);
  }
  await editor.getByRole('button', { name: '取消', exact: true }).click();
  await editor.getByRole('button', { name: '放弃修改', exact: true }).click();
  await expect(editor).toHaveCount(0);
  await expect(page.getByRole('button', { name: '创建工具组', exact: true })).toBeFocused();
  expect(
    (await (await page.request.get('/api/tools/groups')).json()).some(
      (row: { name: string }) => row.name === '尚未保存',
    ),
  ).toBe(false);
});
