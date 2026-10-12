import { test, expect } from '@playwright/test';

test.use({ actionTimeout: 15000 });

test('profile import preview, comparison, rollback, export and task reuse remain explicit', async ({
  page,
}) => {
  test.setTimeout(60000);
  const conversation = await (
    await page.request.post('/api/conversations', { data: { title: '配置浏览器检查' } })
  ).json();
  const create = async (title: string) =>
    (
      await page.request.post('/api/learning-tasks', {
        data: {
          conversation_id: conversation.id,
          request_key: title,
          title,
          goal: '解释并保留学习要求',
          mode: 'interactive',
        },
      })
    ).json();
  const source = await create('配置来源任务');
  const destination = await create('配置复用任务');
  const sourceRevision = await (
    await page.request.post(`/api/learning-tasks/${source.id}/configuration`, {
      data: {
        expected_revision: source.revision,
        changes: { examples: '只使用SQL事务例子' },
      },
    })
  ).json();
  await page.goto(`/learning-profiles?task_id=${destination.id}`);
  const panel = page
    .locator('section.card')
    .filter({ has: page.getByRole('heading', { name: '任务个性化配置', exact: true }) })
    .first();
  const content = panel.getByRole('textbox', { name: '人工配置正文', exact: true });
  await content.fill('人工设定：先给例子，再说明边界。');
  const saved = page.waitForResponse(
    (r) => r.url().includes('/api/learning-profiles/') && r.request().method() === 'PATCH',
  );
  await panel.getByRole('button', { name: '保存配置', exact: true }).click();
  const version = await (await saved).json();
  await expect(panel.getByText(new RegExp(`修订 ${version.revision}`))).toBeVisible();
  await panel.getByLabel('导入 Markdown / SKILL.md').setInputFiles({
    name: 'SKILL.md',
    mimeType: 'text/markdown',
    buffer: Buffer.from('---\nname: reader\n---\n# 读者设定\n关注失败案例。'),
  });
  await expect(panel.getByRole('heading', { name: '待应用的导入内容' })).toBeVisible();
  // 预览本身不改写人工部分。
  await expect(content).toHaveValue('人工设定：先给例子，再说明边界。');
  await panel.getByRole('button', { name: '确认应用导入', exact: true }).click();
  await expect(content).toHaveValue(/关注失败案例/);
  await panel.getByRole('button', { name: '比较与恢复版本', exact: true }).click();
  await panel
    .getByRole('combobox', { name: '比较历史版本', exact: true })
    .selectOption(String(version.revision));
  await expect(
    panel.getByRole('heading', { name: `历史修订 ${version.revision}`, exact: true }),
  ).toBeVisible();
  await panel
    .getByRole('button', { name: `恢复为修订 ${version.revision} 的内容`, exact: true })
    .click();
  await expect(content).toHaveValue('人工设定：先给例子，再说明边界。');
  const exported = await page.request.get(
    (await panel.getByRole('link', { name: '导出人工配置' }).getAttribute('href'))!,
  );
  expect(await exported.text()).toContain('人工设定：先给例子，再说明边界。');
  await panel.getByRole('button', { name: '停用配置', exact: true }).click();
  await expect(panel.getByRole('button', { name: '启用配置', exact: true })).toBeVisible();
  await page.reload();
  await expect(panel.getByRole('button', { name: '启用配置', exact: true })).toBeVisible();
  await panel.getByRole('button', { name: '启用配置', exact: true }).click();
  await page.getByText('比较、恢复或复用任务要求', { exact: true }).click();
  await page.getByRole('combobox', { name: '来源任务', exact: true }).selectOption(source.id);
  await page
    .getByRole('combobox', { name: '来源修订', exact: true })
    .selectOption(String(sourceRevision.revision));
  await expect(page.getByRole('cell', { name: '只使用SQL事务例子', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '应用已预览的任务要求', exact: true }).click();
  await expect(page.getByRole('textbox', { name: '例子要求', exact: true })).toHaveValue(
    '只使用SQL事务例子',
  );
  const task = await (await page.request.get(`/api/learning-tasks/${destination.id}`)).json();
  expect(task.status).toBe('draft');
  expect(task.auto_publish).toBe(false);
  expect(task.units).toEqual([]);
});

test('ordinary chat and interactive tasks keep one conversation and never schedule automatic study', async ({
  page,
}) => {
  test.setTimeout(60000);
  await page.goto('/');
  const send = async (message: string) => {
    await page.getByLabel('发送给 KnowledgeOps 的消息').fill(message);
    const accepted = page.waitForResponse(
      (r) =>
        /\/api\/conversations\/[^/]+\/messages$/.test(r.url()) && r.request().method() === 'POST',
    );
    await page.getByRole('button', { name: '发送', exact: true }).click();
    const run = await (await accepted).json();
    await expect(page.getByRole('article').getByText(message, { exact: true })).toBeVisible();
    const conversationId = page.url().split('/').at(-1)!;
    const response = await page.request.post(`/api/__offline/execute/${run.id}`, {
      timeout: 45000,
    });
    const result = await response.json();
    expect(result?.status, JSON.stringify(result)).toBe('completed');
    return conversationId;
  };
  const conversationId = await send('先做普通聊天，不建立学习任务。');
  expect(
    await (await page.request.get(`/api/learning-tasks?conversation_id=${conversationId}`)).json(),
  ).toEqual([]);
  const task = await (
    await page.request.post('/api/learning-tasks', {
      data: {
        conversation_id: conversationId,
        request_key: 'interactive',
        title: '交互学习兼容检查',
        goal: '了解事务',
        mode: 'interactive',
      },
    })
  ).json();
  await page.reload();
  await expect(page.getByText('交互学习兼容检查', { exact: true }).first()).toBeVisible();
  await send('本任务请用SQL例子。');
  await page.reload();
  await expect(
    page.getByRole('article').getByText('先做普通聊天，不建立学习任务。', { exact: true }),
  ).toBeVisible();
  await expect(page.getByText('本任务请用SQL例子。', { exact: true })).toBeVisible();
  const latest = await (await page.request.get(`/api/learning-tasks/${task.id}`)).json();
  expect(latest.configuration.examples.value).toBe('使用SQL例子');
  const runs = await (
    await page.request.get(`/api/runs/page?conversation_id=${conversationId}&roots_only=true`)
  ).json();
  expect(runs.items).toHaveLength(2);
  expect(
    runs.items.every(
      (r: { origin: string; status: string }) => r.origin === 'human' && r.status === 'completed',
    ),
  ).toBeTruthy();
});
