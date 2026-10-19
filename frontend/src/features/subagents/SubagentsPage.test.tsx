import '@testing-library/jest-dom/vitest';
import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import App from '../../app/App';

const builtin = {
  id: 'builtin-researcher',
  name: 'researcher',
  display_name: '内置研究员',
  description: '内置只读研究角色',
  system_prompt: '依据材料进行研究。',
  tools: ['read_source'],
  skills: [] as string[],
  enabled: true,
  deleted: false,
  revision: 1,
  authorization_epoch: 1,
  builtin: true,
};
const custom = {
  ...builtin,
  id: 'role-audit',
  name: 'auditor',
  display_name: '证据核对员',
  description: '核对事实与证据',
  system_prompt: '逐项核对条件。',
  skills: ['evidence-check'],
  enabled: false,
  revision: 3,
  builtin: false,
};
const options = {
  tool_groups: [{ id: 'materials_read', name: '材料读取', tool_ids: ['builtin:read_source'] }],
  tools: [
    { name: 'read_source', description: '读取来源' },
    { name: 'search_knowledge', description: '搜索知识' },
  ],
  skills: [{ name: 'evidence-check', description: '核对证据' }],
};
type Role = typeof builtin & { tool_groups?: string[] | null; resolved_tools?: string[] };
type Request = { url: string; body: Record<string, unknown>; method: string };
function serve(initial: Role[] = [], conflict = false, mcp = false) {
  let rows = initial;
  const sent: Request[] = [];
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method) {
      const body = JSON.parse(String(init.body));
      sent.push({ url, body, method: init.method });
      if (conflict) return Response.json({ detail: '配置已更新，请刷新后重试' }, { status: 409 });
      if (init.method === 'POST') {
        const role = { ...custom, ...body, id: 'new-role', revision: 1 };
        rows = [...rows, role];
        return Response.json(role, { status: 201 });
      }
      const current = rows.find((row) => url.endsWith(`/${row.id}`))!;
      const updated = { ...current, ...body, revision: current.revision + 1 };
      rows = rows
        .map((row) => (row.id === current.id ? updated : row))
        .filter((row) => !row.deleted);
      return Response.json(updated);
    }
    if (url === '/api/settings')
      return Response.json({
        model: 'offline',
        model_calls_enabled: false,
      });
    if (url === '/api/subagents/options')
      return Response.json({
        ...options,
        tool_groups: [
          ...options.tool_groups,
          ...(mcp ? [{ id: 'mcp', name: '继承只读 MCP', tool_ids: [] }] : []),
        ],
      });
    if (url === '/api/subagents') return Response.json(rows);
    if (url === '/api/subagents/templates')
      return Response.json([
        {
          id: 'researcher',
          display_name: '只读研究者',
          description: '可选研究模板',
          fields: {
            name: 'researcher',
            display_name: '只读研究者',
            description: '研究材料',
            system_prompt: '依据材料进行研究。',
            tools: ['read_source'],
            tool_groups: [],
            skills: [],
            enabled: false,
          },
        },
      ]);
    if (url.endsWith('/history'))
      return Response.json([{ ...custom, revision: 1, system_prompt: '最早的说明' }, custom]);
    if (url.startsWith('/api/subagents/')) {
      const row = rows.find((item) => url.endsWith(`/${item.id}`));
      return row
        ? Response.json(row)
        : Response.json({ detail: '子Agent角色不存在' }, { status: 404 });
    }
    return Response.json([]);
  });
  vi.stubGlobal('fetch', fetcher);
  return { sent, fetcher };
}
function open(path = '/subagents') {
  window.history.replaceState({}, '', path);
  render(<App />);
}
async function createForm() {
  if (!screen.queryByRole('form', { name: '创建子Agent' }))
    await userEvent.click(await screen.findByRole('link', { name: '新建子Agent' }));
  return within(await screen.findByRole('form', { name: '创建子Agent' }));
}
async function fillCreate() {
  const form = await createForm();
  await userEvent.type(form.getByLabelText('稳定名称'), 'fact-checker');
  await userEvent.type(form.getByLabelText('显示名称'), '事实核对');
  await userEvent.type(form.getByLabelText('用途说明'), '核对材料');
  await userEvent.type(form.getByLabelText('角色提示'), '只按材料核对。');
  return form;
}
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.history.replaceState({}, '', '/');
});

it('creates a role with a tool group without silently selecting individual tools', async () => {
  const { sent } = serve([]);
  open();
  const form = await fillCreate();
  await userEvent.click(form.getByRole('button', { name: '选择工具组' }));
  await userEvent.click(screen.getByRole('checkbox', { name: '材料读取' }));
  await userEvent.click(screen.getByRole('button', { name: '完成' }));
  await userEvent.click(form.getByRole('button', { name: '创建子Agent' }));
  await waitFor(() => expect(sent).toHaveLength(1));
  expect(sent[0].body).toMatchObject({
    tools: [],
    tool_groups: ['materials_read'],
    skills: [],
    enabled: false,
  });
});

it('shows inherited MCP by default and persists an explicit removal', async () => {
  const { sent } = serve([], false, true);
  open();
  const form = await fillCreate();
  await userEvent.click(form.getByRole('button', { name: '选择工具组' }));
  const option = screen.getByRole('checkbox', { name: '继承只读 MCP' });
  expect(option).toBeChecked();
  await userEvent.click(option);
  await userEvent.click(screen.getByRole('button', { name: '完成' }));
  await userEvent.click(form.getByRole('button', { name: '创建子Agent' }));
  await waitFor(() => expect(sent).toHaveLength(1));
  expect(sent[0].body.tool_groups).toEqual([]);
});

it('preserves frozen group membership when only editing the prompt and links to the effective preview', async () => {
  const { sent } = serve([
    { ...custom, tool_groups: ['materials_read'], resolved_tools: ['read_source'] },
  ]);
  open();
  const card = within(await screen.findByRole('region', { name: '证据核对员' }));
  expect(card.getByRole('button', { name: '查看有效工具' })).toBeVisible();
  await userEvent.click(card.getByRole('link', { name: '编辑配置' }));
  const form = within(await screen.findByRole('form', { name: '编辑 证据核对员' }));
  await userEvent.click(form.getByRole('button', { name: '选择工具组' }));
  expect(screen.getByRole('checkbox', { name: '材料读取' })).toBeChecked();
  await userEvent.click(screen.getByRole('button', { name: '完成' }));
  await userEvent.clear(form.getByLabelText('角色提示'));
  await userEvent.type(form.getByLabelText('角色提示'), '仅修改说明。');
  await userEvent.click(form.getByRole('button', { name: '保存配置' }));
  await waitFor(() => expect(sent).toHaveLength(1));
  expect(sent[0].body).toEqual({
    expected_revision: 3,
    display_name: custom.display_name,
    description: custom.description,
    system_prompt: '仅修改说明。',
  });
});

it('opens a concise directory with optional templates and no forced role or inline form', async () => {
  serve();
  open('/preferences');
  await userEvent.click(screen.getByRole('link', { name: '子Agent' }));
  expect(await screen.findByRole('heading', { name: '子Agent', level: 1 })).toBeVisible();
  expect(await screen.findByText('尚无子Agent配置')).toBeVisible();
  expect(screen.getByRole('link', { name: '使用只读研究者模板' })).toBeVisible();
  expect(screen.queryByRole('form')).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '查看历史' })).not.toBeInTheDocument();
  expect(screen.getByText(/只读、无递归/)).toBeVisible();
  expect(screen.getByText(/继承主任务的模型/)).toBeVisible();
  expect(screen.getByText(/新任务使用新配置/)).toBeVisible();
});

it('creates only explicitly selected tools and skills, with no implicit defaults', async () => {
  const { sent } = serve([]);
  open();
  expect(await screen.findByText('尚无子Agent配置')).toBeVisible();
  const form = await fillCreate();
  await userEvent.click(form.getByRole('button', { name: '选择工具' }));
  expect(screen.getByRole('checkbox', { name: /read_source/ })).not.toBeChecked();
  await userEvent.click(screen.getByRole('checkbox', { name: /search_knowledge/ }));
  await userEvent.click(screen.getByRole('button', { name: '完成' }));
  await userEvent.click(form.getByRole('button', { name: '选择Skills' }));
  expect(screen.getByRole('checkbox', { name: /evidence-check/ })).not.toBeChecked();
  await userEvent.click(screen.getByRole('checkbox', { name: /evidence-check/ }));
  await userEvent.click(screen.getByRole('button', { name: '完成' }));
  await userEvent.click(form.getByRole('checkbox', { name: '创建后启用' }));
  await userEvent.click(form.getByRole('button', { name: '创建子Agent' }));
  await waitFor(() => expect(sent).toHaveLength(1));
  expect(sent[0]).toEqual({
    url: '/api/subagents',
    method: 'POST',
    body: {
      name: 'fact-checker',
      display_name: '事实核对',
      description: '核对材料',
      system_prompt: '只按材料核对。',
      tools: ['search_knowledge'],
      skills: ['evidence-check'],
      enabled: true,
    },
  });
  expect(await screen.findByRole('region', { name: '事实核对' })).toBeVisible();
  expect(screen.queryByRole('form', { name: '创建子Agent' })).not.toBeInTheDocument();
});

it('sends empty tool and skill allowlists as empty, including when editing an existing role', async () => {
  const { sent } = serve([custom]);
  open();
  const card = within(await screen.findByRole('region', { name: '证据核对员' }));
  await userEvent.click(card.getByRole('link', { name: '编辑配置' }));
  const form = within(await screen.findByRole('form', { name: '编辑 证据核对员' }));
  expect(form.getByLabelText('稳定名称')).toHaveValue('auditor');
  expect(form.getByLabelText('稳定名称')).toHaveAttribute('readonly');
  await userEvent.click(form.getByRole('button', { name: '选择工具' }));
  await userEvent.click(screen.getByRole('checkbox', { name: /read_source/ }));
  await userEvent.click(screen.getByRole('button', { name: '完成' }));
  await userEvent.click(form.getByRole('button', { name: '选择Skills' }));
  await userEvent.click(screen.getByRole('checkbox', { name: /evidence-check/ }));
  await userEvent.click(screen.getByRole('button', { name: '完成' }));
  await userEvent.clear(form.getByLabelText('角色提示'));
  await userEvent.type(form.getByLabelText('角色提示'), '只分析已分配材料。');
  await userEvent.click(form.getByRole('button', { name: '保存配置' }));
  await waitFor(() => expect(sent).toHaveLength(1));
  expect(sent[0]).toEqual({
    url: '/api/subagents/role-audit',
    method: 'PATCH',
    body: {
      expected_revision: 3,
      display_name: custom.display_name,
      description: custom.description,
      system_prompt: '只分析已分配材料。',
      tools: [],
      skills: [] as string[],
    },
  });
});

it('preserves unsaved edits after a revision conflict and reloads only on explicit action', async () => {
  const { sent, fetcher } = serve([custom], true);
  open();
  const card = within(await screen.findByRole('region', { name: '证据核对员' }));
  await userEvent.click(card.getByRole('link', { name: '编辑配置' }));
  const form = within(await screen.findByRole('form', { name: '编辑 证据核对员' }));
  await userEvent.clear(form.getByLabelText('角色提示'));
  await userEvent.type(form.getByLabelText('角色提示'), '保留我的修改');
  await userEvent.click(form.getByRole('button', { name: '保存配置' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('配置已更新，请刷新后重试');
  expect(form.getByLabelText('角色提示')).toHaveValue('保留我的修改');
  expect(sent).toHaveLength(1);
  expect(fetcher.mock.calls.filter(([url]) => url === '/api/subagents')).toHaveLength(1);
  await userEvent.click(screen.getByRole('button', { name: '放弃本地修改并刷新' }));
  await userEvent.click(screen.getByRole('button', { name: '确认放弃并刷新' }));
  await waitFor(() =>
    expect(
      fetcher.mock.calls.filter(
        ([url, init]) => url === '/api/subagents/role-audit' && !init?.method,
      ),
    ).toHaveLength(2),
  );
});

it('toggles a precise revision and requires confirmation naming the role before tombstone deletion', async () => {
  const { sent } = serve([custom]);
  open();
  let card = within(await screen.findByRole('region', { name: '证据核对员' }));
  await userEvent.click(card.getByRole('button', { name: '启用' }));
  await waitFor(() => expect(sent).toHaveLength(1));
  expect(sent[0].body).toEqual({ expected_revision: 3, enabled: true });
  await screen.findByRole('button', { name: '停用' });
  card = within(screen.getByRole('region', { name: '证据核对员' }));
  await userEvent.click(card.getByRole('button', { name: '删除' }));
  expect(screen.getByText(/确认删除「证据核对员」（auditor）/)).toBeVisible();
  expect(sent).toHaveLength(1);
  await userEvent.click(screen.getByRole('button', { name: '取消删除' }));
  expect(sent).toHaveLength(1);
  await userEvent.click(card.getByRole('button', { name: '删除' }));
  await userEvent.click(screen.getByRole('button', { name: '确认删除此角色' }));
  await waitFor(() => expect(sent).toHaveLength(2));
  expect(sent[1]).toEqual({
    url: '/api/subagents/role-audit',
    method: 'PATCH',
    body: { expected_revision: 4, deleted: true },
  });
  await waitFor(() =>
    expect(screen.queryByRole('region', { name: '证据核对员' })).not.toBeInTheDocument(),
  );
  expect(screen.queryByRole('region', { name: '配置历史' })).not.toBeInTheDocument();
  expect(window.location.search).toBe('');
});

it('loads immutable history on demand and restores its URL even if the role was deleted', async () => {
  const { fetcher } = serve([custom]);
  open();
  const card = within(await screen.findByRole('region', { name: '证据核对员' }));
  expect(fetcher.mock.calls.some(([url]) => url.endsWith('/history'))).toBe(false);
  expect(card.queryByRole('button', { name: '查看历史' })).not.toBeInTheDocument();
  cleanup();
  serve([]);
  open('/subagents?history=role-audit');
  expect(await screen.findByText('最早的说明')).toBeVisible();
});

it('shows list errors and prevents editing when the options catalog cannot load', async () => {
  let retryList = false;
  let retryOptions = false;
  const { fetcher } = serve([]);
  const normalFetch = fetcher.getMockImplementation()!;
  fetcher.mockImplementation(async (url, init) => {
    if (url === '/api/subagents' && !retryList)
      return Response.json({ detail: '角色目录暂不可用' }, { status: 503 });
    if (url === '/api/subagents/options' && !retryOptions)
      return Response.json({ detail: '选项目录暂不可用' }, { status: 503 });
    return normalFetch(url, init);
  });
  open();
  expect(await screen.findByRole('alert')).toHaveTextContent('角色目录暂不可用');
  retryList = true;
  await userEvent.click(screen.getByRole('button', { name: '刷新' }));
  expect(await screen.findByText('尚无子Agent配置')).toBeVisible();
  await userEvent.click(screen.getByRole('link', { name: '新建子Agent' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('选项目录暂不可用');
  expect(screen.queryByRole('form')).not.toBeInTheDocument();
  retryOptions = true;
  await userEvent.click(screen.getByRole('button', { name: '重试加载' }));
  expect(await screen.findByRole('form', { name: '创建子Agent' })).toBeVisible();
});
it('keeps a rejected creation intact and sends genuinely empty allowlists when no option is selected', async () => {
  const { fetcher, sent } = serve([]);
  const normalFetch = fetcher.getMockImplementation()!;
  fetcher.mockImplementation(async (url, init) => {
    if (init?.method === 'POST') {
      sent.push({ url, body: JSON.parse(String(init.body)), method: 'POST' });
      return Response.json({ detail: '此稳定名称已被使用' }, { status: 409 });
    }
    return normalFetch(url, init);
  });
  open();
  const form = await fillCreate();
  await userEvent.click(form.getByRole('button', { name: '创建子Agent' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('此稳定名称已被使用');
  expect(sent[0].body).toMatchObject({ tools: [], skills: [] as string[], enabled: false });
  expect(form.getByLabelText('稳定名称')).toHaveValue('fact-checker');
  expect(form.getByLabelText('角色提示')).toHaveValue('只按材料核对。');
});

it('shows a history loading failure and can retry without changing any configuration', async () => {
  const { fetcher, sent } = serve([]);
  const normalFetch = fetcher.getMockImplementation()!;
  let resolveHistory!: (response: Response) => void;
  let retry = false;
  fetcher.mockImplementation(async (url, init) => {
    if (url.endsWith('/history'))
      return retry
        ? Response.json([])
        : new Promise<Response>((resolve) => {
            resolveHistory = resolve;
          });
    return normalFetch(url, init);
  });
  open('/subagents?history=removed-role');
  const history = within(await screen.findByRole('region', { name: '配置历史' }));
  expect(history.getByRole('status')).toHaveTextContent('正在加载');
  resolveHistory(Response.json({ detail: '历史读取失败' }, { status: 503 }));
  expect(await history.findByRole('alert')).toHaveTextContent('历史读取失败');
  retry = true;
  await userEvent.click(screen.getByRole('button', { name: '刷新' }));
  expect(await history.findByText('暂无历史版本。')).toBeVisible();
  expect(sent).toHaveLength(0);
});

it('opens a template without writing and saves it as an editable ordinary role', async () => {
  const { sent } = serve();
  open();
  await userEvent.click(await screen.findByRole('link', { name: '使用只读研究者模板' }));
  const form = within(await screen.findByRole('form', { name: '创建子Agent' }));
  expect(form.getByLabelText('稳定名称')).toHaveValue('researcher');
  expect(form.getByLabelText('创建后启用')).not.toBeChecked();
  expect(sent).toHaveLength(0);
  await userEvent.type(form.getByLabelText('显示名称'), '甲');
  await userEvent.click(form.getByRole('button', { name: '创建子Agent' }));
  const card = within(await screen.findByRole('region', { name: '只读研究者甲' }));
  expect(card.getByRole('link', { name: '编辑配置' })).toBeVisible();
  expect(card.getByRole('button', { name: '删除' })).toBeVisible();
  expect(sent[0].body.name).toBe('researcher');
});

it('guards sidebar navigation and cancel without saving or losing a draft silently', async () => {
  const { sent } = serve();
  open();
  const form = await fillCreate();
  await userEvent.click(screen.getByRole('link', { name: '工具与能力' }));
  const guard = within(screen.getByRole('dialog', { name: '离开编辑页面？' }));
  await userEvent.click(guard.getByRole('button', { name: '继续编辑' }));
  expect(form.getByLabelText('稳定名称')).toHaveValue('fact-checker');
  expect(window.location.pathname).toBe('/subagents/new');
  await userEvent.click(form.getByRole('button', { name: '取消' }));
  await userEvent.click(screen.getByRole('button', { name: '放弃修改并离开' }));
  expect(await screen.findByText('尚无子Agent配置')).toBeVisible();
  expect(sent).toHaveLength(0);
});

it('does not treat an absent or historical role as a new editable role', async () => {
  serve();
  open('/subagents/missing/edit');
  expect(await screen.findByRole('alert')).toHaveTextContent('子Agent角色不存在');
  expect(screen.queryByRole('form')).not.toBeInTheDocument();
  cleanup();
  serve([builtin]);
  open('/subagents/builtin-researcher/edit');
  expect(await screen.findByText(/这是旧任务使用的固定定义/)).toBeVisible();
  expect(screen.queryByRole('form')).not.toBeInTheDocument();
});
