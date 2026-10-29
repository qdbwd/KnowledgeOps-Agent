import '@testing-library/jest-dom/vitest';
import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { ApplicationActivity, useApplicationActivity } from './ApplicationActivity';

const summary = { revision: 1, counts: { all: 1, attention: 0, active: 0, unread: 1 },
  conversations: {}, management: { attention: 0, items: [], truncated: false }, checked_at: '2026-10-24T00:00:00Z' };
function State() {
  const { data, error } = useApplicationActivity();
  return <p>{error || (data ? `未读 ${data.counts.unread}` : '尚未核对')}</p>;
}
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers(); });

it('uses one foreground poll, stops while hidden, and refreshes immediately on return', async () => {
  vi.useFakeTimers();
  const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible');
  const fetcher = vi.fn().mockResolvedValue(Response.json(summary));
  // 每次请求返回独立 Response，不能二次读取同一响应的正文。
  fetcher.mockImplementation(async () => Response.json(summary));
  vi.stubGlobal('fetch', fetcher);
  render(<ApplicationActivity conversationIds={['chat']} version={0}><State /><State /></ApplicationActivity>);
  await act(async () => {});
  expect(fetcher).toHaveBeenCalledTimes(1);
  await act(() => vi.advanceTimersByTimeAsync(5000));
  expect(fetcher).toHaveBeenCalledTimes(2);
  visibility.mockReturnValue('hidden');
  act(() => document.dispatchEvent(new Event('visibilitychange')));
  await act(() => vi.advanceTimersByTimeAsync(15000));
  expect(fetcher).toHaveBeenCalledTimes(2);
  visibility.mockReturnValue('visible');
  await act(async () => document.dispatchEvent(new Event('visibilitychange')));
  expect(fetcher).toHaveBeenCalledTimes(3);
  expect(screen.getAllByText('未读 1')).toHaveLength(2);
  expect(fetcher.mock.calls.every(([, options]) => !options.method)).toBe(true);
});

it('discards a slow hidden response and keeps last known facts on a network failure', async () => {
  vi.useFakeTimers();
  const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible');
  let release!: (response: Response) => void;
  const fetcher = vi.fn().mockImplementationOnce(() => new Promise<Response>((resolve) => { release = resolve; }))
    .mockImplementationOnce(async () => Response.json(summary)).mockRejectedValue(new TypeError('offline'));
  vi.stubGlobal('fetch', fetcher);
  render(<ApplicationActivity conversationIds={[]} version={0}><State /></ApplicationActivity>);
  visibility.mockReturnValue('hidden');
  act(() => document.dispatchEvent(new Event('visibilitychange')));
  visibility.mockReturnValue('visible');
  await act(async () => document.dispatchEvent(new Event('visibilitychange')));
  expect(screen.getByText('未读 1')).toBeInTheDocument();
  await act(async () => release(Response.json({ ...summary, counts: { ...summary.counts, unread: 0 } })));
  expect(screen.getByText('未读 1')).toBeInTheDocument();
  await act(() => vi.advanceTimersByTimeAsync(5000));
  expect(screen.getByText(/暂时无法更新任务状态/)).toBeInTheDocument();
  expect(fetcher).toHaveBeenCalledTimes(3);
});
