import { expect, test } from '@playwright/test';
import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';

test('manage a complete skill through preview, read, enable, update, export and removal', async ({
  page,
}, info) => {
  test.setTimeout(120000);
  const name = `browser-skill-${Date.now()}`;
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto('/skills');
  await expect(page.getByRole('heading', { name: 'Skills', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '导入技能', exact: true }).click();
  let dialog = page.getByRole('dialog', { name: '导入技能', exact: true });
  await dialog.getByLabel('选择 SKILL.md 或 ZIP').setInputFiles({
    name: 'SKILL.md',
    mimeType: 'text/markdown',
    buffer: Buffer.from('# 原始说明\n\n读取资料后解释，不执行脚本。'),
  });
  await dialog.getByRole('button', { name: '检查并预览' }).click();
  await expect(dialog.getByText('请补填名称和用途，然后重新检查。')).toBeVisible();
  await expect(dialog.getByRole('button', { name: '确认导入（停用）' })).toBeDisabled();
  await dialog.getByLabel('技能名称', { exact: true }).fill(name);
  await dialog.getByLabel('用途说明').fill('验证完整技能管理。');
  await dialog.getByRole('button', { name: '检查并预览' }).click();
  await dialog.getByRole('button', { name: '试读', exact: true }).click();
  await expect(dialog.getByRole('region', { name: '试读结果' })).toContainText('原始说明');
  await page.screenshot({ path: info.outputPath('skill-import-desktop.png') });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: info.outputPath('skill-import-mobile.png') });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await dialog.getByRole('button', { name: '确认导入（停用）' }).click();
  await expect(dialog).toHaveCount(0);
  let card = page.getByRole('article', { name, exact: true });
  await expect(card).toBeVisible();
  const toggle = card.getByRole('checkbox', { name: `启用技能：${name}` });
  await expect(toggle).not.toBeChecked();
  await toggle.click();
  await expect(toggle).toBeChecked();

  const temporary = await mkdtemp(path.join(tmpdir(), 'knowledgeops-browser-skill-'));
  try {
    const directory = path.join(temporary, name);
    await mkdir(path.join(directory, 'scripts'), { recursive: true });
    await mkdir(path.join(directory, 'assets'));
    await writeFile(
      path.join(directory, 'SKILL.md'),
      `---\nname: ${name}\ndescription: 完整文件包\n---\n# 更新说明\n\n读取 scripts/report.py。`,
    );
    await writeFile(
      path.join(directory, 'scripts/report.py'),
      '# 只供试读\nraise RuntimeError("import must not execute")',
    );
    await writeFile(path.join(directory, 'assets/input.bin'), Buffer.from([255, 0, 8]));
    await card.getByRole('button', { name: '更新', exact: true }).click();
    dialog = page.getByRole('dialog', { name: `更新技能：${name}`, exact: true });
    await dialog.getByLabel('选择完整文件夹').setInputFiles(directory);
    await dialog.getByRole('button', { name: '检查并预览' }).click();
    await expect(dialog.getByText(/完整文件清单 · 3 个文件/)).toBeVisible();
    await dialog.getByLabel('试读资源').selectOption('scripts/report.py');
    await dialog.getByRole('button', { name: '试读', exact: true }).click();
    await expect(dialog.getByRole('region', { name: '试读结果' })).toContainText(
      'import must not execute',
    );
    await dialog.getByRole('button', { name: '确认更新' }).click();
    await expect(dialog).toHaveCount(0);
    await expect(toggle).toBeChecked();
    await card.getByRole('button', { name: '查看详情' }).click();
    const detail = page.getByRole('dialog', { name, exact: true });
    await expect(detail.getByRole('heading', { name: '更新说明' })).toBeVisible();
    await expect(
      detail.getByRole('listitem').filter({ hasText: 'assets/input.bin' }),
    ).toContainText('3 字节');
    await detail.getByRole('button', { name: `关闭${name}`, exact: true }).click();
    const downloaded = page.waitForEvent('download');
    await card.getByRole('button', { name: '导出', exact: true }).click();
    expect((await downloaded).suggestedFilename()).toBe(`${name}.zip`);
    await toggle.click();
    const disabling = page.getByRole('dialog', { name: '停用技能', exact: true });
    await expect(disabling.getByText(/已经读取它的任务/)).toBeVisible();
    await disabling.getByRole('button', { name: '确认停用技能' }).click();
    await expect(toggle).not.toBeChecked();
    await card.getByRole('button', { name: '移除', exact: true }).click();
    dialog = page.getByRole('dialog', { name: '移除技能', exact: true });
    await page.screenshot({ path: info.outputPath('skill-remove-mobile.png') });
    await dialog.getByRole('button', { name: '确认移除技能' }).click();
    await expect(card).toHaveCount(0);
    await page.reload();
    card = page.getByRole('article', { name, exact: true });
    await expect(card).toHaveCount(0);
    expect(errors).toEqual([]);
  } finally {
    // 只清理由本用例创建的系统临时目录；与项目真实技能目录完全分开。
    await rm(temporary, { recursive: true, force: true });
  }
});

test('rejected updates preserve the installed package and cancelled previews stay inactive', async ({
  page,
  request,
}) => {
  test.setTimeout(60000);
  const name = `browser-guard-${Date.now()}`;
  const content = `---\nname: ${name}\ndescription: 原版本\n---\n# 已安装正文`;
  const staged = await (
    await request.post('/api/skills/imports', {
      multipart: {
        files: { name: 'SKILL.md', mimeType: 'text/markdown', buffer: Buffer.from(content) },
      },
    })
  ).json();
  expect((await request.post(`/api/skills/imports/${staged.id}/commit`)).ok()).toBe(true);
  await page.goto('/skills');
  const card = page.getByRole('article', { name, exact: true });
  await card.getByRole('button', { name: '更新', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: `更新技能：${name}`, exact: true });
  await dialog.getByLabel('选择 SKILL.md 或 ZIP').setInputFiles({
    name: 'broken.zip',
    mimeType: 'application/zip',
    buffer: Buffer.from('not an archive'),
  });
  await dialog.getByRole('button', { name: '检查并预览' }).click();
  await expect(dialog.getByRole('alert')).toContainText('ZIP');
  expect((await (await request.get(`/api/skills/${name}`)).json()).content).toContain('已安装正文');
  await dialog.getByLabel('选择 SKILL.md 或 ZIP').setInputFiles({
    name: 'SKILL.md',
    mimeType: 'text/markdown',
    buffer: Buffer.from(content.replace('已安装正文', '尚未提交正文')),
  });
  await dialog.getByRole('button', { name: '检查并预览' }).click();
  await expect(dialog.getByRole('button', { name: '确认更新' })).toBeEnabled();
  await dialog.getByRole('button', { name: '取消', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  expect((await (await request.get(`/api/skills/${name}`)).json()).content).toContain('已安装正文');
});
