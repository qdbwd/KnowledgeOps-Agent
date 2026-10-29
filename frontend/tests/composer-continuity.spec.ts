import { chromium, expect, test } from '@playwright/test';
import { mkdir } from 'node:fs/promises';
import type { Page } from '@playwright/test';

const input = (page: Page) => page.getByRole('textbox', { name: '发送给 KnowledgeOps 的消息' });
const saved = (page: Page) => page.getByText('草稿已保存到本浏览器', { exact: true });

test('a real browser profile restores a new draft after closing and restarting the browser', async ({ browserName }, info) => {
  expect(browserName).toBe('chromium');
  const profile = info.outputPath('browser-profile');
  await mkdir(profile, { recursive: true });
  let context = await chromium.launchPersistentContext(profile, { baseURL: 'http://127.0.0.1:4173', headless: true });
  try {
    let page = context.pages()[0];
    await page.goto('/');
    await input(page).fill('关闭浏览器后仍要保留这段思路');
    await page.getByRole('button', { name: '更多设置与材料', exact: true }).click();
    await page.getByRole('checkbox', { name: '允许辅助模型' }).check();
    await expect(saved(page)).toBeVisible();
    const identity = new URL(page.url()).searchParams.get('draft');
    expect(identity).toBeTruthy();
    await context.close();
    context = await chromium.launchPersistentContext(profile, { baseURL: 'http://127.0.0.1:4173', headless: true });
    page = context.pages()[0];
    await page.goto('/');
    await expect(input(page)).toHaveValue('关闭浏览器后仍要保留这段思路');
    expect(new URL(page.url()).searchParams.get('draft')).toBe(identity);
    await page.getByRole('button', { name: '更多设置与材料', exact: true }).click();
    await expect(page.getByRole('checkbox', { name: '允许辅助模型' })).toBeChecked();
    await page.getByRole('button', { name: '更多设置与材料', exact: true }).click();
    await page.screenshot({ path: info.outputPath('restored-draft.png') });
    await page.getByRole('button', { name: '放弃草稿', exact: true }).click();
    await page.getByRole('button', { name: '确认放弃草稿', exact: true }).click();
    await expect(input(page)).toHaveValue('');
    await page.reload();
    await expect(input(page)).toHaveValue('');
  } finally { await context.close(); }
});

test('a lost send response is reconciled after reload without resending or losing the next draft', async ({ page, request }, info) => {
  const chat = await (await request.post('/api/conversations', { data: { title: '回执浏览器验收' } })).json();
  let postCount = 0;
  await page.route(`**/api/conversations/${chat.id}/messages`, async (route) => {
    if (route.request().method() !== 'POST') return route.continue();
    postCount += 1;
    await route.fetch(); // 后端已真实接受，故意丢失给浏览器的响应。
    await route.abort('failed');
  });
  await page.goto(`/chat/${chat.id}`);
  await input(page).fill('这一条只能提交一次');
  await page.getByRole('button', { name: '发送', exact: true }).click();
  await expect(page.getByText('上次提交结果待核对', { exact: true })).toBeVisible();
  await input(page).fill('这是之后编辑的另一条消息');
  await expect(saved(page)).toBeVisible();
  await page.reload();
  await expect(page.getByRole('region', { name: '待核对的提交' })).toHaveCount(0);
  await expect(input(page)).toHaveValue('这是之后编辑的另一条消息');
  await expect(page.getByRole('article').filter({ hasText: '这一条只能提交一次' })).toHaveCount(1);
  expect(postCount).toBe(1);
  const messages = await (await request.get(`/api/conversations/${chat.id}/messages`)).json();
  expect(messages.filter((row: { role: string }) => row.role === 'user')).toHaveLength(1);
  await page.screenshot({ path: info.outputPath('reconciled-message.png') });
});

test('lost creation response keeps one identity and waits for explicit continuation before sending', async ({ page, request }) => {
  let createdId = '';
  let createCount = 0;
  let sends = 0;
  await page.route('**/api/conversations', async (route) => {
    if (route.request().method() !== 'POST') return route.continue();
    createCount += 1;
    const response = await route.fetch();
    createdId = (await response.json()).id;
    await route.abort('failed');
  });
  page.on('request', (event) => { if (event.method() === 'POST' && event.url().endsWith('/messages')) sends += 1; });
  await page.goto('/');
  await input(page).fill('创建回执也需要保护');
  await page.getByRole('button', { name: '发送', exact: true }).click();
  await expect(page.getByText('上次提交结果待核对', { exact: true })).toBeVisible();
  expect(createdId).toBeTruthy();
  await page.reload();
  await expect(page.getByRole('button', { name: '继续原提交' })).toBeVisible();
  expect(sends).toBe(0);
  expect((await (await request.get('/api/conversations')).json()).some((row: { id: string }) => row.id === createdId)).toBe(false);
  await page.getByRole('button', { name: '继续原提交' }).click();
  await expect(page).toHaveURL(`/chat/${createdId}`);
  await expect(input(page)).toHaveValue('');
  expect(createCount).toBe(1);
  expect(sends).toBe(1);
});

test('stale tabs keep both draft versions and explicit choice resolves them', async ({ page, context, request }, info) => {
  const chat = await (await request.post('/api/conversations', { data: { title: '多标签草稿验收' } })).json();
  await page.goto(`/chat/${chat.id}`);
  await input(page).fill('共同起点');
  await expect(saved(page)).toBeVisible();
  const other = await context.newPage();
  // 注入通知延迟：两个页面仍共用真实 IndexedDB，不能只靠通知避免竞争。
  await other.addInitScript(() => {
    Object.defineProperty(window, 'BroadcastChannel', { value: undefined, configurable: true });
    window.addEventListener('storage', (event) => event.stopImmediatePropagation(), { capture: true });
  });
  await other.goto(`/chat/${chat.id}`);
  await expect(input(other)).toHaveValue('共同起点');
  await input(page).fill('第一标签页的修改');
  await expect(saved(page)).toBeVisible();
  await input(other).fill('第二标签页的修改');
  await expect(other.getByText('草稿存在不同版本，已分别保留')).toBeVisible();
  await expect(other.getByText('第一标签页的修改', { exact: true })).toBeVisible();
  await expect(other.getByText('第二标签页的修改', { exact: true }).first()).toBeVisible();
  await other.setViewportSize({ width: 390, height: 844 });
  await other.screenshot({ path: info.outputPath('draft-conflict-mobile.png') });
  expect(await other.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await other.getByRole('button', { name: '使用版本 2，舍弃其他版本' }).click();
  await other.reload();
  await expect(input(other)).toHaveValue('第二标签页的修改');
  await expect(other.getByText('草稿存在不同版本，已分别保留')).toHaveCount(0);
});

test('a storage failure keeps in-memory input and blocks sending until saving recovers', async ({ page }) => {
  let posts = 0;
  page.on('request', (event) => { if (event.method() === 'POST') posts += 1; });
  await page.addInitScript(() => {
    const original = window.indexedDB;
    Object.assign(window, { restoreDraftStorage: () => Object.defineProperty(window, 'indexedDB', { value: original, configurable: true }) });
    Object.defineProperty(window, 'indexedDB', { configurable: true, get() { throw new DOMException('storage disabled', 'SecurityError'); } });
  });
  await page.goto('/');
  await input(page).fill('存储失败也不能丢失输入');
  await expect(page.getByRole('button', { name: '发送', exact: true })).toBeDisabled();
  await expect(page.getByText(/草稿尚未保存，当前输入仍在本页/)).toBeVisible();
  expect(posts).toBe(0);
  await page.evaluate(() => (window as unknown as { restoreDraftStorage: () => void }).restoreDraftStorage());
  await page.getByRole('button', { name: '重试保存草稿' }).click();
  await expect(saved(page)).toBeVisible();
  await expect(input(page)).toHaveValue('存储失败也不能丢失输入');
  await expect(page.getByRole('button', { name: '发送', exact: true })).toBeEnabled();
  expect(posts).toBe(0);
});

test('deleting a conversation clears the other tab and cannot resurrect its draft on reload', async ({ page, context, request }) => {
  const chat = await (await request.post('/api/conversations', { data: { title: '草稿删除验收' } })).json();
  await page.goto(`/chat/${chat.id}`);
  await input(page).fill('即将删除的未发送输入');
  await expect(saved(page)).toBeVisible();
  const other = await context.newPage();
  await other.goto(`/chat/${chat.id}`);
  await expect(input(other)).toHaveValue('即将删除的未发送输入');
  await request.post('/api/__offline/assets-ready');
  await page.getByRole('button', { name: '删除会话', exact: true }).click();
  await page.getByRole('button', { name: '确认删除会话', exact: true }).click();
  await expect(input(other)).toHaveValue('');
  await expect(input(other)).toBeDisabled();
  await other.reload();
  await expect(input(other)).toHaveValue('');
  await expect(input(other)).toBeDisabled();
});
