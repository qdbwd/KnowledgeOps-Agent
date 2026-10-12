import { test, expect } from '@playwright/test';

test.use({ actionTimeout: 15000 });

test('two units, genuine feedback, manual role document, review and pause resume', async ({
  page,
}, testInfo) => {
  test.setTimeout(120000);
  await page.goto('/learning');
  await page.getByLabel('任务标题', { exact: true }).fill('两单元离线演示');
  await page.getByLabel('想了解什么、用于什么').fill('理解事务并形成两份带边界说明的学习笔记');
  await page.getByRole('combobox', { name: /^模式/ }).selectOption('delegated');
  await page.getByRole('button', { name: '建立任务', exact: true }).click();
  await expect(page).toHaveURL(/\/learning\/.+/);
  const taskId = page.url().split('/').at(-1)!;
  for (let n = 1; n <= 2; n++) {
    await page.getByRole('button', { name: '添加知识单元', exact: true }).click();
    await page
      .getByRole('textbox', { name: '单元标题', exact: true })
      .nth(n - 1)
      .fill(`第${n}单元：事务${n}`);
    await page
      .getByRole('textbox', { name: '要解决的问题', exact: true })
      .nth(n - 1)
      .fill('说明原理、例子和失败边界');
  }
  await page.getByRole('button', { name: '保存要求和框架', exact: true }).click();
  await expect(page.getByText(/要求修订 2/)).toBeVisible();
  await expect(
    page.getByRole('checkbox', { name: '允许在已确认范围内通过审读后自动保存' }),
  ).toHaveCount(1);
  await page.getByLabel('允许在已确认范围内通过审读后自动保存').check();
  await page.getByRole('button', { name: '确认已保存的范围', exact: true }).click();
  await expect(page.getByRole('button', { name: '暂停任务', exact: true })).toBeEnabled();
  let task = await (await page.request.get(`/api/learning-tasks/${taskId}`)).json();
  const runs = async () =>
    (await (await page.request.get('/api/runs')).json()) as {
      id: string;
      origin: string;
      status: string;
      conversation_id: string;
    }[];
  const first = (await runs()).find(
    (r) => r.conversation_id === task.conversation_id && r.origin === 'automatic',
  )!;
  await page.getByRole('button', { name: '切换到此任务并进入对话', exact: true }).click();
  await page
    .getByLabel('发送给 KnowledgeOps 的消息')
    .fill('本任务请用SQL例子；以后解释先给具体例子。');
  const accepted = page.waitForResponse(
    (r) =>
      r.url().endsWith(`/api/conversations/${task.conversation_id}/messages`) &&
      r.request().method() === 'POST',
  );
  await page.getByRole('button', { name: '发送', exact: true }).click();
  const human = await (await accepted).json();
  expect(human.origin).toBe('human');
  await expect(
    page.getByText('本任务请用SQL例子；以后解释先给具体例子。', { exact: true }),
  ).toBeVisible();
  expect(await (await page.request.post(`/api/__offline/execute/${first.id}`)).json()).toBeNull();
  const humanResult = await (await page.request.post(`/api/__offline/execute/${human.id}`)).json();
  expect(humanResult.status).toBe('completed');
  task = await (await page.request.get(`/api/learning-tasks/${taskId}`)).json();
  expect(task.configuration.examples.value).toBe('使用SQL例子');
  const memories = await (await page.request.get('/api/memories')).json();
  expect(memories.some((m: { content: string }) => m.content === '解释先给具体例子')).toBeTruthy();
  await page.goto(`/learning/${taskId}`);
  await page.getByRole('button', { name: '暂停任务', exact: true }).click();
  await expect(page.getByRole('button', { name: '继续任务', exact: true })).toBeEnabled();
  expect(await (await page.request.post(`/api/__offline/execute/${first.id}`)).json()).toBeNull();
  await page.goto(`/learning-profiles?task_id=${taskId}`);
  const taskProfile = page
    .locator('section')
    .filter({ has: page.getByRole('heading', { name: '任务个性化配置', exact: true }) })
    .first();
  await taskProfile.getByLabel('人工配置正文').fill('每篇先说明用途，最后讨论失败边界。');
  const savedProfile = page.waitForResponse(
    (r) => r.url().includes('/api/learning-profiles/') && r.request().method() === 'PATCH',
  );
  await taskProfile.getByRole('button', { name: '保存配置', exact: true }).click();
  expect((await savedProfile).ok()).toBeTruthy();
  await page.reload();
  await expect(taskProfile.getByLabel('人工配置正文')).toHaveValue(
    '每篇先说明用途，最后讨论失败边界。',
  );
  await page.goto(`/learning/${taskId}`);
  await page.getByRole('button', { name: '继续任务', exact: true }).click();
  await expect(page.getByRole('button', { name: '暂停任务', exact: true })).toBeEnabled();
  const firstResult = await (
    await page.request.post(`/api/__offline/execute/${first.id}`, { timeout: 60000 })
  ).json();
  expect(firstResult.status, JSON.stringify(firstResult)).toBe('completed');
  await page.getByRole('button', { name: '暂停任务', exact: true }).click();
  await expect(page.getByRole('button', { name: '继续任务', exact: true })).toBeEnabled();
  const second = (await runs()).find(
    (r) =>
      r.id !== first.id && r.conversation_id === task.conversation_id && r.origin === 'automatic',
  )!;
  expect(second.status).toBe('interrupted');
  await page.getByRole('button', { name: '继续任务', exact: true }).click();
  await expect(page.getByRole('button', { name: '暂停任务', exact: true })).toBeEnabled();
  const finalResult = await (
    await page.request.post(`/api/__offline/execute/${second.id}`, { timeout: 60000 })
  ).json();
  expect(finalResult.status, JSON.stringify(finalResult)).toBe('completed');
  await page.reload();
  task = await (await page.request.get(`/api/learning-tasks/${taskId}`)).json();
  expect(task.status).toBe('completed');
  expect(
    task.units.every(
      (u: { status: string; result_version_id: string }) =>
        u.status === 'published' && u.result_version_id,
    ),
  ).toBeTruthy();
  await expect(page.getByRole('link', { name: '查看实际子任务' })).toHaveCount(4);
  const reports = await (
    await page.request.get(`/api/learning-tasks/${taskId}/collaboration`)
  ).json();
  expect(reports).toHaveLength(4);
  expect(
    reports.every(
      (r: {
        status: string;
        child_run_id: string;
        material_length: number;
        read_ranges: number[][];
      }) =>
        r.status === 'completed' &&
        r.child_run_id &&
        JSON.stringify(r.read_ranges) === JSON.stringify([[0, r.material_length]]),
    ),
  ).toBeTruthy();
  await page.screenshot({ path: '../artifacts/verification/learning-browser.png', fullPage: true });
  await testInfo.attach('two-unit-evidence', {
    body: JSON.stringify({ task, reports }, null, 2),
    contentType: 'application/json',
  });
});
