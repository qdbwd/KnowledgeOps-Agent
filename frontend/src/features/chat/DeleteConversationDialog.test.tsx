import '@testing-library/jest-dom/vitest';
import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { DeleteConversationDialog } from './DeleteConversationDialog';
import { BackgroundDeletions } from './BackgroundDeletions';

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
  expect(JSON.parse(localStorage.getItem('knowledgeops:pending-conversation-deletions')!)).toEqual([
    'chat',
  ]);
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
it('reloads pending cleanup, only reports failure and retries the same job', async () => {
  localStorage.setItem('knowledgeops:pending-conversation-deletions', '["chat"]');
  let completed = false;
  const requests = vi.fn(async (_url: string, options?: RequestInit) => {
    if (options?.method === 'POST') completed = true;
    return Response.json({ status: completed ? 'completed' : 'failed' });
  });
  vi.stubGlobal('fetch', requests);
  render(
    <BackgroundDeletions>
      <p>正常页面</p>
    </BackgroundDeletions>,
  );
  fireEvent.click(await screen.findByRole('button', { name: '重试后台清理' }));
  await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument());
  await waitFor(() =>
    expect(
      JSON.parse(localStorage.getItem('knowledgeops:pending-conversation-deletions')!),
    ).toEqual([]),
  );
  expect(requests.mock.calls.find(([, options]) => options?.method === 'POST')?.[0]).toBe(
    '/api/conversations/chat/deletion/retry',
  );
});
