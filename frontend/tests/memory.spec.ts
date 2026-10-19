import { expect, test } from '@playwright/test';

test('optional memory categories support manual management and retain text after category removal', async ({
  page,
}) => {
  await page.request.post('/api/__offline/memory-classifier/disabled');
  await page.goto('/preferences');
  const entry = page.getByLabel('新增长期记忆');
  const form = page.locator('form').filter({ has: entry });
  await expect(form.getByLabel('类别（可选）')).toHaveValue('');
  await entry.fill('我希望示例先说明 Python 输入和输出');
  const creation = page.waitForResponse(
    (r) => r.url().endsWith('/api/memories') && r.request().method() === 'POST',
  );
  await form.getByRole('button', { name: '新增记忆', exact: true }).click();
  const original = await (await creation).json();
  const field = page.getByLabel(`记忆内容 ${original.id}`, { exact: true });
  const card = page.locator('section.card').filter({ has: field });
  await expect(card.getByText('未分类 · 已生效', { exact: true })).toBeVisible();
  await expect(card.getByText(/模型调用已关闭，原文已保存/)).toBeVisible();
  await page.getByRole('button', { name: '管理类别', exact: true }).click();
  const manager = page.getByRole('dialog', { name: '管理记忆类别', exact: true });
  await manager.getByLabel('新类别名称').fill('代码讲解');
  await manager.getByRole('button', { name: '新增类别', exact: true }).click();
  await expect(manager.getByLabel('类别名称 代码讲解', { exact: true })).toBeVisible();
  await page.screenshot({ path: '../artifacts/verification/memory/categories-desktop.png' });
  await manager.getByRole('button', { name: '关闭管理记忆类别', exact: true }).click();
  await card.getByLabel('类别（可选）').selectOption({ label: '代码讲解' });
  await card.getByRole('button', { name: '保存修改', exact: true }).click();
  await expect(card.getByText('代码讲解 · 已生效', { exact: true })).toBeVisible();
  await page.reload();
  await expect(field).toHaveValue(original.content);
  await page.getByRole('button', { name: '管理类别', exact: true }).click();
  const category = manager
    .locator('form')
    .filter({ has: page.getByLabel('类别名称 代码讲解', { exact: true }) });
  await category.getByLabel('类别名称 代码讲解', { exact: true }).fill('技术讲解');
  await category.getByRole('button', { name: '保存名称', exact: true }).click();
  const renamed = manager
    .locator('form')
    .filter({ has: page.getByLabel('类别名称 技术讲解', { exact: true }) });
  await expect(renamed.getByText('1 条记忆')).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: '../artifacts/verification/memory/categories-mobile.png' });
  await renamed.getByRole('button', { name: '删除类别', exact: true }).click();
  const confirm = page.getByRole('dialog', { name: '删除类别“技术讲解”？', exact: true });
  await expect(confirm.getByText(/1 条记忆将移到“未分类”/)).toBeVisible();
  await confirm.getByRole('button', { name: '删除类别并保留记忆', exact: true }).click();
  await expect(confirm).toHaveCount(0);
  await expect(manager.getByLabel('类别名称 技术讲解', { exact: true })).toHaveCount(0);
  await manager.getByRole('button', { name: '关闭管理记忆类别', exact: true }).click();
  await expect(field).toHaveValue(original.content);
  await expect(card.getByText('未分类 · 已生效', { exact: true })).toBeVisible();
  await card.getByRole('button', { name: '查看修订记录', exact: true }).click();
  await expect(card.getByText(new RegExp(`修订 1 · 已生效 · ${original.content}`))).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({
    path: '../artifacts/verification/memory/memory-mobile.png',
    fullPage: true,
  });
});

test('background classification updates automatically, preserves drafts and offers an explicit retry after failure', async ({
  page,
}) => {
  await page.request.post('/api/__offline/memory-classifier/invalid');
  try {
    await page.goto('/preferences');
    await page.getByLabel('新增长期记忆').fill('我正在学习 TypeScript 和 Python 的差异');
    const creation = page.waitForResponse(
      (r) => r.url().endsWith('/api/memories') && r.request().method() === 'POST',
    );
    await page.getByRole('button', { name: '新增记忆', exact: true }).click();
    const original = await (await creation).json();
    const field = page.getByLabel(`记忆内容 ${original.id}`, { exact: true });
    const card = page.locator('section.card').filter({ has: field });
    await expect(card.getByRole('status')).toContainText('原文已保存，正在等待分类结果…');
    const failed = await page.request.post(
      `/api/__offline/memory-worker/${original.classification.run_id}`,
    );
    expect((await failed.json()).status).toBe('failed');
    await expect(card.getByText('分类结果无效，原文已保存。')).toBeVisible({ timeout: 10000 });
    await page.request.post('/api/__offline/memory-classifier/success');
    const retry = page.waitForResponse((r) =>
      r.url().endsWith(`/api/memories/${original.id}/classify`),
    );
    await card.getByRole('button', { name: '重新分类', exact: true }).click();
    const pending = await (await retry).json();
    await field.fill('我正在写尚未保存的本地修改');
    const applied = await page.request.post(
      `/api/__offline/memory-worker/${pending.classification.run_id}`,
    );
    expect((await applied.json()).status).toBe('completed');
    await expect(card.getByText('编程学习 · 已生效', { exact: true })).toBeVisible({
      timeout: 10000,
    });
    await expect(field).toHaveValue('我正在写尚未保存的本地修改');
    await expect(card.getByText(/服务器已有新修订/)).toBeVisible();
    await card.getByRole('button', { name: '保存修改', exact: true }).click();
    await expect(card.getByText(/修订已变化，请刷新后再操作/, { exact: false })).toBeVisible();
    await card.getByRole('button', { name: '放弃本地修改并重新加载', exact: true }).click();
    await expect(field).toHaveValue(original.content);
    await expect(card.getByLabel('类别（可选）')).toHaveValue(
      (await applied.json()).outcome.category_id,
    );
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({
      path: '../artifacts/verification/memory/classification-desktop.png',
      fullPage: true,
    });
    await page.reload();
    await expect(card.getByText('编程学习 · 已生效', { exact: true })).toBeVisible();
  } finally {
    await page.request.post('/api/__offline/memory-classifier/disabled');
  }
});

test('a queued memory classification can pause and continue through the shared task controls', async ({
  page,
}) => {
  await page.request.post('/api/__offline/memory-classifier/success');
  try {
    await page.goto('/preferences');
    await page.getByLabel('新增长期记忆').fill('我正在学习任务生命周期');
    const creation = page.waitForResponse(
      (r) => r.url().endsWith('/api/memories') && r.request().method() === 'POST',
    );
    await page.getByRole('button', { name: '新增记忆', exact: true }).click();
    const memory = await (await creation).json();
    const field = page.getByLabel(`记忆内容 ${memory.id}`, { exact: true });
    const card = page.locator('section.card').filter({ has: field });
    await card.getByRole('link', { name: '查看分类任务', exact: true }).click();
    await page.getByRole('button', { name: '暂停任务', exact: true }).click();
    await expect(page.getByRole('button', { name: '继续任务', exact: true })).toBeVisible();
    await page.goto('/preferences');
    await card.getByRole('link', { name: '分类已暂停，查看任务', exact: true }).click();
    const resumed = page.waitForResponse((r) =>
      r.url().endsWith(`/api/runs/${memory.classification.run_id}/resume`),
    );
    await page.getByRole('button', { name: '继续任务', exact: true }).click();
    expect((await resumed).status()).toBe(202);
    const finished = await page.request.post(
      `/api/__offline/memory-worker/${memory.classification.run_id}`,
    );
    expect((await finished.json()).status).toBe('completed');
    await page.goto('/preferences');
    await expect(card.getByText('编程学习 · 已生效', { exact: true })).toBeVisible();
    await expect(field).toHaveValue(memory.content);
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({ path: '../artifacts/verification/memory/memory-desktop.png' });
  } finally {
    await page.request.post('/api/__offline/memory-classifier/disabled');
  }
});
