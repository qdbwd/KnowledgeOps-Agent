import '@testing-library/jest-dom/vitest';
import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { ExecutionApprovals } from './ExecutionApprovals';
import type { Run } from '../../shared/types/types';

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});
const run = {
  id: 'root',
  root_id: 'root',
  status: 'interrupted',
  outcome: { kind: 'waiting_approval' },
} as Run;
const row = {
  id: 'action',
  run_id: 'root',
  tool_name: 'execute',
  arguments: { command: 'touch work/file' },
  cwd: '/work',
  status: 'pending',
  fingerprint: 'a'.repeat(64),
  rule_candidate: { argv: ['touch', 'work/file'], cwd: '/work', match: 'exact' },
};

it('shows exact file changes and the review fallback reason with only once approval', async () => {
  const sent: unknown[] = [];
  const file = { ...row, tool_name: 'edit_file', rule_candidate: null,
    arguments: { file_path: '/work/note.md', old_string: '原内容', new_string: '更新内容', replace_all: true },
    decision: { actor: 'system', file_target: { kind: 'file', path: '/work/note.md', text: '原内容及背景', text_complete: true },
      review: { result: { verdict: 'ask', reason: '模型调用已关闭，请人工确认本次操作。' } } },
  };
  vi.stubGlobal('fetch', vi.fn(async (_path: string, init?: RequestInit) => {
    if (init?.method === 'POST') { sent.push(JSON.parse(String(init.body))); return Response.json({}); }
    return Response.json({ actions: [file], commands: [], mcp: [] });
  }));
  render(<MemoryRouter><ExecutionApprovals run={run} refreshKey={0} onChanged={() => {}} /></MemoryRouter>);
  expect(await screen.findByText('编辑文件 · 等待批准')).toBeInTheDocument();
  expect(screen.getByText('/work/note.md')).toBeInTheDocument();
  expect(screen.getByText('原内容', { exact: true })).toBeInTheDocument();
  expect(screen.getByText('更新内容')).toBeInTheDocument();
  expect(screen.getByText('替换为（替换全部匹配）')).toBeInTheDocument();
  expect(screen.getByRole('status')).toHaveTextContent('模型调用已关闭');
  expect(screen.queryByText('减少重复询问')).not.toBeInTheDocument();
  expect(screen.queryByText(/此命令包含脚本/)).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: '允许一次' }));
  expect(sent).toEqual([{ expected_fingerprint: file.fingerprint, approve: true, scope: 'once', match: 'exact' }]);
});

it('keeps model denial visible and allows only an explicit once decision', async () => {
  const sent: unknown[] = [];
  vi.stubGlobal('fetch', vi.fn(async (_path: string, init?: RequestInit) => {
    if (init?.method === 'POST') { sent.push(JSON.parse(String(init.body))); return Response.json({}); }
    return Response.json({ actions: [{ ...row, decision: { actor: 'model', review: { result: { verdict: 'deny', reason: '目标未获授权' } } } }], commands: [], mcp: [] });
  }));
  render(<MemoryRouter><ExecutionApprovals run={run} refreshKey={0} onChanged={() => {}} /></MemoryRouter>);
  expect(await screen.findByRole('status')).toHaveTextContent('目标未获授权');
  expect(screen.getByRole('button', { name: '保持拒绝' })).toBeEnabled();
  expect(screen.queryByText('减少重复询问')).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: '核对后仍允许一次' }));
  expect(sent).toEqual([{ expected_fingerprint: row.fingerprint, approve: true, scope: 'once', match: 'exact' }]);
});

it('can stop a run waiting only for MCP approval', async () => {
  const paths: string[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (path: string, init?: RequestInit) => {
      if (init?.method === 'POST') {
        paths.push(path);
        return Response.json({});
      }
      return Response.json({
        actions: [],
        commands: [],
        mcp: [
          {
            id: 'mcp',
            status: 'pending',
            tool_name: 'external_write',
            request: { service_name: 'fixture', arguments: {}, schema_hash: 'hash' },
          },
        ],
      });
    }),
  );
  render(
    <MemoryRouter>
      <ExecutionApprovals run={run} refreshKey={0} onChanged={() => {}} />
    </MemoryRouter>,
  );
  await userEvent.click(await screen.findByRole('button', { name: '停止本次任务' }));
  expect(paths).toEqual(['/api/runs/root/cancel']);
});

it('accepts a decision while paused without requesting automatic resume', async () => {
  const paths: string[] = [];
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (init?.method === 'POST') {
      paths.push(path);
      return Response.json({ ...row, status: 'approved' });
    }
    return Response.json({ actions: [row], commands: [], mcp: [] });
  }));
  render(<MemoryRouter><ExecutionApprovals run={{ ...run, status: 'paused' }} refreshKey={0} onChanged={() => {}} /></MemoryRouter>);
  await userEvent.click(await screen.findByRole('button', { name: '允许一次' }));
  expect(paths).toEqual(['/api/runs/root/approvals/action/decision']);
  expect(screen.getByText('处理决定后仍保持暂停，请明确继续任务后再执行。')).toBeInTheDocument();
});

it('binds once approval to the original fingerprint and supports refresh without a manual resume button', async () => {
  const sent: unknown[] = [];
  let decided = false;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_path: string, init?: RequestInit) => {
      if (init?.method === 'POST') {
        sent.push(JSON.parse(String(init.body)));
        decided = true;
        return Response.json({ ...row, status: 'approved' });
      }
      return Response.json({
        actions: [{ ...row, status: decided ? 'approved' : 'pending' }],
        commands: [],
        mcp: [],
      });
    }),
  );
  const refresh = vi.fn();
  const ui = render(
    <MemoryRouter>
      <ExecutionApprovals run={run} refreshKey={0} onChanged={refresh} />
    </MemoryRouter>,
  );
  await userEvent.click(await screen.findByRole('button', { name: '允许一次' }));
  expect(sent).toEqual([
    { expected_fingerprint: row.fingerprint, approve: true, scope: 'once', match: 'exact' },
  ]);
  expect(await screen.findByText(/命令执行 · 已批准/)).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /恢复/ })).not.toBeInTheDocument();
  ui.rerender(
    <MemoryRouter>
      <ExecutionApprovals run={run} refreshKey={1} onChanged={refresh} />
    </MemoryRouter>,
  );
  expect(await screen.findByText(/命令执行 · 已批准/)).toBeInTheDocument();
});

it('shows scope and argv before saving a persistent rule, and blocks duplicate clicks while submitting', async () => {
  const sent: unknown[] = [];
  let finish!: (response: Response) => void;
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_path: string, init?: RequestInit) => {
      if (init?.method === 'POST') {
        sent.push(JSON.parse(String(init.body)));
        return new Promise<Response>((resolve) => {
          finish = resolve;
        });
      }
      return Response.json({ actions: [row], commands: [], mcp: [] });
    }),
  );
  render(
    <MemoryRouter>
      <ExecutionApprovals run={run} refreshKey={0} onChanged={() => {}} />
    </MemoryRouter>,
  );
  await userEvent.click(await screen.findByText('减少重复询问'));
  await userEvent.selectOptions(screen.getByRole('combobox', { name: '匹配方式' }), 'prefix');
  expect(screen.getByText(/之后的参数不受这条允许规则限制/)).toBeInTheDocument();
  const button = screen.getByRole('button', { name: '保存为跨会话规则' });
  await userEvent.click(button);
  expect(button).toBeDisabled();
  expect(sent).toEqual([
    { expected_fingerprint: row.fingerprint, approve: true, scope: 'persistent', match: 'prefix' },
  ]);
  finish(Response.json({}));
});

it('resolves unknown results explicitly without claiming success', async () => {
  const sent: unknown[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_path: string, init?: RequestInit) => {
      if (init?.method === 'POST') {
        sent.push(JSON.parse(String(init.body)));
        return Response.json({});
      }
      return Response.json({
        actions: [],
        commands: [
          {
            id: 'receipt',
            status: 'unknown',
            revision: 3,
            attempt: 1,
            request: { args: { command: 'touch marker' }, cwd: '/work' },
          },
        ],
        mcp: [],
      });
    }),
  );
  render(
    <MemoryRouter>
      <ExecutionApprovals run={run} refreshKey={0} onChanged={() => {}} />
    </MemoryRouter>,
  );
  await userEvent.click(await screen.findByRole('button', { name: '已核对，跳过并继续' }));
  expect(sent).toEqual([{ expected_revision: 3, decision: 'skip' }]);
  expect(screen.getByText(/重试可能重复产生副作用/)).toBeInTheDocument();
});
