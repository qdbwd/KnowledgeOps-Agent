import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';
import { ApplicationActivity } from './ApplicationActivity';
import { ActivityRunsPage } from './ActivityRunsPage';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const run = { id: 'root', parent_id: null, conversation_id: 'chat', task: '等待核对的任务', kind: 'agent', status: 'interrupted',
  created_at: '2026-10-24T00:00:00Z', href: '/runs/root', answer: '已经保存的结果一', result_version: 1,
  unread: true, attention: true, attention_reasons: ['unknown'], children: [], child_count: 0 };

it('acknowledges only the displayed version and leaves a newer result unread without recovering execution', async () => {
  let latest = run;
  const requests = vi.fn(async (url: string, options?: RequestInit) => {
    if (options?.method === 'POST') {
      latest = { ...run, result_version: 2, answer: '请求途中到达的结果二' };
      return Response.json({ unread: true });
    }
    if (url.startsWith('/api/activity?')) return Response.json({ revision: latest.result_version, checked_at: '',
      counts: { all: 1, attention: 1, active: 0, unread: 1 }, conversations: {}, management: { attention: 0, items: [], truncated: false } });
    if (url.startsWith('/api/activity/runs')) return Response.json({ items: [latest], next_cursor: null });
    if (url.startsWith('/api/asset-deletions')) return Response.json([]);
    throw new Error(`unexpected request ${url}`);
  });
  vi.stubGlobal('fetch', requests);
  render(<MemoryRouter><ApplicationActivity conversationIds={[]} version={0}><ActivityRunsPage /></ApplicationActivity></MemoryRouter>);
  await screen.findByText('已经保存的结果一');
  expect(requests.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(0);
  await userEvent.click(screen.getByRole('button', { name: '标为已读' }));
  await screen.findByText('请求途中到达的结果二');
  const writes = requests.mock.calls.filter(([, options]) => options?.method === 'POST');
  expect(writes).toHaveLength(1);
  expect(writes[0][0]).toBe('/api/activity/runs/root/read');
  expect(writes[0][1]?.body).toBe('{"version":1}');
  expect(screen.getByRole('button', { name: '标为已读' })).toBeInTheDocument();
  expect(screen.getByText('执行结果待核对')).toBeInTheDocument();
  expect(screen.getByRole('link', { name: '查看并处理 →' })).toHaveAttribute('href', '/runs/root');
});

it('keeps all four views reachable and paginates management notices beyond the compact global summary', async () => {
  const notice = { id: 'notice', kind: 'mcp', status: 'failed', attention: true, label: '最近操作', href: '/mcp?operation=notice' };
  const requests = vi.fn(async (url: string) => {
    if (url.startsWith('/api/activity?')) return Response.json({ revision: 1, checked_at: '', conversations: {},
      counts: { all: 1, attention: 0, active: 1, unread: 0 }, management: { attention: 2, items: [notice], truncated: true } });
    if (url.startsWith('/api/activity/management?kind=mcp')) return Response.json({ items: [url.includes('cursor=older')
      ? { ...notice, id: 'older', label: '更早的失败', href: '/mcp?operation=older' } : notice], next_cursor: url.includes('cursor=older') ? null : 'older' });
    if (url.startsWith('/api/activity/')) return Response.json({ items: [], next_cursor: null });
    return Response.json([]);
  });
  vi.stubGlobal('fetch', requests);
  render(<MemoryRouter><ApplicationActivity conversationIds={[]} version={0}><ActivityRunsPage /></ApplicationActivity></MemoryRouter>);
  await userEvent.click(await screen.findByRole('button', { name: '查看全部后台操作' }));
  await userEvent.click(await screen.findByRole('button', { name: '加载更多工具服务操作' }));
  const old = await screen.findByText('更早的失败');
  expect(within(old.parentElement!).getByRole('link')).toHaveAttribute('href', '/mcp?operation=older');
  expect(screen.getByText('最近操作')).toBeInTheDocument();
  for (const label of ['进行中 1', '新结果 0', '全部 1']) await userEvent.click(screen.getByRole('button', { name: label }));
  await waitFor(() => expect(requests.mock.calls.some(([url]) => url === '/api/activity/runs?view=all')).toBe(true));
});
