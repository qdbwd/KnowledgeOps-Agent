// 请求失败后的重试和用户明确再次发送是不同动作，不能共享永久幂等键。
import '@testing-library/jest-dom/vitest';
import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { ChatPage } from './ChatPage';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it('shows only provider-declared reasoning levels and submits the selected value', async () => {
  const submissions: { reasoning_effort: string }[] = [];
  let settings = { permission_mode: 'ask', reasoning_effort: 'default', revision: 0 };
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, options?: RequestInit) => {
      if (url.endsWith('/runtime-settings')) {
        if (options?.method === 'PATCH')
          settings = {
            ...settings,
            ...JSON.parse(String(options.body)),
            revision: settings.revision + 1,
          };
        return Response.json(settings);
      }
      if (url === '/api/model-capabilities')
        return Response.json({
          levels: ['low', 'high', 'max'],
          default_level: 'high',
          can_disable: true,
        });
      if (options?.method === 'POST') {
        submissions.push(JSON.parse(String(options.body)));
        return Response.json({ id: 'reasoning-run' }, { status: 202 });
      }
      return Response.json(url.includes('/page') ? { items: [], next_cursor: null } : []);
    }),
  );
  const user = userEvent.setup();
  render(
    <MemoryRouter initialEntries={['/chat/conversation']}>
      <Routes>
        <Route path="/chat/:conversationId" element={<ChatPage />} />
      </Routes>
    </MemoryRouter>,
  );
  const selector = await screen.findByRole('combobox', { name: '思考强度' });
  expect(screen.queryByRole('option', { name: '中' })).not.toBeInTheDocument();
  await user.selectOptions(selector, 'max');
  await user.type(screen.getByRole('textbox', { name: '发送给 KnowledgeOps 的消息' }), '分析材料');
  await user.click(screen.getByRole('button', { name: '发送' }));
  await waitFor(() => expect(submissions[0].reasoning_effort).toBe('max'));
});

it('shows model-input context percentage and explains its measurement basis', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      if (url.endsWith('/context-usage'))
        return Response.json({
          input_tokens: 131072,
          model_window: 1048576,
          percentage: 50,
          method: 'provider_usage',
          estimated: false,
          basis: 'last_model_input',
          output_reserve: 8192,
          framing_reserve: 4096,
          summary_trigger_tokens: 235929,
          summary_fraction: 0.9,
          summary_trigger_basis: 'input_tokens',
          working_window: 262144,
        });
      return Response.json(url.includes('/page') ? { items: [], next_cursor: null } : []);
    }),
  );
  render(
    <MemoryRouter initialEntries={['/chat/conversation']}>
      <Routes>
        <Route path="/chat/:conversationId" element={<ChatPage />} />
      </Routes>
    </MemoryRouter>,
  );
  expect(await screen.findByText('上下文 50%')).toBeInTheDocument();
  const detail = screen.getByText(/131,072/);
  expect(detail).toHaveTextContent('262,144');
  expect(detail).toHaveTextContent('最近一次模型输入');
  expect(detail).toHaveTextContent('供应商用量');
});

it('reuses an uncertain request key but creates a fresh key after confirmed success', async () => {
  const submissions: { content: string; request_key: string }[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, options?: RequestInit) => {
      if (options?.method === 'POST') {
        submissions.push(JSON.parse(String(options.body)));
        if (submissions.length === 1) throw new TypeError('连接中断，提交结果未知');
        return Response.json({ id: `run-${submissions.length}` }, { status: 202 });
      }
      return Response.json(url.includes('/page') ? { items: [], next_cursor: null } : []);
    }),
  );
  const user = userEvent.setup();
  render(
    <MemoryRouter initialEntries={['/chat/conversation']}>
      <Routes>
        <Route path="/chat/:conversationId" element={<ChatPage />} />
      </Routes>
    </MemoryRouter>,
  );
  const input = screen.getByRole('textbox', { name: '发送给 KnowledgeOps 的消息' });
  await user.type(input, '继续');
  await user.click(screen.getByRole('button', { name: '发送' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('提交结果未知');
  expect(input).toHaveValue('继续');
  await user.click(screen.getByRole('button', { name: '发送' }));
  await waitFor(() => expect(input).toHaveValue(''));
  await user.type(input, '继续');
  await user.click(screen.getByRole('button', { name: '发送' }));
  await waitFor(() => expect(submissions).toHaveLength(3));
  expect(submissions[0].request_key).toBe(submissions[1].request_key);
  expect(submissions[2].request_key).not.toBe(submissions[1].request_key);
});

it('defaults auxiliary off and changing its choice gives an uncertain request a new key', async () => {
  const submissions: { auxiliary: boolean; request_key: string }[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, options?: RequestInit) => {
      if (options?.method === 'POST') {
        submissions.push(JSON.parse(String(options.body)));
        if (submissions.length === 1) throw new TypeError('连接中断，提交结果未知');
        return Response.json({ id: 'run-auxiliary' }, { status: 202 });
      }
      return Response.json(url.includes('/page') ? { items: [], next_cursor: null } : []);
    }),
  );
  const user = userEvent.setup();
  render(
    <MemoryRouter initialEntries={['/chat/conversation']}>
      <Routes>
        <Route path="/chat/:conversationId" element={<ChatPage />} />
      </Routes>
    </MemoryRouter>,
  );
  await user.click(screen.getByRole('button', { name: '更多设置与材料' }));
  let option = screen.getByRole('checkbox', { name: '允许辅助模型' });
  expect(option).not.toBeChecked();
  await user.type(screen.getByRole('textbox', { name: '发送给 KnowledgeOps 的消息' }), '核对材料');
  await user.click(screen.getByRole('button', { name: '发送' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('提交结果未知');
  expect(submissions[0].auxiliary).toBe(false);
  await user.click(screen.getByRole('button', { name: '更多设置与材料' }));
  option = screen.getByRole('checkbox', { name: '允许辅助模型' });
  await user.click(option);
  await user.click(screen.getByRole('button', { name: '发送' }));
  await waitFor(() => expect(submissions).toHaveLength(2));
  expect(submissions[1].auxiliary).toBe(true);
  expect(submissions[1].request_key).not.toBe(submissions[0].request_key);
});

it('lets the user turn subagent delegation off without forcing it back on', async () => {
  const submissions: { delegation: boolean }[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, options?: RequestInit) => {
      if (options?.method === 'POST') {
        submissions.push(JSON.parse(String(options.body)));
        return Response.json({ id: 'run-no-delegation' }, { status: 202 });
      }
      return Response.json(url.includes('/page') ? { items: [], next_cursor: null } : []);
    }),
  );
  const user = userEvent.setup();
  render(
    <MemoryRouter initialEntries={['/chat/conversation']}>
      <Routes>
        <Route path="/chat/:conversationId" element={<ChatPage />} />
      </Routes>
    </MemoryRouter>,
  );
  await user.click(screen.getByRole('button', { name: '更多设置与材料' }));
  const option = screen.getByRole('checkbox', { name: '允许子Agent分工' });
  if ((option as HTMLInputElement).checked) await user.click(option);
  await user.type(screen.getByRole('textbox', { name: '发送给 KnowledgeOps 的消息' }), '自行核对');
  await user.click(screen.getByRole('button', { name: '发送' }));
  await waitFor(() => expect(submissions).toHaveLength(1));
  expect(submissions[0].delegation).toBe(false);
  expect(option).not.toBeChecked();
});
