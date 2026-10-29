import { expect, test } from '@playwright/test';

// APP-04 分层就绪与排队原因：页面只读展示事实，不领取、批准或恢复任务。
test('system page separates foundation readiness from worker readiness', async ({ page }, info) => {
  await page.goto('/system');
  await expect(page.getByRole('heading', { name: '系统与维护' })).toBeVisible();
  const foundation = page.getByRole('region', { name: '基础依赖' });
  await expect(foundation).toContainText('业务库');
  await expect(foundation).toContainText('数据库版本');
  // 浏览器验收服务器没有 Worker 进程：执行层必须如实显示未就绪，
  // 而不是因为 API 能响应就宣称整体可用。
  const execution = page.getByRole('region', { name: '执行就绪' });
  await expect(execution).toContainText(/Worker (未启动或已停止|空闲，可接单|正在执行任务)/);
  // 备份记录只读：列出本机命令产生的结果与状态，不提供下载或执行入口。
  const backups = page.getByRole('region', { name: '备份与恢复' });
  await expect(backups).toContainText('已校验并有隔离恢复');
  await expect(backups).toContainText('未完成');
  await expect(backups).toContainText('./scripts/backup.ps1');
  await expect(backups.getByRole('link')).toHaveCount(0);
  await page.screenshot({ path: info.outputPath('system-desktop.png') });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/system');
  await expect(page.getByRole('heading', { name: '系统与维护' })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: info.outputPath('system-mobile.png') });
});

test('queued run explains why it is waiting and links back to its original entry', async ({ page, request }, info) => {
  const chat = await (await request.post('/api/conversations', { data: { title: '排队原因验收' } })).json();
  const reply = await request.post(`/api/conversations/${chat.id}/messages`, {
    data: { content: '排队中的任务', request_key: crypto.randomUUID() },
  });
  expect(reply.status()).toBe(202);
  const run = await reply.json();

  await page.goto(`/runs/${run.id}`);
  const notice = page.locator('[role="status"]').filter({ hasText: '还在排队' });
  await expect(notice).toContainText('Worker 未就绪');
  await expect(notice.getByRole('link', { name: '前往处理入口 →' })).toHaveAttribute('href', `/runs/${run.id}`);
  await page.screenshot({ path: info.outputPath('run-blocking-desktop.png') });
});
