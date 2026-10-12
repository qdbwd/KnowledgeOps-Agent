import '@testing-library/jest-dom/vitest';
import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { PreferencesPage } from './PreferencesPage';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it('confirms an inferred preference against its exact displayed revision', async () => {
  const calls: { url: string; body: unknown }[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, options?: RequestInit) => {
      const row = {
        id: 'p1',
        revision: 2,
        content: '先给例子',
        scope: 'global',
        topic: '',
        status: 'pending',
        source: { kind: 'inferred' },
      };
      if (options?.method === 'PATCH') {
        calls.push({ url, body: JSON.parse(String(options.body)) });
        return Response.json({ ...row, revision: 3, status: 'active' });
      }
      return Response.json([row]);
    }),
  );
  render(
    <MemoryRouter>
      <PreferencesPage />
    </MemoryRouter>,
  );
  await userEvent.click(await screen.findByRole('button', { name: '确认生效' }));
  expect(calls).toEqual([
    { url: '/api/memories/p1', body: { expected_revision: 2, status: 'active' } },
  ]);
});

it('shows genuine evidence and restores the chosen immutable revision', async () => {
  const writes: { url: string; body: unknown }[] = [];
  const row = {
    id: 'm1',
    revision: 3,
    content: '现在偏好例子',
    category: 'cognitive',
    scope: 'global',
    topic: '',
    status: 'active',
    source: { kind: 'manual' },
  };
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, options?: RequestInit) => {
      if (options?.method === 'POST') {
        writes.push({ url, body: JSON.parse(String(options.body)) });
        return Response.json(row);
      }
      if (url.endsWith('/history'))
        return Response.json([{ ...row, revision: 1, content: '以前先讲原理' }, row]);
      if (url === '/api/memories/m1')
        return Response.json({
          ...row,
          evidence: [
            {
              id: 'e1',
              quote: '我的习惯是先看例子',
              kind: 'explicit',
              context_key: 'conversation:c1',
            },
          ],
        });
      return Response.json([row]);
    }),
  );
  render(
    <MemoryRouter>
      <PreferencesPage />
    </MemoryRouter>,
  );
  await userEvent.click(await screen.findByRole('button', { name: '查看来源证据' }));
  expect(await screen.findByText(/^我的习惯是先看例子/)).toBeInTheDocument();
  expect(screen.getByRole('link', { name: '来源对话' })).toHaveAttribute('href', '/chat/c1');
  await userEvent.click(screen.getByRole('button', { name: '查看修订记录' }));
  await userEvent.click(await screen.findByRole('button', { name: '恢复修订 1' }));
  expect(writes).toEqual([
    { url: '/api/memories/m1/rollback', body: { expected_revision: 3, target_revision: 1 } },
  ]);
});

it('preserves the current form and displays a conflicting revision error', async () => {
  const row = {
    id: 'm2',
    revision: 4,
    content: '我是后端开发者',
    category: 'background',
    scope: 'global',
    topic: '',
    status: 'active',
    source: { kind: 'manual' },
  };
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url: string, options?: RequestInit) =>
      options?.method === 'PATCH'
        ? Response.json({ detail: '修订已变化，请刷新后重试' }, { status: 409 })
        : Response.json([row]),
    ),
  );
  render(
    <MemoryRouter>
      <PreferencesPage />
    </MemoryRouter>,
  );
  const input = await screen.findByRole('textbox', { name: '记忆内容 m2' });
  await userEvent.clear(input);
  await userEvent.type(input, '我现在做产品开发');
  await userEvent.click(screen.getByRole('button', { name: '保存修改' }));
  expect(await screen.findByText('修订已变化，请刷新后重试')).toBeInTheDocument();
  expect(input).toHaveValue('我现在做产品开发');
});
