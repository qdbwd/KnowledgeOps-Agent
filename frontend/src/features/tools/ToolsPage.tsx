import { useEffect, useRef, useState } from 'react';
import { Link, Navigate, useLocation, useSearchParams } from 'react-router-dom';
import { messageOf, send, useResource } from '../../shared/api/api';
import { Button } from '../../shared/ui/Button';
import { ErrorBox, Loading, PageTitle } from '../../shared/ui/components';
import { ExecutionRules } from '../permissions/ExecutionRules';
import { CapabilitySelector, type CapabilityChoice } from './CapabilitySelector';
import { EffectiveCapabilities } from './EffectiveCapabilities';

type Tool = CapabilityChoice & { source: string; legacy_only?: boolean };
type Group = CapabilityChoice & { tool_ids: string[] };
type Catalog = { tools: Tool[]; groups: Group[] };
type Policy = {
  revision: number;
  enabled: Record<string, boolean>;
  skill_enabled?: Record<string, boolean>;
  main_tools: string[] | null;
  main_tool_groups: string[] | null;
  resolved_main_tools: string[] | null;
};
type Skill = CapabilityChoice & {
  enabled?: boolean;
  available?: boolean;
  availability_status?: string;
};

/** 只提交变更项；保存冲突不丢弃草稿，也不重新展开未修改的组选择。 */
function PolicyEditor({
  saved,
  catalog,
  skills,
  refreshing,
  refresh,
}: {
  saved: Policy;
  catalog: Catalog;
  skills: Skill[];
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
  const skillEnabled = Object.fromEntries(
    skills
      .filter(
        (skill) =>
          (draft.skill_enabled?.[skill.name] ?? true) !==
          (saved.skill_enabled?.[skill.name] ?? true),
      )
      .map((skill) => [skill.name, draft.skill_enabled?.[skill.name] ?? true]),
  );
  if (Object.keys(skillEnabled).length) patch.skill_enabled = skillEnabled;
  for (const field of ['main_tools', 'main_tool_groups'] as const) {
    if (JSON.stringify(draft[field]) !== JSON.stringify(saved[field])) patch[field] = draft[field];
  }
  const changed = Object.keys(patch).length > 1;
  useEffect(() => {
    if (!changed) return;
    const warn = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [changed]);
  const skillOptions = skills.map((skill) => {
    const available = skill.available ?? !skill.unavailable_reason;
    return {
      ...skill,
      id: skill.name,
      disabled: !available,
      detail_url: `/api/skills/${encodeURIComponent(skill.name)}`,
      status: !available
        ? '不可用'
        : (draft.skill_enabled?.[skill.name] ?? true)
          ? '已启用'
          : '已关闭',
    };
  });
  const groups = catalog.groups
    .filter((group) => group.id !== 'mcp')
    .map((group) => ({
      ...group,
      summary: group.description,
      members: local.filter((tool) => group.tool_ids.includes(tool.id)),
    }));
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
      aria-label="工具策略配置"
      className="mt-5 space-y-5"
      onSubmit={(event) => {
        event.preventDefault();
        if (changed && !busy && !refreshing) void save();
      }}
    >
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="m-0 text-xs text-muted">
          当前配置修订 {saved.revision}
          {changed ? ' · 有未保存修改' : ''}
        </p>
        <EffectiveCapabilities version={saved.revision} />
      </div>
      <CapabilitySelector
        title="全局工具开关"
        options={local}
        selected={local.filter((tool) => draft.enabled[tool.id] === true).map((tool) => tool.id)}
        disabled={busy || refreshing}
        buttonLabel="管理全部工具"
        description="全局关闭同时约束主 Agent 和子 Agent。"
        onChange={(selected) =>
          setDraft({
            ...draft,
            enabled: {
              ...draft.enabled,
              ...Object.fromEntries(local.map((tool) => [tool.id, selected.includes(tool.id)])),
            },
          })
        }
      />
      <CapabilitySelector
        title="Skills"
        options={skillOptions}
        selected={skillOptions
          .filter((skill) => !skill.disabled && (draft.skill_enabled?.[skill.name] ?? true))
          .map((skill) => skill.id)}
        disabled={busy || refreshing}
        buttonLabel="管理全部 Skills"
        description={`已发现 ${skills.length} 项。技能提供任务方法；按需读取，不会自动运行脚本或授予权限。`}
        onChange={(selected) =>
          setDraft({
            ...draft,
            skill_enabled: {
              ...draft.skill_enabled,
              ...Object.fromEntries(
                skillOptions
                  .filter((skill) => !skill.disabled)
                  .map((skill) => [skill.name, selected.includes(skill.id)]),
              ),
            },
          })
        }
      />
      <section
        className="rounded-xl border border-line bg-white p-5"
        aria-label="主 Agent 的工具选择"
      >
        <h2 className="mt-0 text-base font-semibold">主 Agent 的工具选择</h2>
        <label className="m-0 flex-row items-center gap-2 text-sm font-normal">
          <input
            type="checkbox"
            checked={inherited}
            disabled={busy || refreshing}
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
        <p className="text-xs text-muted">
          {inherited
            ? '使用默认选择，仍受全局开关和任务选项约束。'
            : '工具组与单独工具合并；两者都不选表示不使用本地工具。MCP 许可单独管理。'}
        </p>
        {!inherited && (
          <div className="grid grid-cols-2 gap-4 max-md:grid-cols-1">
            <CapabilitySelector
              title="工具组"
              options={groups}
              selected={draft.main_tool_groups ?? []}
              disabled={busy || refreshing}
              onChange={(main_tool_groups) => setDraft({ ...draft, main_tool_groups })}
            />
            <CapabilitySelector
              title="单独工具"
              options={local.filter((item) => !item.legacy_only)}
              selected={draft.main_tools ?? []}
              disabled={busy || refreshing}
              onChange={(main_tools) => setDraft({ ...draft, main_tools })}
            />
          </div>
        )}
        <Link className="text-sm" to="/tools/groups">
          创建或编辑工具组
        </Link>
      </section>
      <ErrorBox message={error} />
      <div className="sticky bottom-0 z-10 flex flex-wrap items-center gap-3 rounded-xl border border-line bg-white/95 p-4 shadow-sm backdrop-blur-sm">
        <Button type="submit" disabled={busy || refreshing || !changed}>
          {busy ? '正在保存…' : '保存工具配置'}
        </Button>
        <Button
          type="button"
          className="secondary button-secondary"
          disabled={busy || refreshing}
          onClick={refresh}
        >
          放弃本地修改并刷新
        </Button>
      </div>
      <p className="text-xs text-muted">
        新选择用于新任务。关闭会限制后续调用，不能撤销已执行操作；重新开启不会恢复旧任务已撤回的许可。
      </p>
    </form>
  );
}

function ToolsManagement() {
  const location = useLocation();
  const rules = useRef<HTMLDetailsElement>(null);
  const [version, setVersion] = useState(0);
  const refresh = () => setVersion((value) => value + 1);
  const catalog = useResource<Catalog>('/api/tools/catalog', version);
  const policy = useResource<Policy>('/api/tools/policy', version);
  const skills = useResource<Skill[]>('/api/skills', version);
  const loading = catalog.loading || policy.loading || skills.loading;
  useEffect(() => {
    // 聊天入口直接定位规则；普通工具页仍保持折叠，兼容旧会话参数链接。
    if (loading || !rules.current) return;
    if (
      location.hash !== '#execution-rules' &&
      !new URLSearchParams(location.search).has('conversation_id')
    )
      return;
    rules.current.open = true;
    rules.current.scrollIntoView?.({ block: 'start' });
  }, [loading, location.hash, location.search]);
  return (
    <>
      <PageTitle eyebrow="能力配置" title="工具与能力" />
      <p className="text-muted">
        管理可用工具与任务方法，为主 Agent 和子 Agent 设置清晰的能力范围。
      </p>
      <nav className="mb-5 flex flex-wrap gap-4" aria-label="能力管理入口">
        <Link to="/skills">管理技能包</Link>
        <Link to="/mcp">管理MCP连接</Link>
        <Link to="/tools/groups">管理工具组</Link>
        <Link to="/subagents">管理子Agent</Link>
      </nav>
      <ErrorBox message={catalog.error || policy.error || skills.error} />
      {(catalog.loading || policy.loading || skills.loading) && <Loading />}
      {(catalog.error || policy.error || skills.error) && (
        <Button className="secondary button-secondary" onClick={refresh}>
          重试加载配置
        </Button>
      )}
      {catalog.data && policy.data && (
        <PolicyEditor
          key={`${policy.data.revision}:${version}`}
          saved={policy.data}
          catalog={catalog.data}
          skills={skills.data ?? []}
          refreshing={
            catalog.loading ||
            policy.loading ||
            skills.loading ||
            Boolean(catalog.error || policy.error || skills.error)
          }
          refresh={refresh}
        />
      )}
      <details
        ref={rules}
        id="execution-rules"
        className="mt-7 scroll-mt-4 rounded-xl border border-line bg-white p-5"
      >
        <summary className="cursor-pointer font-medium">命令执行规则</summary>
        <div className="mt-4">
          <ExecutionRules />
        </div>
      </details>
    </>
  );
}

export function ToolsPage() {
  const [query] = useSearchParams();
  const role = query.get('role_id');
  return role ? (
    <Navigate to={`/subagents?effective=${encodeURIComponent(role)}`} replace />
  ) : (
    <ToolsManagement />
  );
}
