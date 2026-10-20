import { expect, test } from '@playwright/test';

test('task detail loads diagnostics on demand and independent files survive real detail cleanup', async ({
  page,
  request,
}, info) => {
  test.setTimeout(120000);
  const seeded = await (await request.post('/api/__offline/task-details-seed')).json();
  const recordRequests: string[] = [];
  const recordResponses: number[] = [];
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(message.text());
  });
  page.on('request', (req) => {
    if (req.url().includes(`/api/runs/${seeded.run_id}/records`)) recordRequests.push(req.url());
  });
  page.on('response', (res) => {
    if (res.url().includes(`/api/runs/${seeded.run_id}/records`))
      recordResponses.push(res.status());
  });
  await page.goto(`/runs/${seeded.run_id}`);
  await expect(page.getByRole('heading', { name: '任务详情', exact: true })).toBeVisible();
  await expect(page.getByRole('link', { name: seeded.delivery.name })).toBeVisible();
  await expect(page.getByText('笔记及草稿已经保存。', { exact: true })).toBeVisible();
  await expect(page.getByRole('link', { name: '独立保留的笔记草稿 →' })).toBeVisible();
  expect(recordRequests).toHaveLength(0);
  await expect(page.getByRole('heading', { name: /模型调用/ })).toHaveCount(0);
  await page.screenshot({ path: info.outputPath('task-detail-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: info.outputPath('task-detail-mobile.png'), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  );
  await page.getByRole('link', { name: '诊断', exact: true }).click();
  await expect(page).toHaveURL(`/runs/${seeded.run_id}/diagnostics`);
  await expect(page.getByRole('region', { name: '任务产出与活动' })).toHaveCount(0);
  await expect(page.getByRole('heading', { name: '模型调用 1 次' })).toBeVisible();
  // 开发态StrictMode会立即取消首次挂载请求；核对真正完成的响应和关闭后的新增请求。
  expect(recordResponses).toEqual([200]);
  const requestsAfterOpening = recordRequests.length;
  await page.getByText('#1 · agent · succeeded', { exact: false }).click();
  await expect(page.getByText(/只在诊断显示的完整请求/)).toBeVisible();
  await page.screenshot({ path: info.outputPath('task-diagnostic-mobile.png'), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  );
  await page.getByRole('link', { name: '返回任务详情' }).click();
  await expect(page.getByRole('region', { name: '任务产出与活动' })).toHaveCount(1);
  await expect(page.getByRole('region', { name: '任务诊断' })).toHaveCount(0);
  await page.reload();
  await expect(page.getByRole('link', { name: '诊断', exact: true })).toBeVisible();
  expect(recordRequests).toHaveLength(requestsAfterOpening);
  const expired = await request.post(`/api/__offline/task-details/${seeded.run_id}/expire`);
  expect(expired.ok()).toBe(true);
  expect(await expired.json()).toMatchObject({ status: 'completed' });
  await page.reload();
  await expect(page.getByRole('heading', { name: '运行结果摘要' })).toBeVisible();
  await expect(page.getByRole('link', { name: seeded.delivery.name })).toBeVisible();
  await expect(page.getByRole('link', { name: '诊断', exact: true })).toHaveCount(0);
  expect(recordRequests).toHaveLength(requestsAfterOpening);
  expect((await request.get(seeded.delivery.url)).status()).toBe(200);
  await page.getByRole('link', { name: seeded.delivery.name }).click();
  await expect(page.getByRole('heading', { name: '已交付正文', exact: true })).toBeVisible();
  expect(errors).toEqual([]);
});

test('diagnostic navigation never duplicates task activity or leaks it into chat', async ({
  page,
  request,
}, info) => {
  const seeded = await (await request.post('/api/__offline/task-details-seed')).json();
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(message.text());
  });
  const activity = page.getByRole('region', { name: '任务产出与活动' });
  const diagnostics = page.getByRole('region', { name: '任务诊断' });
  const chatPath = `/chat/${seeded.conversation_id}`;
  const taskPath = `/runs/${seeded.run_id}`;
  const diagnosticPath = `${taskPath}/diagnostics`;
  await page.goto(chatPath);
  await page.getByRole('link', { name: '任务详情 →', exact: true }).click();
  for (let cycle = 0; cycle < 3; cycle++) {
    await expect(activity).toHaveCount(1);
    await page.getByRole('link', { name: '诊断', exact: true }).click();
    await expect(page).toHaveURL(diagnosticPath);
    await expect(page.getByRole('heading', { name: '模型调用 1 次' })).toBeVisible();
    await expect(activity).toHaveCount(0);
    await page.getByRole('link', { name: '返回任务详情' }).click();
    await expect(page).toHaveURL(taskPath);
    await expect(activity).toHaveCount(1);
    await expect(diagnostics).toHaveCount(0);
  }
  await page.getByRole('link', { name: '回到对话 →', exact: true }).click();
  await expect(page).toHaveURL(chatPath);
  await expect(activity).toHaveCount(0);
  await expect(diagnostics).toHaveCount(0);
  await page.getByRole('link', { name: '任务详情 →', exact: true }).click();
  await page.getByRole('link', { name: '诊断', exact: true }).click();
  await expect(page.getByRole('heading', { name: '模型调用 1 次' })).toBeVisible();
  await page.goBack();
  await expect(activity).toHaveCount(1);
  await page.goForward();
  await expect(page.getByRole('heading', { name: '模型调用 1 次' })).toBeVisible();
  await expect(activity).toHaveCount(0);
  // 独立地址可以刷新，不依赖从详情页带入的临时状态。
  await page.reload();
  await expect(page.getByRole('heading', { name: '模型调用 1 次' })).toBeVisible();
  await page.screenshot({ path: info.outputPath('diagnostic-page-desktop.png'), fullPage: true });
  await page.getByRole('link', { name: '回到对话', exact: true }).click();
  await expect(page).toHaveURL(chatPath);
  await expect(activity).toHaveCount(0);
  await expect(diagnostics).toHaveCount(0);
  expect(errors).toEqual([]);
});
