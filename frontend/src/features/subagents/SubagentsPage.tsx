import { useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { messageOf, send, useResource } from '../../shared/api/api';
import { Empty, ErrorBox, Loading, PageTitle } from '../../shared/ui/components';

type Role = {
  id: string;
  name: string;
  display_name: string;
  description: string;
  system_prompt: string;
  tools: string[];
  tool_groups?: string[] | null;
  resolved_tools?: string[] | null;
  skills: string[];
  enabled: boolean;
  deleted: boolean;
  revision: number;
  authorization_epoch: number;
  builtin: boolean;
};
type Option = { name: string; description: string; label?: string };
type Options = {
  tools: Option[];
  skills: Option[];
  tool_groups?: { id: string; name: string; tool_ids: string[]; description?: string }[];
};
type Fields = Pick<
  Role,
  'name' | 'display_name' | 'description' | 'system_prompt' | 'tools' | 'skills'
> & { tool_groups: string[] | null };
const emptyFields: Fields = {
  name: '',
  display_name: '',
  description: '',
  system_prompt: '',
  tools: [],
  tool_groups: null,
  skills: [],
};
function fieldsOf(row: Role): Fields {
  return {
    name: row.name,
    display_name: row.display_name,
    description: row.description,
    system_prompt: row.system_prompt,
    tools: row.tools,
    tool_groups: row.tool_groups ?? null,
    skills: row.skills,
  };
}

function Choices({
  title,
  options,
  selected,
  disabled,
  onChange,
}: {
  title: string;
  options: Option[];
  selected: string[];
  disabled: boolean;
  onChange: (value: string[]) => void;
}) {
  // 已保存但不再提供的选项仍显示，允许用户明确移除，不能静默扩大或替换许可。
  const available: Option[] = [
    ...options,
    ...selected
      .filter((name) => !options.some((item) => item.name === name))
      .map((name) => ({ name, description: '当前已不可用，请取消选择后保存' })),
  ];
  return (
    <div role="group" aria-label={title}>
      <h3>{title}</h3>
      {available.length === 0 && <p className="muted">暂无可选项。</p>}
      {available.map((item) => (
        <label className="check" key={item.name}>
          <input
            type="checkbox"
            checked={selected.includes(item.name)}
            disabled={disabled}
            onChange={(event) =>
              onChange(
                event.target.checked
                  ? [...selected, item.name]
                  : selected.filter((name) => name !== item.name),
              )
            }
          />
          <span>
            {item.label ?? item.name}{' '}
            {item.description && <span className="muted">— {item.description}</span>}
          </span>
        </label>
      ))}
      <p className="muted small">
        已选 {selected.length} 项；空列表表示不授予任何{title}。
      </p>
    </div>
  );
}

function RoleFields({
  value,
  onChange,
  options,
  disabled,
  creating = false,
}: {
  value: Fields;
  onChange: (value: Fields) => void;
  options: Options;
  disabled: boolean;
  creating?: boolean;
}) {
  const selectedGroups = value.tool_groups ?? (
    options.tool_groups?.some((group) => group.id === 'mcp') ? ['mcp'] : []
  );
  return (
    <>
      <div className="two-columns">
        <label>
          稳定名称
          <input
            value={value.name}
            readOnly={!creating}
            disabled={disabled}
            required
            placeholder="例如 fact-checker"
            onChange={(event) => onChange({ ...value, name: event.target.value })}
          />
        </label>
        <label>
          显示名称
          <input
            value={value.display_name}
            disabled={disabled}
            required
            onChange={(event) => onChange({ ...value, display_name: event.target.value })}
          />
        </label>
      </div>
      {creating && (
        <p className="muted small">
          名称为 1–64
          位小写英文、数字或连字符，首位为英文或数字。创建后不可修改，删除后也不复用；显示名称可调整。
        </p>
      )}
      <label>
        用途说明
        <input
          value={value.description}
          disabled={disabled}
          required
          onChange={(event) => onChange({ ...value, description: event.target.value })}
        />
      </label>
      <label>
        角色提示
        <textarea
          rows={4}
          value={value.system_prompt}
          disabled={disabled}
          required
          onChange={(event) => onChange({ ...value, system_prompt: event.target.value })}
        />
      </label>
      <div className="two-columns">
        <Choices
          title="工具组"
          options={(options.tool_groups ?? []).map((group) => ({
            name: group.id,
            label: group.name,
            description: group.description ?? '',
          }))}
          selected={selectedGroups}
          disabled={disabled}
          onChange={(tool_groups) => onChange({ ...value, tool_groups })}
        />
        <Choices
          title="工具"
          options={options.tools}
          selected={value.tools}
          disabled={disabled}
          onChange={(tools) => onChange({ ...value, tools })}
        />
        <Choices
          title="Skills"
          options={options.skills}
          selected={value.skills}
          disabled={disabled}
          onChange={(skills) => onChange({ ...value, skills })}
        />
      </div>
      {selectedGroups.length > 0 && (
        <p className="muted small">
          所选组的当前成员：
          {(options.tool_groups ?? [])
            .filter((group) => selectedGroups.includes(group.id))
            .flatMap((group) => group.tool_ids)
            .map((id) => id.split(':').slice(1).join(':'))
            .join('、') || '当前不可用'}
          。 修改工具选择时重新保存组成员；只修改提示不会增加已有许可。
        </p>
      )}
      {selectedGroups.includes('mcp') && (
        <p className="muted small">
          MCP 成员在新任务开始时取自已允许的只读服务目录；可在“工具与能力”查看。
          不增加外部服务或写入权限，既有任务保持原冻结范围。
        </p>
      )}
      <p className="muted small">
        工具组和单独选择的工具合并，Skills
        需另行选择。核心系统规则始终生效；联网与辅助模型还受主任务开关限制。
      </p>
    </>
  );
}

function RoleContent({ row }: { row: Role }) {
  return (
    <>
      <p>{row.description}</p>
      <p className="muted small">
        已保存工具：{(row.resolved_tools ?? row.tools).join('、') || '无'} · Skills：
        {row.skills.join('、') || '无'}
      </p>
      {(row.tool_groups ?? []).length > 0 && (
        <p className="muted small">工具组：{row.tool_groups?.join('、')}</p>
      )}
      <pre>{row.system_prompt}</pre>
    </>
  );
}

function RoleCard({
  row,
  options,
  changed,
  showHistory,
}: {
  row: Role;
  options: Options | null;
  changed: () => void;
  showHistory: (id: string) => void;
}) {
  const [fields, setFields] = useState(() => fieldsOf(row));
  const [editing, setEditing] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function act(body: Record<string, unknown>) {
    setBusy(true);
    setError('');
    try {
      await send(
        `/api/subagents/${encodeURIComponent(row.id)}`,
        { expected_revision: row.revision, ...body },
        'PATCH',
      );
      setEditing(false);
      setConfirming(false);
      changed();
      if (body.deleted) showHistory(row.id);
    } catch (error) {
      setError(messageOf(error));
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="card" aria-label={row.display_name}>
      <div className="row wrap between">
        <h2>{row.display_name}</h2>
        <span>
          {row.builtin ? '内置 · 只读 · ' : ''}
          {row.enabled ? '已启用' : '已停用'} · 修订 {row.revision}
        </span>
      </div>
      <p className="muted small">
        <code>{row.name}</code>
      </p>
      <p>{row.description}</p>
      <ErrorBox message={error} />
      {error && (
        <button
          className="secondary"
          disabled={busy}
          onClick={() => {
            setFields(fieldsOf(row));
            setEditing(false);
            setConfirming(false);
            setError('');
            changed();
          }}
        >
          放弃本地修改并刷新
        </button>
      )}
      {busy && <p role="status">正在保存配置…</p>}
      <div className="row wrap">
        {!row.builtin && (
          <>
            <button
              className="secondary"
              disabled={busy || !options}
              onClick={() => setEditing(!editing)}
            >
              {editing ? '收起编辑' : '编辑配置'}
            </button>
            <button
              className="secondary"
              disabled={busy}
              onClick={() => void act({ enabled: !row.enabled })}
            >
              {row.enabled ? '停用' : '启用'}
            </button>
            <button className="quiet danger" disabled={busy} onClick={() => setConfirming(true)}>
              删除
            </button>
          </>
        )}
        <button className="secondary" onClick={() => showHistory(row.id)}>
          查看历史
        </button>
        <Link to={`/tools?role_id=${encodeURIComponent(row.id)}`}>查看有效工具</Link>
      </div>
      {confirming && (
        <div className="notice">
          <p>
            确认删除「{row.display_name}」（{row.name}）？旧绑定将停止，配置历史保留，名称不可复用。
          </p>
          <div className="row wrap">
            <button
              className="danger secondary"
              disabled={busy}
              onClick={() => void act({ deleted: true })}
            >
              确认删除此角色
            </button>
            <button className="secondary" disabled={busy} onClick={() => setConfirming(false)}>
              取消删除
            </button>
          </div>
        </div>
      )}
      {editing && options ? (
        <form
          aria-label={`编辑 ${row.display_name}`}
          onSubmit={(event) => {
            event.preventDefault();
            const { display_name, description, system_prompt } = fields;
            const body: Record<string, unknown> = { display_name, description, system_prompt };
            const previous = fieldsOf(row);
            // 省略未变化的选择字段，保留此前保存的组展开结果，避免目录更新后隐式扩权。
            for (const field of ['tools', 'tool_groups', 'skills'] as const) {
              if (JSON.stringify(fields[field]) !== JSON.stringify(previous[field]))
                body[field] = fields[field];
            }
            void act(body);
          }}
        >
          <RoleFields value={fields} onChange={setFields} options={options} disabled={busy} />
          <button disabled={busy}>保存配置</button>
        </form>
      ) : (
        <details>
          <summary>查看提示与许可</summary>
          <RoleContent row={row} />
        </details>
      )}
    </section>
  );
}

export function SubagentsPage() {
  const [version, setVersion] = useState(0);
  const [search, setSearch] = useSearchParams();
  const result = useResource<Role[]>('/api/subagents', version);
  const options = useResource<Options>('/api/subagents/options', version);
  const historyId = search.get('history');
  const history = useResource<Role[]>(
    historyId ? `/api/subagents/${encodeURIComponent(historyId)}/history` : null,
    version,
  );
  const [fields, setFields] = useState<Fields>(emptyFields);
  const [enabled, setEnabled] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const changed = () => setVersion((value) => value + 1);
  const showHistory = (id: string) => setSearch({ history: id });
  const optionsReady = !options.loading && !options.error ? options.data : null;
  return (
    <>
      <PageTitle eyebrow="SUBAGENTS" title="子Agent">
        <button className="secondary" onClick={changed}>
          刷新
        </button>
      </PageTitle>
      <Link to="/tools">返回工具与能力</Link>
      <p>
        为不同研究分工保存角色提示、工具和
        Skills。配置不会自动启动任务；聊天中勾选“允许子Agent分工”后，主 Agent 才可按需委派。
      </p>
      <p className="muted">所有子Agent只读、无递归，继承主任务的模型并共享其预算，最多两个并发。</p>
      <p className="muted">
        新任务使用新配置。停用、删除、收窄工具或 Skills
        许可，都会让旧绑定在下一次调用时停止；重新启用也不会恢复旧绑定。
      </p>
      <ErrorBox message={result.error} />
      <ErrorBox message={options.error} />
      {(result.loading || options.loading) && <Loading />}
      {!result.loading && result.data?.length === 0 && (
        <Empty title="尚无子Agent配置">可在下方创建角色，明确选择允许使用的工具与 Skills。</Empty>
      )}
      {result.data?.map((row) => (
        <RoleCard
          key={`${row.id}:${row.revision}`}
          row={row}
          options={optionsReady}
          changed={changed}
          showHistory={showHistory}
        />
      ))}
      {historyId && (
        <section className="card" aria-label="配置历史">
          <div className="row between">
            <h2>配置历史</h2>
            <button className="quiet" onClick={() => setSearch({})}>
              关闭历史
            </button>
          </div>
          <p className="muted">历史快照只供核对，包含已删除角色；当前地址可用于再次查看。</p>
          <ErrorBox message={history.error} />
          {history.loading && <Loading />}
          {!history.loading && history.data?.length === 0 && <p>暂无历史版本。</p>}
          {history.data?.map((row) => (
            <article className="card" key={row.revision}>
              <h3>
                {row.display_name} · 修订 {row.revision}
              </h3>
              <p className="muted small">
                {row.name} · {row.deleted ? '已删除' : row.enabled ? '已启用' : '已停用'}
              </p>
              <RoleContent row={row} />
            </article>
          ))}
        </section>
      )}
      <section className="card">
        <h2>创建子Agent</h2>
        <ErrorBox message={error} />
        <form
          aria-label="创建子Agent"
          onSubmit={async (event) => {
            event.preventDefault();
            setError('');
            setBusy(true);
            try {
              const { tool_groups, ...individual } = fields;
              // null 沿用旧显式 tools 语义；新用户选择了组才提交该字段。
              await send('/api/subagents', {
                ...individual,
                ...(tool_groups === null ? {} : { tool_groups }),
                enabled,
              });
              setFields(emptyFields);
              setEnabled(false);
              changed();
            } catch (error) {
              setError(messageOf(error));
            } finally {
              setBusy(false);
            }
          }}
        >
          <RoleFields
            value={fields}
            onChange={setFields}
            options={optionsReady ?? { tools: [], skills: [] }}
            disabled={busy || !optionsReady}
            creating
          />
          <label className="check">
            <input
              type="checkbox"
              checked={enabled}
              disabled={busy}
              onChange={(event) => setEnabled(event.target.checked)}
            />
            创建后启用
          </label>
          <button disabled={busy || !optionsReady || result.loading || Boolean(result.error)}>
            {busy ? '正在创建…' : '创建子Agent'}
          </button>
        </form>
      </section>
    </>
  );
}
