import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import type { Run } from '../../shared/types/types';
import { RunPauseControl } from './RunPauseControl';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it('requests a graceful pause without calling cancellation', async () => {
  const fetch = vi.fn<typeof globalThis.fetch>(async () =>
    Response.json({ status: 'running', pause_requested: true }),
  );
  vi.stubGlobal('fetch', fetch);
  const onChanged = vi.fn();
  render(
    <RunPauseControl
      run={{ id: 'r', kind: 'agent', status: 'running' } as Run}
      onChanged={onChanged}
    />,
  );
  await userEvent.click(screen.getByRole('button', { name: '暂停任务' }));
  await waitFor(() => expect(onChanged).toHaveBeenCalledOnce());
  expect(fetch.mock.calls[0][0]).toBe('/api/runs/r/pause');
});

it.each(['agent', 'memory_classification'] as const)(
  'resumes a persisted paused %s without changing its budgets',
  async (kind) => {
    const fetch = vi.fn<typeof globalThis.fetch>(async () => Response.json({ status: 'queued' }));
    vi.stubGlobal('fetch', fetch);
    render(
      <RunPauseControl
        run={{ id: 'r', kind, status: 'paused', pause_requested: true } as Run}
        onChanged={() => {}}
      />,
    );
    await userEvent.click(screen.getByRole('button', { name: '继续任务' }));
    expect(fetch.mock.calls[0][0]).toBe('/api/runs/r/resume');
    expect(JSON.parse(String(fetch.mock.calls[0][1]?.body))).toEqual({ budgets: {} });
  },
);

it('shows pending pause as pending rather than claiming the response was interrupted', () => {
  render(
    <RunPauseControl
      run={{ kind: 'agent', status: 'running', pause_requested: true } as Run}
      onChanged={() => {}}
    />,
  );
  expect(screen.getByRole('button', { name: '正在收尾暂停…' })).toBeDisabled();
});
