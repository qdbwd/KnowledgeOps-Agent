import '@testing-library/jest-dom/vitest';
import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { DeleteConversationDialog } from './DeleteConversationDialog';
import { BackgroundDeletions } from './BackgroundDeletions';
import { ApplicationActivity } from '../activity/ApplicationActivity';
import { ConversationDeletionHistory } from './ConversationDeletionHistory';

afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.unstubAllGlobals();
});
const preview = {
  title: '待删除会话',
  state: 'active',
  version: 'a'.repeat(64),
  eligible: true,
  blockers: [],
  counts: { messages: 4, runs: 2 },
  preserved: ['长期记忆正文'],
};
function show(onDeleted = vi.fn()) {
  render(
    <MemoryRouter>
      <BackgroundDeletions>
        <DeleteConversationDialog conversationId="chat" onClose={() => {}} onDeleted={onDeleted} />
      </BackgroundDeletions>
    </MemoryRouter>,
  );
  return onDeleted;
}
it('returns immediately after accepted deletion without a cleaning page', async () => {
  const requests = vi.fn(async (url: string, options?: RequestInit) =>
    Response.json(
      options?.method === 'DELETE'
        ? { status: 'pending' }
        : url.endsWith('deletion-preview')
          ? preview
          : { status: 'pending' },
    ),
  );
  vi.stubGlobal('fetch', requests);
  const onDeleted = show();
  await screen.findByText(/以下内容会保留/);
  fireEvent.click(screen.getByRole('button', { name: '确认删除会话' }));
  await waitFor(() => expect(onDeleted).toHaveBeenCalledOnce());
  expect(requests.mock.calls.find(([, options]) => options?.method === 'DELETE')?.[1]?.body).toBe(
    JSON.stringify({ expected_version: preview.version }),
  );
  expect(screen.queryByText('正在清理会话')).not.toBeInTheDocument();
  // 服务端已持久登记，浏览器不再维护第二份待清理索引。
  expect(localStorage.getItem('knowledgeops:pending-conversation-deletions')).toBeNull();
});
it('blocks unresolved execution and does not cancel it', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () =>
      Response.json({ ...preview, eligible: false, blockers: ['command:one:unknown'] }),
    ),
  );
  show();
  await screen.findByText('有执行结果尚未核实，需先完成核对。');
  expect(screen.getByRole('button', { name: '确认删除会话' })).toBeDisabled();
});
it('keeps a changed preview open without resubmitting deletion', async () => {
  const requests = vi.fn(async (_url: string, options?: RequestInit) =>
    options?.method === 'DELETE'
      ? Response.json({ detail: '请重新预览' }, { status: 409 })
      : Response.json(preview),
  );
  vi.stubGlobal('fetch', requests);
  const onDeleted = show();
  await screen.findByText(/以下内容会保留/);
  fireEvent.click(screen.getByRole('button', { name: '确认删除会话' }));
  await screen.findByText('请重新预览');
  expect(onDeleted).not.toHaveBeenCalled();
  expect(requests.mock.calls.filter(([, options]) => options?.method === 'DELETE')).toHaveLength(1);
});
it('finds failure from the global feed and retries only from its original conversation handler', async () => {
  localStorage.setItem('knowledgeops:pending-conversation-deletions', '["chat"]');
  let completed = false;
  const notice = { id: 'cleanup', target_id: 'chat', kind: 'deletion', status: 'failed', label: '未完成的会话清理', attention: true, href: '/conversations?deletion=chat' };
  const requests = vi.fn(async (url: string, options?: RequestInit) => {
    if (options?.method === 'POST') completed = true;
    if (url.startsWith('/api/activity?')) return Response.json({ revision: 1,
      counts: { all: 0, attention: 0, active: 0, unread: 0 }, conversations: {}, checked_at: '',
      management: { attention: completed ? 0 : 1, items: completed ? [] : [notice], truncated: false } });
    if (url.startsWith('/api/activity/management')) return Response.json({ items: completed ? [] : [notice], next_cursor: null });
    return Response.json({ status: completed ? 'completed' : 'failed', summary: { title: '原会话' } });
  });
  vi.stubGlobal('fetch', requests);
  render(
    <MemoryRouter initialEntries={['/conversations?deletion=chat']}>
      <ApplicationActivity conversationIds={[]} version={0}><BackgroundDeletions>
        <ConversationDeletionHistory />
      </BackgroundDeletions></ApplicationActivity>
    </MemoryRouter>,
  );
  fireEvent.click(await screen.findByRole('button', { name: '重试后台清理' }));
  await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument());
  await waitFor(() =>
    expect(
      localStorage.getItem('knowledgeops:pending-conversation-deletions'),
    ).toBeNull(),
  );
  expect(requests.mock.calls.find(([, options]) => options?.method === 'POST')?.[0]).toBe(
    '/api/conversations/chat/deletion/retry',
  );
  expect(requests.mock.calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(1);
  expect(await screen.findByText('清理已完成，仍有依赖的内容已保留。')).toBeInTheDocument();
});
