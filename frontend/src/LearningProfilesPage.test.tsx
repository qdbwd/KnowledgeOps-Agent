import '@testing-library/jest-dom/vitest';
import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { LearningProfilesPage } from './LearningProfilesPage';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it('keeps human-edited profile text when the displayed version conflicts', async () => {
  const profile = {
    id: 'global',
    revision: 2,
    content: '人工说明',
    task_id: null,
    enabled: true,
    source: { kind: 'manual' },
  };
  const writes: unknown[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url: string, options?: RequestInit) => {
      if (_url.includes('/effective'))
        return Response.json({ default: '默认职责', profiles: [profile], automatic: [] });
      if (options?.method === 'PATCH') {
        writes.push(JSON.parse(String(options.body)));
        return Response.json({ detail: '学习配置修订已变化，请刷新后重试' }, { status: 409 });
      }
      return Response.json(profile);
    }),
  );
  render(
    <MemoryRouter>
      <LearningProfilesPage />
    </MemoryRouter>,
  );
  const input = await screen.findByRole('textbox', { name: '人工配置正文' });
  await userEvent.clear(input);
  await userEvent.type(input, '新的人工说明');
  await userEvent.click(screen.getByRole('button', { name: '保存配置' }));
  expect(await screen.findByText('学习配置修订已变化，请刷新后重试')).toBeInTheDocument();
  expect(input).toHaveValue('新的人工说明');
  expect(writes).toEqual([{ expected_revision: 2, content: '新的人工说明' }]);
});

it('previews an imported Skill as text before applying its exact hash', async () => {
  const writes: { url: string; body: unknown }[] = [];
  const profile = {
    id: 'global',
    revision: 1,
    content: '',
    task_id: null,
    enabled: true,
    source: { kind: 'default' },
  };
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, options?: RequestInit) => {
      if (url.includes('/effective'))
        return Response.json({ default: '默认职责', profiles: [profile], automatic: [] });
      if (options?.method === 'POST') {
        writes.push({ url, body: JSON.parse(String(options.body)) });
        if (url.endsWith('import-preview'))
          return Response.json({
            id: 'preview',
            content: '先讲例子',
            sha256: 'fixed-hash',
            notice: '只导入文本',
          });
      }
      return Response.json(profile);
    }),
  );
  render(
    <MemoryRouter>
      <LearningProfilesPage />
    </MemoryRouter>,
  );
  const upload = await screen.findByLabelText('导入 Markdown / SKILL.md');
  const file = new File(['先讲例子'], 'SKILL.md', { type: 'text/markdown' });
  Object.defineProperty(file, 'text', { value: async () => '先讲例子' });
  await userEvent.upload(upload, file);
  const apply = await screen.findByRole('button', { name: '确认应用导入' });
  expect(writes).toHaveLength(1);
  expect(screen.getByText('先讲例子')).toBeInTheDocument();
  await userEvent.click(apply);
  expect(writes[1]).toEqual({
    url: '/api/learning-profiles/global/import-apply',
    body: { preview_id: 'preview', expected_hash: 'fixed-hash' },
  });
});
