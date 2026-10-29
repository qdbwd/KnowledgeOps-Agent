import '@testing-library/jest-dom/vitest';
import { act, cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';
import { SystemPage } from './SystemPage';

const payload = {
  status: 'degraded',
  checked_at: 1792861126,
  cached: false,
  layers: {
    process: { ok: true, detail: 'API 进程可响应' },
    foundation: {
      ok: true,
      database: { ok: true },
      migration: { ok: false, current: null, head: '0044', detail: '迁移版本表缺失' },
      receipts: { ok: true },
      directories: { ok: true, items: { workspace_path: '/workspace', skills_path: '/workspace/skills', mcp_path: '/workspace/mcp' } },
    },
    execution: {
      ok: true, state: 'offline', worker_lease: false, active_runs: 0, heartbeat_seconds: null,
      redis: { broker: { ok: true }, runtime: { ok: true } },
    },
    backlog: { ok: true, queued_runs: 2, due_dispatches: 0, retrying_dispatches: 0 },
  },
};

const backups = {
  available: true,
  items: [
    {
      id: 'b1', created_at: '2026-10-24T00:00:00Z', format: '3', status: 'verified',
      detail: '已发布清单并通过隔离恢复校验',
      operations: [{ id: 'op1', kind: 'backup', state: 'complete', phase: 'complete', database: '' }],
      restore: { checked: true, passed: true, failed_checks: [], database: 'restore_b1' },
    },
    {
      id: 'b2', created_at: null, format: '', status: 'incomplete', detail: '没有成功清单：创建失败或中断，目录保留供诊断',
      operations: [], restore: { checked: false, passed: false, failed_checks: [], database: '' },
    },
  ],
  truncated: false,
};

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers(); });

it('shows layered readiness, refreshes on a bounded interval and lists backup records read-only', async () => {
  vi.useFakeTimers();
  const fetcher = vi.fn().mockImplementation(async (url: string) =>
    Response.json(String(url).includes('/api/readiness') ? payload : backups));
  vi.stubGlobal('fetch', fetcher);
  render(<MemoryRouter><SystemPage /></MemoryRouter>);
  await act(async () => {});

  expect(screen.getByText(/基础就绪，执行未就绪/)).toBeInTheDocument();
  expect(screen.getByText(/迁移版本表缺失/)).toBeInTheDocument();
  expect(screen.getByText(/Worker 未启动或已停止/)).toBeInTheDocument();
  expect(screen.getByText('2 个')).toBeInTheDocument();
  expect(screen.getByText('已校验并有隔离恢复')).toBeInTheDocument();
  expect(screen.getByText('b1')).toBeInTheDocument();
  expect(screen.getByText('未完成')).toBeInTheDocument();
  expect(fetcher).toHaveBeenCalledTimes(2);

  await act(() => vi.advanceTimersByTimeAsync(5000));
  expect(fetcher).toHaveBeenCalledTimes(4);
});
