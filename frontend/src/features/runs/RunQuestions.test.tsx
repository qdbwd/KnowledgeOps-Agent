import '@testing-library/jest-dom/vitest';
import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { RunQuestions } from './RunQuestions';
import type { Run } from '../../shared/types/types';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const run = { id: 'r', root_id: 'r', status: 'waiting_input', question_count: 1 } as Run;
const question = { id: 'q', revision: 3, status: 'pending', answer: null,
  request: { question: '面向哪些读者？', options: ['初学者', '有经验'], context: '决定解释深度' } };

it('loads saved questions and submits the selected answer with its revision', async () => {
  const posts: { path: string; body: unknown }[] = [];
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (init?.method === 'POST') {
      posts.push({ path, body: JSON.parse(String(init.body)) });
      return Response.json({});
    }
    return Response.json([question]);
  }));
  render(<RunQuestions run={run} refreshKey={1} onChanged={() => {}} />);
  await userEvent.click(await screen.findByRole('button', { name: '初学者' }));
  expect(posts).toHaveLength(0);
  await userEvent.click(screen.getByRole('button', { name: '提交回答' }));
  expect(posts).toEqual([{ path: '/api/runs/r/questions/q/answer', body: { expected_revision: 3, kind: 'option', value: '初学者' } }]);
});

it('keeps paused answers distinct from continuation and supports free text', async () => {
  const posts: unknown[] = [];
  vi.stubGlobal('fetch', vi.fn(async (_path: string, init?: RequestInit) => {
    if (init?.method === 'POST') { posts.push(JSON.parse(String(init.body))); return Response.json({}); }
    return Response.json([question]);
  }));
  render(<RunQuestions run={{ ...run, status: 'paused' }} refreshKey={1} onChanged={() => {}} />);
  await userEvent.type(await screen.findByRole('textbox', { name: '你的回答' }), '结合代码讲解');
  expect(screen.getByText('回答会保存，任务保持暂停，仍需明确继续。')).toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: '提交回答' }));
  expect(posts).toEqual([{ expected_revision: 3, kind: 'text', value: '结合代码讲解' }]);
});

it('reloads answered questions without enabling resubmission', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => Response.json([{ ...question, status: 'answered', answer: { kind: 'text', value: '先讲概念' } }])));
  render(<RunQuestions run={{ ...run, status: 'completed' }} refreshKey={1} onChanged={() => {}} />);
  expect(await screen.findByText('你的回答：先讲概念')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '提交回答' })).not.toBeInTheDocument();
});

it('submits typed MCP fields while the main run waits for its child', async () => {
  const posts: unknown[] = [];
  vi.stubGlobal('fetch', vi.fn(async (_path: string, init?: RequestInit) => {
    if (init?.method === 'POST') { posts.push(JSON.parse(String(init.body))); return Response.json({}); }
    return Response.json([{ ...question, kind: 'mcp', request: { question: '服务需要信息', requests: [{
      id: 'quantity', mode: 'form', message: '选择核对范围', requested_schema: {
        type: 'object', properties: {
          count: { type: 'integer', title: '条数', minimum: 1, maximum: 5 },
          detailed: { type: 'boolean', title: '详细说明' },
          depth: { type: 'string', title: '深度', enum: ['基础', '进阶'] },
        }, required: ['count', 'detailed', 'depth'],
      },
    }] } }]);
  }));
  render(<RunQuestions run={{ ...run, status: 'waiting_children' }} refreshKey={1} onChanged={() => {}} />);
  await userEvent.type(await screen.findByRole('spinbutton', { name: '条数（必填）' }), '3');
  await userEvent.selectOptions(screen.getByRole('combobox', { name: '详细说明（必填）' }), 'false');
  await userEvent.selectOptions(screen.getByRole('combobox', { name: '深度（必填）' }), '基础');
  await userEvent.click(screen.getByRole('button', { name: '提交并继续' }));
  expect(posts).toEqual([{ expected_revision: 3, kind: 'mcp', value: {
    quantity: { action: 'accept', content: { count: 3, detailed: false, depth: '基础' } },
  } }]);
});

it('URL continuation asks for acknowledgment without collecting credentials', async () => {
  const posts: unknown[] = [];
  vi.stubGlobal('fetch', vi.fn(async (_path: string, init?: RequestInit) => {
    if (init?.method === 'POST') { posts.push(JSON.parse(String(init.body))); return Response.json({}); }
    return Response.json([{ ...question, kind: 'mcp', request: { question: '服务需要确认', requests: [{
      id: 'verify', mode: 'url', message: '请核对服务状态', url: 'https://example.com/verify',
    }] } }]);
  }));
  render(<RunQuestions run={run} refreshKey={1} onChanged={() => {}} />);
  expect(await screen.findByRole('link', { name: '打开服务页面完成操作 ↗' })).toHaveAttribute('href', 'https://example.com/verify');
  expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole('button', { name: '提交并继续' }));
  expect(posts).toHaveLength(0);
  await userEvent.click(screen.getByRole('checkbox', { name: '我已在服务页面操作，继续核对' }));
  await userEvent.click(screen.getByRole('button', { name: '提交并继续' }));
  expect(posts).toEqual([{ expected_revision: 3, kind: 'mcp', value: { verify: { action: 'accept' } } }]);
});
