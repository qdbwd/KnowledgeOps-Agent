import '@testing-library/jest-dom/vitest';
import { afterEach, expect, it, vi } from 'vitest';
import { act, cleanup, render, screen } from '@testing-library/react';
import { useCallback, useState } from 'react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { RunsPage } from './RunsPage';
import { ChatPage } from './ChatPage';
import { WorkspaceContext } from './workspace';

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

it.each(['running', 'completed'])(
  'observes queued work becoming %s without navigation',
  async (nextStatus) => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    let phase = 'queued';
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
        if (url.includes('/runs/page'))
          return Response.json({
            items: url.includes('status=running') && phase !== 'running' ? [] : [run],
            next_cursor: null,
          });
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
    expect(await screen.findByText(/排队任务：/)).toBeInTheDocument();
    expect(streams).toHaveLength(0);
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
  expect(streams.some((url) => url.includes('root-active'))).toBe(true);
  expect(streams.some((url) => url.includes('root-queued'))).toBe(false);
});
