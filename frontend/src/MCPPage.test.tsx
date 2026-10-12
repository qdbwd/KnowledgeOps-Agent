import '@testing-library/jest-dom/vitest';
import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { MCPApprovals, MCPPage } from './MCPPage';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it('starts with an empty service list instead of a preconfigured external service', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => Response.json([])),
  );
  render(
    <MemoryRouter>
      <MCPPage />
    </MemoryRouter>,
  );
  expect(await screen.findByText('尚未添加 MCP 服务')).toBeVisible();
  expect(screen.queryByText('Context7')).not.toBeInTheDocument();
});

it('shows exact arguments and binds approval to its displayed receipt hash', async () => {
  const sent: { url: string; body: unknown }[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, options?: RequestInit) => {
      if (options?.method === 'POST') {
        sent.push({ url, body: JSON.parse(String(options.body)) });
        return Response.json({});
      }
      return Response.json([
        {
          id: 'receipt',
          input_hash: 'exact-input',
          status: 'pending',
          tool_name: 'write_note',
          connection_revision: 4,
          request: {
            service_name: '测试服务',
            arguments: { title: '指定内容' },
            description: '写入笔记',
          },
        },
      ]);
    }),
  );
  render(
    <MemoryRouter>
      <MCPApprovals rootId="root" refreshKey={0} onChanged={() => {}} />
    </MemoryRouter>,
  );
  expect(await screen.findByText(/指定内容/)).toBeVisible();
  await userEvent.click(screen.getByRole('button', { name: '允许这次调用' }));
  expect(sent).toEqual([
    {
      url: '/api/runs/root/mcp/receipt/decision',
      body: { expected_hash: 'exact-input', approve: true },
    },
  ]);
});
