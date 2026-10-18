import { test, expect } from '@playwright/test';

// 样式迁移回归：使用既有隔离 API，不创建运行，也不调用模型。
test('management pages retain usable controls and fit desktop and narrow viewports', async ({
  page,
}) => {
  test.setTimeout(60000);
  const routes = [
    '/knowledge',
    '/sources',
    '/changes',
    '/runs',
    '/memories',
    '/learning',
    '/learning-profiles',
    '/tools',
    '/subagents',
    '/mcp',
  ];
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 900 });
    for (const route of routes) {
      await page.goto(route);
      await expect(page.locator('main h1').first()).toBeVisible();
      await expect(
        page.locator('main [role="status"]').filter({ hasText: '正在加载' }),
      ).toHaveCount(0);
      expect(
        await page.evaluate(() => document.documentElement.scrollWidth),
        `${route}, ${width}px`,
      ).toBeLessThanOrEqual(width);
      if (['/sources', '/tools', '/learning', '/subagents'].includes(route)) {
        await page.screenshot({
          path: `../artifacts/verification/tailwind-${route.slice(1)}-${width}.png`,
        });
      }
    }
  }
  expect(errors).toEqual([]);
});

test('rich document typography and keyboard controls survive the CSS reset', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const response = await page.request.post('/api/sources/text', {
    data: {
      title: '样式回归材料',
      content:
        '# 正文标题\n\n普通段落与 **强调**。\n\n## 阅读层次\n\n- 第一条\n- 第二条\n\n1. 第一步\n2. 第二步\n\n> 一段引用\n\n| 名称 | 说明 |\n| --- | --- |\n| 工具 | 测试内容 |\n\n```python\nprint("保留源码")\n```',
    },
  });
  expect(response.status()).toBe(201);
  const source = await response.json();
  await page.goto(`/sources/${source.id}`);
  const prose = page.locator('.markdown');
  await expect(prose.getByRole('heading', { name: '正文标题' })).toBeVisible();
  await expect(prose.locator('ul')).toHaveCSS('list-style-type', 'disc');
  await expect(prose.locator('ol')).toHaveCSS('list-style-type', 'decimal');
  await expect(prose.locator('h2')).toHaveCSS('font-size', '21px');
  await expect(prose.locator('h1')).toHaveCSS('font-weight', '650');
  await expect(prose).toHaveCSS('line-height', '28.5px');
  await expect(prose.locator('td').first()).toHaveCSS('border-top-width', '1px');
  await expect(prose.locator('blockquote')).toHaveCSS('border-left-width', '3px');
  await page.screenshot({
    path: '../artifacts/verification/tailwind-document.png',
    fullPage: true,
  });
  await page.getByRole('button', { name: '查看源文本', exact: true }).click();
  await expect(page.locator('.document-code')).toContainText('# 正文标题');
  await page.getByRole('button', { name: '预览文档', exact: true }).click();
  await expect(prose.getByRole('heading', { name: '正文标题' })).toBeVisible();

  await page.goto('/');
  const menu = page.getByRole('button', { name: '更多设置与材料' });
  await menu.focus();
  await page.keyboard.press('Enter');
  await expect(page.getByRole('checkbox', { name: '允许辅助模型' })).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(menu).toBeFocused();
  await expect(page.getByRole('checkbox', { name: '允许辅助模型' })).not.toBeVisible();
  await expect(page.getByRole('button', { name: '发送', exact: true })).toBeDisabled();
  const chats = await (await page.request.get('/api/conversations')).json();
  await page.goto(`/chat/${chats[0].id}`);
  await expect(page.locator('.context-ring')).toHaveCSS('stroke-width', '2.4px');
  await expect(page.locator('.context-ring-track')).toHaveCSS('stroke-width', '2.4px');
});
