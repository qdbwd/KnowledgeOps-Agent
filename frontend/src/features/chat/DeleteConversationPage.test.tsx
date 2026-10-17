import '@testing-library/jest-dom/vitest';
import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { DeleteConversationPage } from './DeleteConversationPage';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const preview = { conversation_id: 'chat', title: '待删除会话', state: 'active', version: 'a'.repeat(64),
  eligible: true, blockers: [], counts: { messages: 4, runs: 2, snapshots: 1, history_records: 3 },
  preserved: ['上传资料', '知识文档及版本', '草稿', '独立文件产物', '长期记忆正文', '学习配置'] };
const job = { id: 'delete-job', target_id: 'chat', status: 'pending', phase: 'conversation_detach', last_error: null };
function show() {
  return render(<MemoryRouter initialEntries={['/chat/chat/deletion']}><Routes>
    <Route path="/chat/:conversationId/deletion" element={<DeleteConversationPage />} />
  </Routes></MemoryRouter>);
}

it('shows retained assets, submits the preview version and waits for real completion', async () => {
  const requests = vi.fn(async (url: string, options?: RequestInit) => {
    if (options?.method === 'DELETE') return Response.json(job);
    if (url.endsWith('deletion-preview')) return Response.json(preview);
    return Response.json({ ...job, status: 'completed', phase: 'completed' });
  });
  vi.stubGlobal('fetch', requests);
  show();
  expect(await screen.findByText(/以下内容会保留/)).toHaveTextContent('长期记忆正文');
  fireEvent.click(screen.getByRole('button', { name: '确认删除会话' }));
  await screen.findByRole('heading', { name: '正在清理会话' });
  expect(requests.mock.calls.find(([, options]) => options?.method === 'DELETE')?.[1]?.body)
    .toBe(JSON.stringify({ expected_version: preview.version }));
  expect(screen.queryByRole('heading', { name: '会话已删除' })).not.toBeInTheDocument();
  await waitFor(() => expect(screen.getByRole('heading', { name: '会话已删除' })).toBeInTheDocument(), { timeout: 3500 });
  expect(screen.queryByRole('button', { name: '确认删除会话' })).not.toBeInTheDocument();
});

it('does not offer deletion while an operation result remains unknown', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => Response.json({ ...preview, eligible: false, blockers: ['command:one:unknown'] })));
  show();
  await screen.findByText('有执行结果尚未核实，需先完成核对。');
  expect(screen.getByRole('button', { name: '确认删除会话' })).toBeDisabled();
});

it('reloads a failed deletion and retries the same durable job', async () => {
  vi.stubGlobal('fetch', vi.fn(async (url: string, options?: RequestInit) => {
    if (options?.method === 'POST') return Response.json({ ...job, status: 'completed' });
    return Response.json(url.endsWith('deletion-preview') ? { ...preview, state: 'deleting' } : { ...job, status: 'failed' });
  }));
  show();
  fireEvent.click(await screen.findByRole('button', { name: '重试清理' }));
  await screen.findByRole('heading', { name: '会话已删除' });
});

it('reports a changed preview without automatically sending another deletion', async () => {
  const requests = vi.fn(async (_url: string, options?: RequestInit) => options?.method === 'DELETE'
    ? Response.json({ detail: '会话状态已变化，请重新预览后删除。' }, { status: 409 })
    : Response.json(preview));
  vi.stubGlobal('fetch', requests);
  show();
  fireEvent.click(await screen.findByRole('button', { name: '确认删除会话' }));
  await screen.findByText('会话状态已变化，请重新预览后删除。');
  expect(requests.mock.calls.filter(([, options]) => options?.method === 'DELETE')).toHaveLength(1);
});
