import '@testing-library/jest-dom/vitest';
import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ContextUsage } from './ContextUsage';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it('explains model input composition without displaying prompt contents or cumulative usage', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => Response.json({
    basis: 'last_model_input', input_tokens: 12000, working_window: 20000, model_window: 1048576,
    percentage: 60, estimated: false, output_reserve: 1000, framing_reserve: 100,
    summary_trigger_tokens: 18000, summary_trigger_basis: 'input_tokens',
    components: { system_rules: 3000, required_background: 2000, optional_background: 1000,
      history: 5000, tool_schemas: 1000, protocol_base: 0 },
  })));
  render(<ContextUsage conversationId="one" active={false} version={1} />);
  await userEvent.click(await screen.findByText('上下文 60%'));
  expect(screen.getByText('必需任务背景')).toBeVisible();
  expect(screen.getByText('工具定义')).toBeVisible();
  expect(screen.getByText(/输入组成（本地估计/)).toBeVisible();
  expect(screen.getByText(/12,000 \/ 20,000 tokens（供应商用量）/)).toBeVisible();
});

it('submits a distinct compact operation and displays its completion without a chat message', async () => {
  const submissions: string[] = [];
  const changed = vi.fn();
  vi.stubGlobal('fetch', vi.fn(async (url: string, options?: RequestInit) => {
    if (options?.method === 'POST') {
      expect(url).toBe('/api/conversations/one/context-compaction');
      submissions.push(JSON.parse(String(options.body)).request_key);
      return Response.json({ id: 'compact', status: 'queued', outcome: {} });
    }
    if (url.endsWith('/context-compaction')) return Response.json({
      available: submissions.length === 0, reason: '', run: submissions.length ? {
        id: 'compact', status: 'completed', outcome: { message: '上下文已压缩，原聊天记录保留。' },
      } : null,
    });
    return Response.json({ percentage: null });
  }));
  render(<ContextUsage conversationId="one" active={false} version={1} onChanged={changed} />);
  await userEvent.click(await screen.findByText('上下文 —'));
  await userEvent.click(screen.getByRole('button', { name: '立即压缩上下文' }));
  expect(await screen.findByText('上下文已压缩，原聊天记录保留。')).toBeVisible();
  expect(submissions).toHaveLength(1);
  expect(submissions[0]).toBeTruthy();
  expect(changed).toHaveBeenCalled();
});
