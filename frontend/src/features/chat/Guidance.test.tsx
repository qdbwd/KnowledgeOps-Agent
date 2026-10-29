import '@testing-library/jest-dom/vitest';
import 'fake-indexeddb/auto';
import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { ChatPage } from './ChatPage';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it('keeps messages queued until the user guides the current root generation', async () => {
  const actions: { url: string; body: unknown }[] = [];
  vi.stubGlobal(
    'EventSource',
    class {
      addEventListener() {}
      close() {}
    },
  );
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, options?: RequestInit) => {
      if (url === '/api/client-state') return Response.json({ namespace: 'guidance-unit', contract_version: 1 });
      if (options?.method === 'POST') {
        actions.push({ url, body: JSON.parse(String(options.body)) });
        return Response.json({ id: 'delivery', status: 'pending' });
      }
      if (url.includes('/guidance')) return Response.json([]);
      if (url.includes('/runs/page'))
        return Response.json({
          items: [
            {
              id: url.includes('status=running') ? 'active' : 'queued',
              status: url.includes('status=running') ? 'running' : 'queued',
              task: '请补充例子',
              kind: 'agent',
              generation: 3,
              parent_id: null,
              outcome: {},
              tool_calls: 0,
            },
          ],
          next_cursor: null,
        });
      return Response.json(url.includes('/page') ? { items: [], next_cursor: null } : []);
    }),
  );
  render(
    <MemoryRouter initialEntries={['/chat/c']}>
      <Routes>
        <Route path="/chat/:conversationId" element={<ChatPage />} />
      </Routes>
    </MemoryRouter>,
  );
  const button = await screen.findByRole('button', { name: '引导当前任务' });
  expect(actions).toHaveLength(0);
  await userEvent.click(button);
  expect(actions).toEqual([
    { url: '/api/runs/queued/guide', body: { target_root_id: 'active', target_generation: 3 } },
  ]);
});
