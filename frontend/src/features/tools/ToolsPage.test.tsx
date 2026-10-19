import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import App from '../../app/App';

const tools = [
  {
    id: 'builtin:read_source',
    name: 'read_source',
    description: '读取来源',
    source: 'builtin',
    group: 'materials_read',
  },
  {
    id: 'middleware:read_file',
    name: 'read_file',
    description: '读取技能',
    source: 'middleware',
    group: 'skills',
  },
];
function serve(conflict = false) {
  let policy = {
    revision: 0,
    enabled: { 'builtin:read_source': true, 'middleware:read_file': true },
    main_tools: null as string[] | null,
    main_tool_groups: null as string[] | null,
    resolved_main_tools: null as string[] | null,
  };
  const sent: Record<string, unknown>[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (path: string, init?: RequestInit) => {
      if (path === '/api/settings')
        return Response.json({
          model: 'offline',
          model_calls_enabled: false,
        });
      if (path === '/api/skills')
        return Response.json([
          {
            name: 'guided-learning',
            description: '学习方法',
            version: '1',
            resource: '/skills/guided-learning/SKILL.md',
            enabled: true,
          },
          {
            name: 'image-generation',
            description: '图片生成',
            version: '1',
            resource: '/skills/image-generation/SKILL.md',
            enabled: false,
            unavailable_reason: '尚未配置生成服务',
          },
        ]);
      if (path === '/api/tools/catalog')
        return Response.json({
          tools,
          groups: [
            { id: 'materials_read', name: '材料读取', tool_ids: ['builtin:read_source'] },
            { id: 'skills', name: '技能读取', tool_ids: ['middleware:read_file'] },
          ],
        });
      if (path === '/api/tools/policy' && init?.method === 'PATCH') {
        const body = JSON.parse(String(init.body));
        sent.push(body);
        if (conflict) {
          policy = { ...policy, revision: 1 };
          return Response.json({ detail: '工具策略修订已变化，请刷新' }, { status: 409 });
        }
        policy = {
          ...policy,
          ...body,
          revision: policy.revision + 1,
          enabled: { ...policy.enabled, ...body.enabled },
        };
        return Response.json(policy);
      }
      if (path === '/api/tools/policy') return Response.json(policy);
      if (path === '/api/tools/policy/history') return Response.json([policy]);
      if (path.startsWith('/api/tools/effective'))
        return Response.json({
          policy_revision: policy.revision,
          role: null,
          skills: {
            available: policy.enabled['middleware:read_file'],
            explanation: '渐进Skills依赖read_file',
          },
          tools: tools.map((item) => ({
            ...item,
            available: policy.enabled[item.id as keyof typeof policy.enabled],
            unavailable_explanations: policy.enabled[item.id as keyof typeof policy.enabled]
              ? []
              : ['全局已关闭'],
          })),
        });
      return Response.json([]);
    }),
  );
  return sent;
}
function open() {
  window.history.replaceState({}, '', '/tools');
  render(<App />);
}
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.history.replaceState({}, '', '/');
});

it('keeps the page compact and loads effective tools only when requested', async () => {
  const sent = serve();
  open();
  expect(await screen.findByRole('heading', { name: '工具与能力', level: 1 })).toBeVisible();
  expect(screen.getByRole('link', { name: '管理MCP连接' })).toHaveAttribute('href', '/mcp');
  expect(screen.getByRole('link', { name: '管理子Agent' })).toHaveAttribute('href', '/subagents');
  expect(screen.queryByRole('region', { name: '有效工具预览' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '查看配置历史' })).not.toBeInTheDocument();
  expect(
    vi.mocked(fetch).mock.calls.some(([url]) => String(url).startsWith('/api/tools/effective')),
  ).toBe(false);
  await userEvent.click(await screen.findByRole('button', { name: '查看有效工具' }));
  const preview = within(await screen.findByRole('dialog', { name: '主 Agent 的有效工具' }));
  expect(await preview.findByText('read_source')).toBeVisible();
  expect(sent).toHaveLength(0);
});

it('shows imported but unavailable skills with reasons without enabling them', async () => {
  const sent = serve();
  open();
  await userEvent.click(await screen.findByRole('button', { name: '管理全部 Skills' }));
  const picker = within(screen.getByRole('dialog', { name: 'Skills' }));
  expect(picker.getByRole('checkbox', { name: 'guided-learning' })).toBeChecked();
  expect(picker.getByRole('checkbox', { name: 'image-generation' })).toBeDisabled();
  expect(screen.getByText('尚未配置生成服务')).toBeVisible();
  expect(sent).toHaveLength(0);
});

it('persists skill switches through the versioned policy without changing deployment availability', async () => {
  const sent = serve();
  open();
  await userEvent.click(await screen.findByRole('button', { name: '管理全部 Skills' }));
  await userEvent.click(screen.getByRole('checkbox', { name: 'guided-learning' }));
  await userEvent.click(screen.getByRole('button', { name: '完成' }));
  await userEvent.click(screen.getByRole('button', { name: '保存工具配置' }));
  await waitFor(() =>
    expect(sent).toEqual([{ expected_revision: 0, skill_enabled: { 'guided-learning': false } }]),
  );
  await screen.findByText('当前配置修订 1');
  await userEvent.click(screen.getByRole('button', { name: '管理全部 Skills' }));
  expect(screen.getByRole('checkbox', { name: 'guided-learning' })).not.toBeChecked();
});

it('saves only changed global flags and refreshes the saved preview', async () => {
  const sent = serve();
  open();
  const form = within(await screen.findByRole('form', { name: '工具策略配置' }));
  await userEvent.click(form.getByRole('button', { name: '管理全部工具' }));
  const global = within(screen.getByRole('dialog', { name: '全局工具开关' }));
  await userEvent.click(global.getByRole('checkbox', { name: /read_source/ }));
  await userEvent.click(global.getByRole('button', { name: '完成' }));
  await userEvent.click(form.getByRole('button', { name: '保存工具配置' }));
  await waitFor(() =>
    expect(sent).toEqual([{ expected_revision: 0, enabled: { 'builtin:read_source': false } }]),
  );
  await screen.findByText('当前配置修订 1');
  await userEvent.click(screen.getByRole('button', { name: '查看有效工具' }));
  expect(
    await within(screen.getByRole('dialog', { name: '主 Agent 的有效工具' })).findByText(
      '全局已关闭',
    ),
  ).toBeVisible();
});

it('distinguishes default inheritance, explicit empty selection and selected groups', async () => {
  const sent = serve();
  open();
  let form = within(await screen.findByRole('form', { name: '工具策略配置' }));
  await userEvent.click(form.getByRole('checkbox', { name: '继承默认本地工具选择' }));
  await userEvent.click(form.getByRole('button', { name: '保存工具配置' }));
  await waitFor(() =>
    expect(sent[0]).toEqual({ expected_revision: 0, main_tools: [], main_tool_groups: [] }),
  );
  await screen.findByText('当前配置修订 1');
  form = within(screen.getByRole('form', { name: '工具策略配置' }));
  await userEvent.click(form.getByRole('button', { name: '选择工具组' }));
  await userEvent.click(screen.getByRole('checkbox', { name: '材料读取' }));
  await userEvent.click(screen.getByRole('button', { name: '完成' }));
  await userEvent.click(form.getByRole('button', { name: '保存工具配置' }));
  await waitFor(() =>
    expect(sent[1]).toEqual({ expected_revision: 1, main_tool_groups: ['materials_read'] }),
  );
  await screen.findByText('当前配置修订 2');
  form = within(screen.getByRole('form', { name: '工具策略配置' }));
  await userEvent.click(form.getByRole('checkbox', { name: '继承默认本地工具选择' }));
  await userEvent.click(form.getByRole('button', { name: '保存工具配置' }));
  await waitFor(() =>
    expect(sent[2]).toEqual({ expected_revision: 2, main_tools: null, main_tool_groups: null }),
  );
});

it('retains local edits on conflict and reloads only when explicitly discarded', async () => {
  const sent = serve(true);
  open();
  let form = within(await screen.findByRole('form', { name: '工具策略配置' }));
  await userEvent.click(form.getByRole('button', { name: '管理全部工具' }));
  await userEvent.click(
    within(screen.getByRole('dialog', { name: '全局工具开关' })).getByRole('checkbox', {
      name: /read_source/,
    }),
  );
  await userEvent.click(screen.getByRole('button', { name: '完成' }));
  await userEvent.click(form.getByRole('button', { name: '保存工具配置' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('修订已变化');
  await userEvent.click(form.getByRole('button', { name: '管理全部工具' }));
  expect(
    within(screen.getByRole('dialog', { name: '全局工具开关' })).getByRole('checkbox', {
      name: /read_source/,
    }),
  ).not.toBeChecked();
  expect(sent).toHaveLength(1);
  await userEvent.click(screen.getByRole('button', { name: '完成' }));
  await userEvent.click(form.getByRole('button', { name: '放弃本地修改并刷新' }));
  await screen.findByText('当前配置修订 1');
  form = within(screen.getByRole('form', { name: '工具策略配置' }));
  await userEvent.click(form.getByRole('button', { name: '管理全部工具' }));
  expect(
    within(screen.getByRole('dialog', { name: '全局工具开关' })).getByRole('checkbox', {
      name: /read_source/,
    }),
  ).toBeChecked();
});

it('does not offer a writable fallback when the policy cannot be loaded and retries explicitly', async () => {
  serve();
  const base = globalThis.fetch;
  let unavailable = true;
  vi.stubGlobal(
    'fetch',
    vi.fn((path: string, init?: RequestInit) =>
      path === '/api/tools/policy' && unavailable
        ? Promise.resolve(Response.json({ detail: '配置暂时无法读取' }, { status: 503 }))
        : base(path, init),
    ),
  );
  open();
  expect(await screen.findByRole('alert')).toHaveTextContent('配置暂时无法读取');
  expect(screen.queryByRole('form', { name: '工具策略配置' })).not.toBeInTheDocument();
  unavailable = false;
  await userEvent.click(screen.getByRole('button', { name: '重试加载配置' }));
  expect(await screen.findByRole('form', { name: '工具策略配置' })).toBeVisible();
});
