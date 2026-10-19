import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { Layers3, Pencil, Plus, Trash2 } from 'lucide-react';
import { api, messageOf, send, useResource } from '../../shared/api/api';
import { Button } from '../../shared/ui/Button';
import { Dialog } from '../../shared/ui/Dialog';
import { Empty, ErrorBox, Loading, PageTitle } from '../../shared/ui/components';
import { CapabilitySelector, capabilityName } from './CapabilitySelector';
import type { CapabilityChoice } from './CapabilitySelector';

type ToolGroup = {
  id: string;
  name: string;
  description: string;
  revision: number;
  tool_ids: string[];
};
type Impact = {
  preview_token: string;
  affected: {
    id: string;
    name: string;
    revision: number;
    added: string[];
    removed: string[];
  }[];
};
type Fields = Pick<ToolGroup, 'name' | 'description' | 'tool_ids'>;

function GroupEditor({
  group,
  deleting,
  tools,
  onClose,
  onSaved,
}: {
  group: ToolGroup | null;
  deleting: boolean;
  tools: CapabilityChoice[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const [base, setBase] = useState(group);
  const initial = (value: ToolGroup | null): Fields => ({
    name: value?.name ?? '',
    description: value?.description ?? '',
    tool_ids: value?.tool_ids ?? [],
  });
  const [draft, setDraft] = useState<Fields>(initial(group));
  const [impact, setImpact] = useState<Impact | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [discard, setDiscard] = useState(false);
  const nameInput = useRef<HTMLInputElement>(null);
  const dirty = JSON.stringify(draft) !== JSON.stringify(initial(base));
  const title = deleting ? '删除工具组' : base ? '编辑工具组' : '创建工具组';
  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
    };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [dirty]);
  function change(patch: Partial<Fields>) {
    setDraft({ ...draft, ...patch });
    setImpact(null);
  }
  function close() {
    if (busy) return;
    if (dirty) setDiscard(true);
    else onClose();
  }
  const payload = deleting ? { deleted: true } : draft;
  async function inspect() {
    if (!base || busy) return;
    setBusy(true);
    setError('');
    setImpact(null);
    try {
      setImpact(
        await send<Impact>(`/api/tools/groups/${base.id}/preview`, {
          expected_revision: base.revision,
          ...payload,
        }),
      );
    } catch (failure) {
      setError(messageOf(failure));
    } finally {
      setBusy(false);
    }
  }
  async function save() {
    if (busy || (base && !impact)) return;
    setBusy(true);
    setError('');
    try {
      if (!base) await send('/api/tools/groups', draft);
      else
        await send(
          `/api/tools/groups/${base.id}`,
          {
            expected_revision: base.revision,
            preview_token: impact!.preview_token,
            ...(deleting ? {} : draft),
          },
          deleting ? 'DELETE' : 'PATCH',
        );
      onSaved();
    } catch (failure) {
      setError(messageOf(failure));
      setImpact(null);
    } finally {
      setBusy(false);
    }
  }
  async function reload() {
    if (!base || busy) return;
    setBusy(true);
    try {
      const current = (await api<ToolGroup[]>('/api/tools/groups')).find(
        (row) => row.id === base.id,
      );
      if (!current) throw new Error('工具组已经删除，请关闭窗口并刷新列表。');
      setBase(current);
      setDraft(initial(current));
      setImpact(null);
      setError('');
    } catch (failure) {
      setError(messageOf(failure));
    } finally {
      setBusy(false);
    }
  }
  const valid = draft.name.trim().length > 0;
  const names = (ids: string[]) =>
    ids
      .map((id) => {
        const tool = tools.find((row) => row.id === id);
        return tool ? capabilityName(tool) : id;
      })
      .join('、');
  return (
    <Dialog
      title={title}
      onClose={close}
      busy={busy}
      footer={
        <>
          <Button
            type="button"
            className="secondary button-secondary"
            disabled={busy}
            onClick={close}
          >
            取消
          </Button>
          {base && (
            <Button
              type="button"
              className="secondary button-secondary"
              disabled={busy || !valid || (!dirty && !deleting)}
              onClick={() => void inspect()}
            >
              预览影响
            </Button>
          )}
          <Button
            type="button"
            disabled={busy || !valid || (Boolean(base) && !impact)}
            onClick={() => void save()}
          >
            {busy ? '处理中…' : deleting ? '确认删除工具组' : '保存工具组'}
          </Button>
        </>
      }
    >
      {deleting ? (
        <p className="mt-0">
          删除“{base?.name}”后，关联角色会移除这个组带来的选择；单独选择的工具保留。
        </p>
      ) : (
        <div className="grid gap-4">
          <label>
            工具组名称
            <input
              ref={nameInput}
              value={draft.name}
              maxLength={100}
              disabled={busy}
              onChange={(event) => change({ name: event.target.value })}
            />
          </label>
          <label>
            用途说明
            <textarea
              rows={2}
              value={draft.description}
              maxLength={500}
              disabled={busy}
              placeholder="说明适合什么任务，方便角色配置时选择"
              onChange={(event) => change({ description: event.target.value })}
            />
          </label>
          <CapabilitySelector
            title="组内工具"
            options={tools}
            selected={draft.tool_ids}
            disabled={busy}
            onChange={(tool_ids) => change({ tool_ids })}
          />
          <p className="m-0 text-xs text-muted">
            工具组用于批量选择。子智能体只取得其中允许的工具，执行仍受全局开关与审批限制。
          </p>
        </div>
      )}
      {impact && (
        <section
          className="mt-5 rounded-xl border border-line bg-canvas p-4"
          aria-label="工具组修改影响"
        >
          <h3 className="mt-0 text-sm">影响 {impact.affected.length} 个配置</h3>
          {!impact.affected.length && <p className="mb-0 text-sm">当前没有角色选择这个组。</p>}
          {impact.affected.map((row) => (
            <article key={row.id} className="border-t border-line py-3 last:pb-0">
              <strong className="text-sm">{row.name}</strong>
              <span className="ml-2 text-xs text-muted">当前修订 {row.revision}</span>
              {row.added.length > 0 && <p className="my-1 text-sm">新增：{names(row.added)}</p>}
              {row.removed.length > 0 && <p className="my-1 text-sm">移除：{names(row.removed)}</p>}
              {!row.added.length && !row.removed.length && (
                <p className="my-1 text-sm text-muted">工具清单不变，保存配置新修订。</p>
              )}
            </article>
          ))}
          <p className="mb-0 text-xs text-muted">
            新增能力只供新任务使用；撤回会在下一次执行前生效，已经完成的操作不受影响。
          </p>
        </section>
      )}
      <div className="mt-4">
        <ErrorBox message={error} />
        {error && base && (
          <Button
            type="button"
            disabled={busy}
            className="secondary button-secondary"
            onClick={() => void reload()}
          >
            放弃修改并重新加载
          </Button>
        )}
      </div>
      {discard && (
        <div className="mt-4 rounded-lg border border-line p-3" role="alert">
          <p className="mt-0">有尚未保存的修改。</p>
          <div className="flex flex-wrap gap-3">
            <Button
              type="button"
              className="secondary button-secondary"
              onClick={() => {
                setDiscard(false);
                nameInput.current?.focus();
              }}
            >
              继续编辑
            </Button>
            <Button type="button" onClick={onClose}>
              放弃修改
            </Button>
          </div>
        </div>
      )}
    </Dialog>
  );
}

export function ToolGroupsPage() {
  const [version, setVersion] = useState(0);
  const groups = useResource<ToolGroup[]>('/api/tools/groups', version);
  const catalog = useResource<{ tools: (CapabilityChoice & { source: string })[] }>(
    '/api/tools/catalog',
    version,
  );
  const [editing, setEditing] = useState<{ group: ToolGroup | null; deleting: boolean } | null>(
    null,
  );
  const [query, setQuery] = useState('');
  const tools = (catalog.data?.tools ?? []).filter((row) => row.source !== 'mcp');
  const shown = groups.data?.filter((row) => `${row.name} ${row.description}`.includes(query));
  return (
    <>
      <Link to="/tools" className="mb-3 inline-block text-sm">
        ← 工具与能力
      </Link>
      <PageTitle eyebrow="能力配置" title="工具组" />
      <div className="mb-6 flex flex-wrap items-center justify-between gap-4">
        <p className="m-0 max-w-2xl text-sm text-muted">
          把常一起使用的工具放在一个组中，供主智能体和子智能体选择。编辑前可查看对已配置角色的影响。
        </p>
        <Button
          disabled={!catalog.data || Boolean(catalog.error)}
          onClick={() => setEditing({ group: null, deleting: false })}
        >
          <Plus size={17} />
          创建工具组
        </Button>
      </div>
      <ErrorBox message={groups.error || catalog.error} />
      {(groups.loading || catalog.loading) && <Loading />}
      {(groups.error || catalog.error) && (
        <Button className="secondary button-secondary" onClick={() => setVersion((n) => n + 1)}>
          重试加载
        </Button>
      )}
      <label className="mb-5 block max-w-md">
        <span className="sr-only">搜索工具组</span>
        <input
          type="search"
          placeholder="搜索工具组或用途"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
      </label>
      <div className="grid grid-cols-2 gap-4 max-lg:grid-cols-1">
        {shown?.map((group) => (
          <article
            key={group.id}
            aria-label={group.name}
            className="min-w-0 rounded-xl border border-line bg-white p-5"
          >
            <div className="flex items-start gap-3">
              <span className="rounded-lg bg-canvas p-2 text-brand">
                <Layers3 size={19} />
              </span>
              <div className="min-w-0 flex-1">
                <h2 className="m-0 text-base [overflow-wrap:anywhere]">{group.name}</h2>
                <p className="mt-1 text-xs text-muted">{group.tool_ids.length} 项工具</p>
              </div>
              <button
                aria-label={`编辑${group.name}`}
                onClick={() => setEditing({ group, deleting: false })}
                className="rounded-lg p-2 text-muted hover:bg-canvas"
              >
                <Pencil size={16} />
              </button>
              <button
                aria-label={`删除${group.name}`}
                onClick={() => setEditing({ group, deleting: true })}
                className="rounded-lg p-2 text-muted hover:bg-[#fff1ec] hover:text-[#8b3434]"
              >
                <Trash2 size={16} />
              </button>
            </div>
            <p className="mt-1 min-h-10 text-sm text-muted">
              {group.description || '尚未填写用途说明。'}
            </p>
            <CapabilitySelector
              title={`${group.name}的工具`}
              options={tools}
              selected={group.tool_ids}
              disabled={!catalog.data}
            />
          </article>
        ))}
      </div>
      {shown?.length === 0 && <Empty title="暂无匹配的工具组" />}
      {editing && catalog.data && (
        <GroupEditor
          key={`${editing.group?.id ?? 'new'}:${editing.deleting}`}
          {...editing}
          tools={tools}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            setVersion((n) => n + 1);
          }}
        />
      )}
    </>
  );
}
