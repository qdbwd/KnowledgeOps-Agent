import '@testing-library/jest-dom/vitest';
import { afterEach, expect, it, vi } from 'vitest';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { RunPage } from './RunsPage';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const completedRun = {
  id: 'root',
  root_id: 'root',
  parent_id: null,
  conversation_id: 'conversation',
  kind: 'agent',
  task: '核对两道知识问答',
  status: 'completed',
  generation: 0,
  checkpoint_id: 'checkpoint',
  budgets: { model_requests: 40, tool_calls: 80, searches: 20, active_seconds: 1200 },
  model_usage: { root_used: 4 },
  tool_calls: 3,
  searches: 0,
  active_ms: 10000,
  created_at: '2026-10-13T12:00:00Z',
  last_event: 29,
  outcome: { kind: 'answered', answer: '本次问答已经完成' },
};

const emptyRecords = {
  models: [],
  events: [],
  event_page: { after: 0, next_after: null, total: 0 },
  artifacts: [],
  resources: null,
  continuation: null,
};

const finalRecords = {
  ...emptyRecords,
  models: Array.from({ length: 4 }, (_, index) => ({
    id: `model-${index}`,
    purpose: 'agent',
    usage: { total_tokens: 100 },
    duration_ms: 10,
    first_token_ms: 5,
    status: 'succeeded',
    request: {},
    response: {},
  })),
  events: [{ sequence: 29, kind: 'finished', data: {}, created_at: '2026-10-13T12:00:10Z' }],
  event_page: { after: 0, next_after: null, total: 29 },
};

function renderRun() {
  return render(
    <MemoryRouter initialEntries={['/runs/root']}>
      <Routes>
        <Route path="/runs/:runId" element={<RunPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

it('shows retained results instead of empty full traces after details expire', async () => {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => Response.json(url === '/api/runs/root'
    ? { ...completedRun, status: 'failed', details_available: false, resumable: false }
    : { ...emptyRecords, artifacts: [{ id: 'old', kind: 'tool_result', length: 0, available: false }] })));
  renderRun();
  await screen.findByRole('heading', { name: '运行结果摘要' });
  expect(screen.getByText(/无法原地恢复或导出完整轨迹/)).toBeInTheDocument();
  expect(screen.getByText('本次问答已经完成')).toBeInTheDocument();
  expect(screen.queryByText('导出记录')).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '确认恢复任务' })).not.toBeInTheDocument();
  expect(screen.queryByRole('heading', { name: /模型调用/ })).not.toBeInTheDocument();
  await screen.findByText('tool_result：正文已过期');
});

it('refreshes records when the status response arrives after an older records snapshot', async () => {
  let finishStatus!: (response: Response) => void;
  const statusResponse = new Promise<Response>((resolve) => {
    finishStatus = resolve;
  });
  let recordReads = 0;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      if (url === '/api/runs/root') return statusResponse;
      if (url.endsWith('/approvals')) return Response.json({ actions: [], commands: [], mcp: [] });
      if (url.startsWith('/api/runs/root/records')) {
        recordReads += 1;
        return Response.json(recordReads === 1 ? emptyRecords : finalRecords);
      }
      return Response.json([]);
    }),
  );
  renderRun();
  await waitFor(() => expect(recordReads).toBeGreaterThan(0));
  await act(async () => finishStatus(Response.json(completedRun)));
  expect(await screen.findByRole('heading', { name: '模型调用 4 次' })).toBeInTheDocument();
  expect(screen.getByText(/事件 29–29 \/ 29/)).toBeInTheDocument();
});

it('does not present records still loading as zero calls or missing frozen resources', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      if (url === '/api/runs/root') return Response.json(completedRun);
      if (url.endsWith('/approvals')) return Response.json({ actions: [], commands: [], mcp: [] });
      if (url.startsWith('/api/runs/root/records')) return new Promise<Response>(() => {});
      return Response.json([]);
    }),
  );
  renderRun();
  await screen.findByText('本次问答已经完成');
  expect(screen.getByRole('heading', { name: /模型调用/ })).toHaveTextContent('加载中');
  expect(screen.queryByText(/此运行尚未冻结资源/)).not.toBeInTheDocument();
  expect(screen.queryByText(/事件 0–0 \/ 0/)).not.toBeInTheDocument();
  expect(screen.queryByText('此运行暂未产生独立材料。')).not.toBeInTheDocument();
});
