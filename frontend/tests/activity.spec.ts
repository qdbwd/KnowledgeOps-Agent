import { expect, test } from '@playwright/test';
import type { APIRequestContext } from '@playwright/test';

// APP-02 跨页面活动、独立已读与后台失败入口。
// 数据由 tests/browser_server.py 的离线种子直接写入隔离 schema；不启动模型、不投递任务。
type Seed = {
  token: string;
  approval: string;
  input: string;
  unknown: string;
  active: string;
  unread: string;
  history: string;
  aggregate: string;
};

async function seedActivity(request: APIRequestContext): Promise<Seed> {
  const response = await request.post('/api/__offline/application-activity-seed');
  expect(response.ok()).toBe(true);
  return (await response.json()) as Seed;
}

test('attention reasons cover the whole task group and management failures keep their original entry', async ({ page, request }, info) => {
  const seed = await seedActivity(request);
  await page.goto('/runs?view=attention');

  // 待批准任务从全局列表可直达原任务详情，而不是新建一套处理流程。
  const approval = page.getByRole('article').filter({ hasText: `approval-${seed.token}` });
  await expect(approval).toBeVisible();
  await expect(approval.getByText('等待批准')).toBeVisible();
  await expect(approval.getByRole('link', { name: `approval-${seed.token}` }))
    .toHaveAttribute('href', `/runs/${seed.approval}`);

  // 根任务为待批准，但"执行结果待核对"只发生在最早一个子任务上：
  // 页面只展示最近三个子摘要时，整组原因仍必须出现（回归此前漏原因的修正）。
  const group = page.getByRole('article').filter({ hasText: `aggregate-${seed.token}` });
  await expect(group.getByText('执行结果待核对')).toBeVisible();
  await expect(group.getByText(`aggregate_child_2-${seed.token}`)).toHaveCount(0);
  await expect(group.getByText('查看全部 5 个子任务 →')).toBeVisible();

  // 后台删除失败展示原作业，并链接回会话管理入口；不在此页自动重试。
  const management = page.getByRole('region', { name: '其他后台操作' });
  await expect(management).toContainText(`后台删除未完成-${seed.token}`);
  // 同类提示可能同时有 MCP 和删除两条，按链接目标区分，避免误点另一入口。
  await management.locator('a[href*="deletion="]').first().click();
  await expect(page).toHaveURL(/\?deletion=/);
  await page.screenshot({ path: info.outputPath('activity-attention-desktop.png') });
});

test('late read confirmation cannot clear a newer result and marking read leaves execution untouched', async ({ page, request }) => {
  const seed = await seedActivity(request);

  // 旧标签页把已读推进到自己看到的版本 1，但结果已是版本 3，不能被清除。
  const stale = await request.post(`/api/activity/runs/${seed.unread}/read`, { data: { version: 1 } });
  expect(stale.ok()).toBe(true);
  const stillUnread = await (await request.get('/api/activity/runs?view=unread&limit=100')).json();
  expect(stillUnread.items.map((item: { id: string }) => item.id)).toContain(seed.unread);

  await page.goto('/runs?view=unread');
  const card = page.getByRole('article').filter({ hasText: `unread-${seed.token}` });
  await expect(card).toBeVisible();
  await expect(card.getByText('新结果')).toBeVisible();
  await card.getByRole('button', { name: '标为已读' }).click();
  await expect(card).toHaveCount(0);

  // 已读只推进展示回执：运行状态、结果内容和任务入口都不变。
  const runs = await (await request.get('/api/activity/runs?view=all&limit=100')).json();
  const row = runs.items.find((item: { id: string }) => item.id === seed.unread);
  expect(row.status).toBe('completed');
  expect(row.unread).toBe(false);
  expect(row.id).toBe(seed.unread);
});

test('sidebar activity changes without navigation while another page stays open', async ({ page, request }, info) => {
  const seed = await seedActivity(request);
  await page.goto('/knowledge');
  const indicator = page.locator('[aria-label*="项待处理"]');
  await expect(indicator).toBeVisible();
  const before = await indicator.getAttribute('aria-label');

  // 只推进后台任务状态：页面不跳转，5 秒有界轮询应自行更新计数。
  const completed = await request.post(`/api/__offline/application-activity-complete/${seed.active}`);
  expect(completed.ok()).toBe(true);
  await expect(indicator).not.toHaveAttribute('aria-label', before!, { timeout: 15000 });
  await expect(indicator).toHaveAttribute('aria-label', /项新结果/);
  await page.screenshot({ path: info.outputPath('activity-sidebar-desktop.png') });
});

test('activity list stays readable at 390px width', async ({ page, request }, info) => {
  const seed = await seedActivity(request);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/runs?view=all');
  const card = page.getByRole('article').filter({ hasText: `aggregate-${seed.token}` });
  await expect(card).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: info.outputPath('activity-mobile.png') });
});
