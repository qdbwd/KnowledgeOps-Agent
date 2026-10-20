import '@testing-library/jest-dom/vitest';
import { afterEach, expect, it, vi } from 'vitest';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { useCallback, useState } from 'react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { RunsPage } from '../features/runs/RunsPage';
import { ChatPage } from '../features/chat/ChatPage';
import { WorkspaceContext } from '../shared/state/workspace';

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

it.each([
  ['queued', 'running'], ['queued', 'completed'], ['waiting_children', 'completed'],
])(
  'observes %s work becoming %s without navigation',
  async (initialStatus, nextStatus) => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    let phase = initialStatus;
    const streams: string[] = [];
    vi.stubGlobal(
      'EventSource',
      class {
        constructor(url: string) {
          streams.push(url);
        }
        addEventListener() {}
        close() {}
      },
    );
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        const run = {
          id: 'root',
          task: '等待处理的消息',
          status: phase,
          outcome: {},
          tool_calls: 0,
          parent_id: null,
        };
        if (url.includes('/runs/page')) {
          const filter = new URL(url, 'http://localhost').searchParams.get('status');
          return Response.json({
            items: filter && filter !== phase ? [] : [run],
            next_cursor: null,
          });
        }
        if (url === '/api/runs/root') return Response.json(run);
        if (url.includes('/changesets')) return Response.json({ items: [], next_cursor: null });
        if (url.endsWith('/messages') && phase === 'completed')
          return Response.json([
            {
              id: 'reply',
              role: 'assistant',
              content: '后台任务已处理完成',
              created_at: '2026-01-01T00:00:00Z',
            },
          ]);
        return Response.json([]);
      }),
    );
    function Workspace() {
      const [version, setVersion] = useState(0);
      const refresh = useCallback(() => setVersion((value) => value + 1), []);
      return (
        <WorkspaceContext.Provider value={{ version, refresh }}>
          <ChatPage />
        </WorkspaceContext.Provider>
      );
    }
    render(
      <MemoryRouter initialEntries={['/chat/conversation']}>
        <Routes>
          <Route path="/chat/:conversationId" element={<Workspace />} />
        </Routes>
      </MemoryRouter>,
    );
    expect(await screen.findByText('查看运行详情')).toBeInTheDocument();
    // 排队/等待也保持事件连接，使子任务结果和人工插入无需刷新即可出现。
    expect(streams).toEqual(['/api/runs/root/events']);
    if (initialStatus === 'waiting_children') {
      // 子结果使主任务短暂入队；刷新时两个专用状态查询均返回空。
      phase = 'queued';
      await act(async () => { await vi.advanceTimersByTimeAsync(2100); });
    }
    phase = nextStatus;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2100);
    });
    if (nextStatus === 'running') {
      expect(await screen.findByText('查看运行详情')).toHaveAttribute('href', '/runs/root');
      phase = 'completed';
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2100);
      });
    }
    expect(await screen.findByText('后台任务已处理完成')).toBeInTheDocument();
    expect(screen.queryByText(/排队任务：/)).not.toBeInTheDocument();
  },
);

it('loads older runs with a cursor and retains already displayed entries', async () => {
  const seen: string[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      seen.push(url);
      if (url.startsWith('/api/asset-deletions')) return Response.json([]);
      const old = url.includes('cursor=older');
      return Response.json({
        items: [
          {
            id: old ? 'old' : 'new',
            task: old ? '旧任务' : '新任务',
            status: 'completed',
            created_at: '2026-01-01T00:00:00Z',
            kind: 'agent',
            parent_id: null,
          },
        ],
        next_cursor: old ? null : 'older',
      });
    }),
  );
  render(
    <MemoryRouter>
      <RunsPage />
    </MemoryRouter>,
  );
  expect(await screen.findByText('新任务')).toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: '加载更多运行' }));
  expect(await screen.findByText('旧任务')).toBeInTheDocument();
  expect(screen.getByText('新任务')).toBeInTheDocument();
  expect(seen.some((url) => url.includes('cursor=older'))).toBe(true);
});

it('selects the running root independently of newer queued work', async () => {
  const streams: string[] = [];
  vi.stubGlobal(
    'EventSource',
    class {
      constructor(url: string) {
        streams.push(url);
      }
      addEventListener() {}
      close() {}
    },
  );
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      if (url.includes('/runs')) {
        const running = url.includes('status=running');
        return Response.json({
          items: [
            {
              id: running ? 'root-active' : 'root-queued',
              task: running ? '执行中' : '等待执行',
              status: running ? 'running' : 'queued',
              outcome: {},
              tool_calls: 0,
              parent_id: null,
            },
          ],
          next_cursor: null,
        });
      }
      if (url.includes('/changesets')) return Response.json({ items: [], next_cursor: null });
      return Response.json([]);
    }),
  );
  render(
    <MemoryRouter initialEntries={['/chat/conversation']}>
      <Routes>
        <Route path="/chat/:conversationId" element={<ChatPage />} />
      </Routes>
    </MemoryRouter>,
  );
  expect(await screen.findByText('查看运行详情')).toHaveAttribute('href', '/runs/root-active');
  await waitFor(() => expect(streams.some((url) => url.includes('root-active'))).toBe(true));
  expect(streams.some((url) => url.includes('root-queued'))).toBe(false);
});
