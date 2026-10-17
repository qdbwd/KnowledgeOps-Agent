import '@testing-library/jest-dom/vitest';
import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { RunChildren } from './RunChildren';
import type { Run } from '../../shared/types/types';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const root = { id: 'root', root_id: 'root', child_count: 1, status: 'paused' } as Run;
const child = { task_id: 'child', run_id: 'child', agent_name: 'reader', task: '核对前置条件', status: 'paused',
  answer: '', error: null, revision: 0, last_event: 2, cancel_requested: false };

it('keeps a stable request identity after network failure and does not resume the group', async () => {
  const posts: { path: string; body: { request_key: string; message: string } }[] = [];
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (init?.method === 'POST') {
      posts.push({ path, body: JSON.parse(String(init.body)) });
      if (posts.length === 1) throw new Error('连接断开');
      return Response.json(child);
    }
    return Response.json(path.endsWith('/instructions') ? [] : [child]);
  }));
  render(<MemoryRouter><RunChildren run={root} refreshKey={1} onChanged={() => {}} /></MemoryRouter>);
  await userEvent.type(await screen.findByRole('textbox', { name: '给这个子任务补充要求' }), '说明适用范围');
  await userEvent.click(screen.getByRole('button', { name: '发送补充' }));
  expect(await screen.findByText('连接断开')).toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: '发送补充' }));
  expect(posts).toHaveLength(2);
  expect(posts[1]).toEqual(posts[0]);
  expect(posts[0].path).toBe('/api/runs/root/children/child/instructions');
  expect(posts[0].body.message).toBe('说明适用范围');
  expect(posts[0].body.request_key.length).toBeGreaterThan(10);
  expect(screen.getByText('补充会保存，整个任务仍保持暂停；继续后在安全点应用。')).toBeInTheDocument();
});

it('cancels only the selected child and reloads its saved result', async () => {
  let current = child;
  const posts: string[] = [];
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (init?.method === 'POST') {
      posts.push(path);
      current = { ...child, status: 'cancelled', answer: '已经完成的部分保留。', cancel_requested: true };
      return Response.json(current);
    }
    return Response.json(path.endsWith('/instructions') ? [] : [current]);
  }));
  render(<MemoryRouter><RunChildren run={{ ...root, status: 'waiting_children' }} refreshKey={1} onChanged={() => {}} /></MemoryRouter>);
  await userEvent.click(await screen.findByRole('button', { name: '取消这个子任务' }));
  expect(posts).toEqual(['/api/runs/root/children/child/cancel']);
  expect(await screen.findByText('已经完成的部分保留。')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '发送补充' })).not.toBeInTheDocument();
});

it('restores saved instruction provenance and does not offer actions on finished children', async () => {
  vi.stubGlobal('fetch', vi.fn(async (path: string) => Response.json(path.endsWith('/instructions')
    ? [{ id: 'i', content: '先解释必要的概念', origin: 'human', status: 'applied' }]
    : [{ ...child, status: 'completed', answer: '核对完成。' }])));
  render(<MemoryRouter><RunChildren run={{ ...root, status: 'completed' }} refreshKey={1} onChanged={() => {}} /></MemoryRouter>);
  await userEvent.click(await screen.findByText('已发送的补充要求'));
  expect(await screen.findByText('先解释必要的概念')).toBeInTheDocument();
  expect(screen.getByText('你 · 已应用')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '取消这个子任务' })).not.toBeInTheDocument();
});

it('offers cancel and instructions while a child is interrupted for human input', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => Response.json([
    { ...child, status: 'interrupted', unsettled: true, error: '等待真人确认' },
  ])));
  render(<MemoryRouter><RunChildren run={{ ...root, status: 'waiting_children' }} refreshKey={1} onChanged={() => {}} /></MemoryRouter>);
  expect(await screen.findByRole('button', { name: '取消这个子任务' })).toBeInTheDocument();
  expect(screen.getByRole('textbox', { name: '给这个子任务补充要求' })).toBeInTheDocument();
});
