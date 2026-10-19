import '@testing-library/jest-dom/vitest';
import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
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
      if (url === '/api/memory-categories') return Response.json([]);
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
      if (url === '/api/memory-categories') return Response.json([]);
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
    vi.fn(async (url: string, options?: RequestInit) =>
      url === '/api/memory-categories'
        ? Response.json([])
        : options?.method === 'PATCH'
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

it('saves text with automatic classification by default and permits an explicit category', async () => {
  const calls: unknown[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, options?: RequestInit) => {
      if (url === '/api/memory-categories')
        return Response.json([{ id: 'coding', name: '编程', revision: 1, memory_count: 0 }]);
      if (options?.method === 'POST') {
        calls.push(JSON.parse(String(options.body)));
        return Response.json({ id: 'saved' });
      }
      return Response.json([]);
    }),
  );
  render(
    <MemoryRouter>
      <PreferencesPage />
    </MemoryRouter>,
  );
  const content = screen.getByLabelText('新增长期记忆');
  const choice = await screen.findByLabelText('类别（可选）');
  expect(choice).toHaveValue('');
  await userEvent.type(content, '请用 Python 举例');
  await userEvent.click(screen.getByRole('button', { name: '新增记忆' }));
  await waitFor(() => expect(content).toHaveValue(''));
  await userEvent.type(content, '说明示例的输入输出');
  await userEvent.selectOptions(choice, 'coding');
  await userEvent.click(screen.getByRole('button', { name: '新增记忆' }));
  expect(calls).toEqual([
    { content: '请用 Python 举例', display_category_id: null, scope: 'global', topic: '' },
    { content: '说明示例的输入输出', display_category_id: 'coding', scope: 'global', topic: '' },
  ]);
});

it('changes only the display category and never sends confirmation or semantic fields', async () => {
  let row = {
    id: 'm3',
    revision: 3,
    content: '后台任务应保持进度',
    scope: 'global',
    topic: '',
    status: 'active',
    category: 'goal',
    display_category_id: null as string | null,
    classification: { status: 'unclassified', source: 'model', error: 'model_disabled' },
    source: { kind: 'manual' },
  };
  const writes: unknown[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, options?: RequestInit) => {
      if (url === '/api/memory-categories')
        return Response.json([{ id: 'coding', name: '编程', revision: 1, memory_count: 0 }]);
      if (options?.method === 'PATCH') {
        const body = JSON.parse(String(options.body));
        writes.push(body);
        row = { ...row, display_category_id: body.display_category_id, revision: 4 };
        return Response.json(row);
      }
      return Response.json([row]);
    }),
  );
  render(
    <MemoryRouter>
      <PreferencesPage />
    </MemoryRouter>,
  );
  const field = await screen.findByLabelText('记忆内容 m3');
  const card = within(field.closest('section')!);
  expect(card.getByRole('button', { name: '保存修改' })).toBeDisabled();
  expect(card.getByText(/模型调用已关闭/)).toBeInTheDocument();
  await userEvent.selectOptions(card.getByLabelText('类别（可选）'), 'coding');
  expect(card.getByRole('button', { name: '停用' })).toBeDisabled();
  await userEvent.click(card.getByRole('button', { name: '保存修改' }));
  expect(writes).toEqual([{ expected_revision: 3, display_category_id: 'coding' }]);
  await waitFor(() => expect(card.getByRole('button', { name: '保存修改' })).toBeDisabled());
});

it('preserves a local draft when category management refreshes a newer memory revision', async () => {
  let row = {
    id: 'm4',
    revision: 1,
    content: '保留原文',
    scope: 'global',
    topic: '',
    status: 'active',
    display_category_id: null as string | null,
    source: { kind: 'manual' },
  };
  const writes: unknown[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, options?: RequestInit) => {
      if (url === '/api/memory-categories') {
        if (options?.method === 'POST') {
          row = { ...row, revision: 2, display_category_id: 'coding' };
          return Response.json({ id: 'coding' });
        }
        return Response.json([{ id: 'coding', name: '编程', revision: 1, memory_count: 1 }]);
      }
      if (options?.method === 'PATCH') {
        writes.push(JSON.parse(String(options.body)));
        return Response.json({ detail: '记忆修订已变化' }, { status: 409 });
      }
      return Response.json(url === '/api/memories/m4' ? row : [row]);
    }),
  );
  render(
    <MemoryRouter>
      <PreferencesPage />
    </MemoryRouter>,
  );
  const field = await screen.findByLabelText('记忆内容 m4');
  await userEvent.clear(field);
  await userEvent.type(field, '尚未保存的原文修改');
  await userEvent.click(screen.getByRole('button', { name: '管理类别' }));
  const manager = within(await screen.findByRole('dialog', { name: '管理记忆类别' }));
  await userEvent.type(manager.getByLabelText('新类别名称'), '新类别');
  await userEvent.click(manager.getByRole('button', { name: '新增类别' }));
  await waitFor(() => expect(manager.getByLabelText('新类别名称')).toHaveValue(''));
  await userEvent.click(manager.getByRole('button', { name: '关闭管理记忆类别' }));
  expect(field).toHaveValue('尚未保存的原文修改');
  expect(await screen.findByText(/服务器已有新修订/)).toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: '保存修改' }));
  expect(writes).toEqual([{ expected_revision: 1, content: '尚未保存的原文修改' }]);
  expect(await screen.findByText('记忆修订已变化')).toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: '放弃本地修改并重新加载' }));
  await waitFor(() => expect(field).toHaveValue('保留原文'));
});
