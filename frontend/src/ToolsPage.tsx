import { useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { messageOf, send, useResource } from './api';
import { Empty, ErrorBox, Loading, PageTitle } from './components';

type Tool = {
  id: string;
  name: string;
  description: string;
  source: string;
  group: string;
  legacy_only?: boolean;
  available?: boolean;
  requires_approval?: boolean;
  unavailable_explanations?: string[];
};
type Group = { id: string; name: string; tool_ids: string[] };
type Catalog = { tools: Tool[]; groups: Group[] };
type Policy = {
  revision: number;
  enabled: Record<string, boolean>;
  main_tools: string[] | null;
  main_tool_groups: string[] | null;
  resolved_main_tools: string[] | null;
};
type Role = { id: string; display_name: string; enabled: boolean; deleted: boolean };
type Preview = {
  policy_revision: number;
  tools: Tool[];
  role: (Role & { revision: number }) | null;
  skills: { available: boolean; explanation: string };
};
const sources: Record<string, string> = {
  builtin: '应用工具',
  middleware: '中间件工具',
  mcp: 'MCP工具',
};
const toggle = (values: string[], name: string, selected: boolean) =>
  selected ? [...values, name] : values.filter((item) => item !== name);

/** 选择表只维护用户编辑；真正可用结果由后端根据已保存配置计算。 */
function Selection({
  title,
  options,
  selected,
  onChange,
}: {
  title: string;
  options: { id: string; name: string }[];
  selected: string[];
  onChange: (value: string[]) => void;
}) {
  const choices = [
    ...options,
    ...selected
      .filter((id) => !options.some((item) => item.id === id))
      .map((id) => ({ id, name: `${id}（当前不可用，请移除）` })),
  ];
  return (
    <fieldset>
      <legend>{title}</legend>
      {choices.map((item) => (
        <label className="check" key={item.id}>
          <input
            type="checkbox"
            checked={selected.includes(item.id)}
            onChange={(event) => onChange(toggle(selected, item.id, event.target.checked))}
          />
          {item.name}
        </label>
      ))}
      {choices.length === 0 && <p className="muted">暂无可选项</p>}
    </fieldset>
  );
}

/** 修订冲突时保留输入；只有成功保存或明确放弃编辑后，父组件才刷新配置。
 * 只提交发生变化的选择字段，避免无关修改重新展开保存过的工具组。
 */
function PolicyEditor({
  saved,
  catalog,
  refreshing,
  refresh,
}: {
  saved: Policy;
  catalog: Catalog;
  refreshing: boolean;
  refresh: () => void;
}) {
  const [draft, setDraft] = useState(saved);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const inherited = draft.main_tools === null && draft.main_tool_groups === null;
  const local = catalog.tools.filter((tool) => tool.source !== 'mcp');
  const patch: Record<string, unknown> = { expected_revision: saved.revision };
  const enabled = Object.fromEntries(
    local
      .filter((tool) => draft.enabled[tool.id] !== saved.enabled[tool.id])
      .map((tool) => [tool.id, draft.enabled[tool.id]]),
  );
  if (Object.keys(enabled).length) patch.enabled = enabled;
  for (const field of ['main_tools', 'main_tool_groups'] as const) {
    if (JSON.stringify(draft[field]) !== JSON.stringify(saved[field])) patch[field] = draft[field];
  }
  const changed = Object.keys(patch).length > 1;
  async function save() {
    setBusy(true);
    setError('');
    try {
      await send('/api/tools/policy', patch, 'PATCH');
      refresh();
    } catch (failure) {
      setError(messageOf(failure));
    } finally {
      setBusy(false);
    }
  }
  return (
    <form
      className="card tool-policy"
      aria-label="工具策略配置"
      onSubmit={(event) => {
        event.preventDefault();
        if (changed && !busy && !refreshing) void save();
      }}
    >
      <h2>配置工具</h2>
      <p className="muted">当前配置修订 {saved.revision}</p>
      <p>全局关闭同时约束主 Agent 和子 Agent；主 Agent 的选择仅限制主 Agent。</p>
      <fieldset disabled={busy || refreshing}>
        <legend>全局工具开关</legend>
        <div className="tool-grid">
          {local.map((tool) => (
            <label className="check" key={tool.id}>
              <input
                type="checkbox"
                checked={draft.enabled[tool.id] === true}
                onChange={(event) =>
                  setDraft({
                    ...draft,
                    enabled: { ...draft.enabled, [tool.id]: event.target.checked },
                  })
                }
              />
              <span>
                <strong>{tool.name}</strong>
                <span className="muted small">
                  {' '}
                  — {tool.description}
                  {tool.legacy_only ? '（仅供旧任务恢复）' : ''}
                </span>
              </span>
            </label>
          ))}
        </div>
      </fieldset>
      <fieldset disabled={busy || refreshing}>
        <legend>主 Agent 的工具选择</legend>
        <label className="check">
          <input
            type="checkbox"
            checked={inherited}
            onChange={(event) =>
              setDraft({
                ...draft,
                main_tools: event.target.checked ? null : [],
                main_tool_groups: event.target.checked ? null : [],
              })
            }
          />
          继承默认本地工具选择
        </label>
        {inherited ? (
          <p className="muted small">使用默认选择，仍受全局开关和任务选项约束。</p>
        ) : (
          <>
            <p className="muted small">
              工具组与单独工具合并；两者都不选表示主 Agent 不使用本地工具。MCP 许可单独管理。
            </p>
            <div className="two-columns">
              <Selection
                title="按组选择"
                options={catalog.groups}
                selected={draft.main_tool_groups ?? []}
                onChange={(main_tool_groups) => setDraft({ ...draft, main_tool_groups })}
              />
              <Selection
                title="单独选择工具"
                options={local.filter((item) => !item.legacy_only)}
                selected={draft.main_tools ?? []}
                onChange={(main_tools) => setDraft({ ...draft, main_tools })}
              />
            </div>
          </>
        )}
      </fieldset>
      <ErrorBox message={error} />
      <div className="row wrap">
        <button type="submit" disabled={busy || refreshing || !changed}>
          {busy ? '正在保存…' : '保存工具配置'}
        </button>
        <button type="button" className="secondary" disabled={busy || refreshing} onClick={refresh}>
          放弃本地修改并刷新
        </button>
      </div>
      <p className="muted small">
        普通选择从新任务生效。关闭工具会在后续调用前检查，不能撤销已经执行的操作；重新开启不会恢复旧任务已撤回的许可。
      </p>
    </form>
  );
}

/** 预览仅发 GET，不创建任务、不连接 MCP、也不调用模型。URL 保留所选角色便于回访。 */
function EffectivePreview({ version }: { version: number }) {
  const [query, setQuery] = useSearchParams();
  const roleId = query.get('role_id') ?? '';
  const [knowledgeOnly, setKnowledgeOnly] = useState(false);
  const [auxiliary, setAuxiliary] = useState(false);
  const [delegation, setDelegation] = useState(true);
  const roles = useResource<Role[]>('/api/subagents', version);
  const params = new URLSearchParams({
    knowledge_only: String(knowledgeOnly),
    auxiliary: String(auxiliary),
    delegation: String(delegation),
  });
  if (roleId) params.set('role_id', roleId);
  const preview = useResource<Preview>(`/api/tools/effective?${params}`, version);
  return (
    <section className="card" aria-label="有效工具预览">
      <h2>有效工具预览</h2>
      <p>根据已保存的配置预览新任务。上方尚未保存的改动不在此处生效；预览不会执行任何工具。</p>
      <label>
        预览角色
        <select
          value={roleId}
          disabled={roles.loading || Boolean(roles.error)}
          onChange={(event) => {
            const next = new URLSearchParams(query);
            if (event.target.value) next.set('role_id', event.target.value);
            else next.delete('role_id');
            setQuery(next);
          }}
        >
          <option value="">主 Agent</option>
          {roles.data?.map((role) => (
            <option key={role.id} value={role.id}>
              {role.display_name}
              {role.enabled && !role.deleted ? '' : '（已停用）'}
            </option>
          ))}
          {roleId && !roles.data?.some((row) => row.id === roleId) && (
            <option value={roleId}>所选角色</option>
          )}
        </select>
      </label>
      <div className="row wrap">
        <label className="check">
          <input
            type="checkbox"
            checked={knowledgeOnly}
            onChange={(event) => setKnowledgeOnly(event.target.checked)}
          />
          仅知识库
        </label>
        <label className="check">
          <input
            type="checkbox"
            checked={auxiliary}
            onChange={(event) => setAuxiliary(event.target.checked)}
          />
          允许辅助模型
        </label>
        <label className="check">
          <input
            type="checkbox"
            checked={delegation}
            onChange={(event) => setDelegation(event.target.checked)}
          />
          允许子 Agent 分工
        </label>
      </div>
      <ErrorBox message={roles.error || preview.error} />
      {preview.loading && <Loading />}
      {preview.data && !preview.loading && !preview.error && (
        <>
          <p className="muted small">
            工具配置修订 {preview.data.policy_revision}
            {preview.data.role && ` · 角色修订 ${preview.data.role.revision}`} · 可用{' '}
            {preview.data.tools.filter((tool) => tool.available).length} 项
          </p>
          <p className="notice">
            Skills 按需读取：{preview.data.skills.available ? '可用' : '不可用'}。
            {preview.data.skills.explanation}
          </p>
          <ul className="tool-grid tool-results">
            {preview.data.tools.map((tool) => (
              <li key={tool.id}>
                <div className="row between">
                  <strong>{tool.name}</strong>
                  <span className="badge">{tool.available ? '可用' : '不可用'}</span>
                </div>
                <p>{tool.description}</p>
                <p className="muted small">{sources[tool.source] ?? tool.source}</p>
                {tool.unavailable_explanations?.map((reason) => (
                  <p className="small" key={reason}>
                    {reason}
                  </p>
                ))}
                {tool.requires_approval && <p className="small">每次调用需用户确认</p>}
              </li>
            ))}
          </ul>
          {preview.data.tools.length === 0 && <Empty title="暂无已登记工具" />}
          <p className="muted small">
            实际执行还会校验最新许可、预算和具体操作条件。旧运行保留当时的选择，可能比此预览更少。
          </p>
        </>
      )}
    </section>
  );
}

export function ToolsPage() {
  const [version, setVersion] = useState(0);
  const refresh = () => setVersion((value) => value + 1);
  const catalog = useResource<Catalog>('/api/tools/catalog', version);
  const policy = useResource<Policy>('/api/tools/policy', version);
  const skills = useResource<
    { name: string; description: string; version: string; resource: string }[]
  >('/api/skills', version);
  const [showHistory, setShowHistory] = useState(false);
  const history = useResource<Policy[]>(showHistory ? '/api/tools/policy/history' : null, version);
  return (
    <>
      <PageTitle eyebrow="能力配置" title="工具与能力" />
      <p>集中查看应用工具、Skills 读取与子任务委派。外部连接和子角色沿用各自的管理页面。</p>
      <div className="row wrap">
        <Link to="/mcp">管理MCP连接</Link>
        <Link to="/subagents">管理子Agent</Link>
      </div>
      <details className="card skill-catalog">
        <summary>已发现的任务技能{skills.data ? `（${skills.data.length}）` : ''}</summary>
        <p className="muted small">
          主 Agent 按需读取方法，子 Agent 可选择技能子集。是否允许读取仍由工具策略决定。
        </p>
        <ErrorBox message={skills.error} />
        {skills.loading && <Loading />}
        {skills.data?.map((skill) => (
          <article key={skill.name}>
            <h3>{skill.name}</h3>
            <p>{skill.description}</p>
            <p className="muted small">
              {skill.resource} · {skill.version}
            </p>
          </article>
        ))}
      </details>
      <ErrorBox message={catalog.error || policy.error} />
      {(catalog.loading || policy.loading) && <Loading />}
      {(catalog.error || policy.error) && (
        <button className="secondary" onClick={refresh}>
          重试加载配置
        </button>
      )}
      {catalog.data && policy.data && (
        <PolicyEditor
          key={`${policy.data.revision}:${version}`}
          saved={policy.data}
          catalog={catalog.data}
          refreshing={catalog.loading || policy.loading || Boolean(catalog.error || policy.error)}
          refresh={refresh}
        />
      )}
      <EffectivePreview version={version} />
      <section className="card">
        <button className="secondary" onClick={() => setShowHistory(!showHistory)}>
          {showHistory ? '收起配置历史' : '查看配置历史'}
        </button>
        {showHistory && (
          <>
            <ErrorBox message={history.error} />
            {history.loading && <Loading />}
            {history.data?.map((item) => (
              <div key={item.revision}>
                <h3>修订 {item.revision}</h3>
                <p>
                  主 Agent：
                  {item.resolved_main_tools === null
                    ? '继承默认选择'
                    : item.resolved_main_tools.length === 0
                      ? '未选择本地工具'
                      : item.resolved_main_tools.join('、')}
                </p>
                <p className="muted small">
                  全局关闭：
                  {Object.entries(item.enabled)
                    .filter(([, value]) => !value)
                    .map(([id]) => id)
                    .join('、') || '无'}
                </p>
              </div>
            ))}
          </>
        )}
      </section>
    </>
  );
}
