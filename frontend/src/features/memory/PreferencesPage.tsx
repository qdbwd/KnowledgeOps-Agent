import { Button } from '../../shared/ui/Button';
import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { api, messageOf, send, useResource } from '../../shared/api/api';
import { Empty, ErrorBox, Loading, PageTitle } from '../../shared/ui/components';
import { useWorkspace } from '../../shared/state/workspace';
import { CategoryManager, CategorySelect, type MemoryCategory } from './MemoryCategories';

type Preference = {
  id: string;
  revision: number;
  content: string;
  scope: 'global' | 'topic';
  topic: string;
  status: 'active' | 'pending' | 'inactive' | 'deleted';
  category?: string;
  display_category_id?: string | null;
  classification?: { status: string; source: string; error?: string; run_id?: string };
  needs_review?: boolean;
  source: {
    kind: string;
    quote?: string;
    conversation_id?: string;
    conflicts?: string[];
    merged_from?: { id: string; before_revision: number }[];
  };
  source_message_id?: string;
  evidence?: {
    id: string;
    message_id?: string;
    kind: string;
    quote: string;
    context_key?: string;
  }[];
};
const statusLabel = { active: '已生效', pending: '待确认', inactive: '已停用', deleted: '已删除' };
const categoryLabel: Record<string, string> = {
  preference: '表达偏好',
  background: '用户背景',
  behavior: '协作习惯',
  cognitive: '思考与学习习惯',
  goal: '持续目标',
  correction: '明确纠正',
};

const displayId = (row: Preference) =>
  row.display_category_id === undefined
    ? `memory-category-${row.category ?? 'preference'}`
    : (row.display_category_id ?? '');
const classificationMessage: Record<string, string> = {
  model_disabled: '模型调用已关闭，原文已保存。可以手动选择类别，启用模型后也可重试。',
  request_failed: '分类请求未成功，原文已保存。',
  invalid_result: '分类结果无效，原文已保存。',
  catalog_changed: '类别目录已变化，本次分类未应用。',
  model_changed: '模型配置已变化，请重新分类。',
};

/** 修改时附上当前修订号；冲突显示后端原错，用户刷新后才能覆盖较新决定。 */
function PreferenceCard({
  row,
  rows,
  onChanged,
  categories,
}: {
  row: Preference;
  rows: Preference[];
  onChanged: () => void;
  categories: MemoryCategory[];
}) {
  const [base, setBase] = useState(row);
  const [content, setContent] = useState(row.content);
  const [topic, setTopic] = useState(row.topic);
  const [scope, setScope] = useState(row.scope);
  const [category, setCategory] = useState(displayId(row));
  const [replace, setReplace] = useState('');
  const [history, setHistory] = useState<Preference[] | null>(null);
  const [evidence, setEvidence] = useState<Preference['evidence'] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const dirty =
    content !== base.content ||
    scope !== base.scope ||
    topic !== base.topic ||
    category !== displayId(base);
  function accept(latest: Preference) {
    setBase(latest);
    setContent(latest.content);
    setScope(latest.scope);
    setTopic(latest.topic);
    setCategory(displayId(latest));
    setError('');
  }
  // 后台分类/类别删除会推进修订；有本地输入时保留草稿和原修订供冲突校验。
  if (row.revision > base.revision && !dirty) accept(row);
  async function update(patch: Record<string, unknown>) {
    setBusy(true);
    setError('');
    try {
      const latest = await send<Preference>(
        `/api/memories/${row.id}`,
        { expected_revision: base.revision, ...patch },
        'PATCH',
      );
      accept(latest);
      onChanged();
    } catch (error) {
      setError(messageOf(error));
    } finally {
      setBusy(false);
    }
  }
  async function action(name: string, body: Record<string, unknown> = {}) {
    setBusy(true);
    setError('');
    try {
      await send(`/api/memories/${row.id}/${name}`, { expected_revision: base.revision, ...body });
      onChanged();
    } catch (error) {
      setError(messageOf(error));
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="card mb-5.5 rounded-[11px] border border-line bg-white p-6.5 max-md:p-4.5">
      <div className="row flex items-center gap-3 between justify-between">
        <strong>
          {categories.find((item) => item.id === displayId(row))?.name ??
            (row.display_category_id === undefined
              ? categoryLabel[row.category ?? 'preference']
              : '未分类')}{' '}
          · {statusLabel[row.status]}
        </strong>
        <span className="muted text-muted small text-xs">修订 {row.revision}</span>
      </div>
      <p className="muted text-muted small text-xs">
        来源：
        {{ manual: '手动设置', explicit: '用户明确表达', inferred: '根据对话推测' }[
          row.source.kind
        ] ?? row.source.kind}
        {row.source.conversation_id && (
          <>
            {' '}
            · <Link to={`/chat/${row.source.conversation_id}`}>原对话</Link>
          </>
        )}
      </p>
      {row.source.quote && <blockquote>{row.source.quote}</blockquote>}
      {row.needs_review && (
        <p className="notice border-l-[3px] border-[#c0a76b] bg-[#f5f0e0] px-4 py-3 text-[13px] [overflow-wrap:anywhere]">
          这条背景或目标已有一段时间未确认，请检查是否仍然适用。
        </p>
      )}
      <ErrorBox message={error} />
      {row.classification?.status === 'pending' && (
        <p role="status" className="text-sm text-muted">
          原文已保存，正在等待分类结果…
          {row.classification.run_id && (
            <>
              {' '}
              <Link to={`/runs/${row.classification.run_id}`}>查看分类任务</Link>
            </>
          )}
        </p>
      )}
      {row.classification?.error && (
        <p className="text-sm text-muted">
          {classificationMessage[row.classification.error] ?? '分类未完成，原文已保存。'}
        </p>
      )}
      {!row.display_category_id &&
        row.status !== 'deleted' &&
        row.classification &&
        row.classification.status !== 'pending' &&
        (['paused', 'interrupted'].includes(row.classification.status) &&
        row.classification.run_id ? (
          <Link to={`/runs/${row.classification.run_id}`}>分类已暂停，查看任务</Link>
        ) : (
          <Button
            type="button"
            className="button-secondary"
            disabled={busy || dirty}
            onClick={() => void action('classify', { request_key: crypto.randomUUID() })}
          >
            重新分类
          </Button>
        ))}
      {dirty && (
        <p className="text-xs text-muted">
          有未保存的修改
          {base.revision !== row.revision
            ? '；服务器已有新修订，输入已保留，保存时会检查冲突。'
            : '。'}
        </p>
      )}
      {(error || base.revision !== row.revision) && (
        <Button
          type="button"
          className="button-quiet"
          disabled={busy}
          onClick={async () => {
            try {
              accept(await api<Preference>(`/api/memories/${row.id}`));
              onChanged();
            } catch (failure) {
              setError(messageOf(failure));
            }
          }}
        >
          放弃本地修改并重新加载
        </Button>
      )}
      <label>
        记忆内容
        <textarea
          aria-label={`记忆内容 ${row.id}`}
          value={content}
          disabled={busy || row.status === 'deleted'}
          maxLength={2000}
          onChange={(e) => setContent(e.target.value)}
        />
      </label>
      <div className="row flex items-center gap-3 wrap flex-wrap">
        <CategorySelect
          categories={categories}
          value={category}
          onChange={setCategory}
          disabled={busy || row.status === 'deleted'}
        />
        <label>
          适用范围
          <select
            value={scope}
            disabled={busy || row.status === 'deleted'}
            onChange={(e) => setScope(e.target.value as typeof scope)}
          >
            <option value="global">全局</option>
            <option value="topic">指定主题</option>
          </select>
        </label>
        {scope === 'topic' && (
          <label>
            主题
            <input
              value={topic}
              maxLength={200}
              disabled={busy || row.status === 'deleted'}
              onChange={(e) => setTopic(e.target.value)}
            />
          </label>
        )}
        {row.status !== 'deleted' && (
          <Button
            disabled={busy || !dirty || !content.trim() || (scope === 'topic' && !topic.trim())}
            onClick={() =>
              void update({
                ...(content !== base.content ? { content } : {}),
                ...(category !== displayId(base) ? { display_category_id: category || null } : {}),
                ...(scope !== base.scope || topic !== base.topic
                  ? { scope, topic: scope === 'global' ? '' : topic }
                  : {}),
              })
            }
          >
            保存修改
          </Button>
        )}
      </div>
      {row.status === 'pending' && (
        <div className="row flex items-center gap-3 wrap flex-wrap">
          <label>
            确认时取代
            <select value={replace} onChange={(e) => setReplace(e.target.value)}>
              <option value="">不取代其他记忆</option>
              {rows
                .filter((item) => item.id !== row.id && item.status === 'active')
                .map((item) => (
                  <option value={item.id} key={item.id}>
                    {item.content.slice(0, 70)}
                  </option>
                ))}
            </select>
          </label>
          <Button
            disabled={busy || dirty}
            onClick={() =>
              void update({ status: 'active', ...(replace ? { supersedes_id: replace } : {}) })
            }
          >
            确认生效
          </Button>
        </div>
      )}
      <div className="row flex items-center gap-3 wrap flex-wrap">
        {row.status === 'active' && (
          <Button
            className="secondary button-secondary"
            disabled={busy || dirty}
            onClick={() => void update({ status: 'inactive' })}
          >
            停用
          </Button>
        )}
        {row.status === 'inactive' && (
          <Button
            className="secondary button-secondary"
            disabled={busy || dirty}
            onClick={() => void update({ status: 'active' })}
          >
            重新启用
          </Button>
        )}
        {row.status !== 'deleted' && (
          <Button
            className="quiet button-quiet"
            disabled={busy || dirty}
            onClick={() => void update({ status: 'deleted' })}
          >
            删除记忆
          </Button>
        )}
        <Button
          className="quiet button-quiet"
          onClick={async () => {
            try {
              setHistory(await api<Preference[]>(`/api/memories/${row.id}/history`));
            } catch (error) {
              setError(messageOf(error));
            }
          }}
        >
          查看修订记录
        </Button>
        <Button
          className="quiet button-quiet"
          onClick={async () => {
            try {
              setEvidence((await api<Preference>(`/api/memories/${row.id}`)).evidence ?? []);
            } catch (error) {
              setError(messageOf(error));
            }
          }}
        >
          查看来源证据
        </Button>
        {row.source.merged_from && row.status === 'active' && (
          <Button
            className="secondary button-secondary"
            disabled={busy || dirty}
            onClick={() => void action('undo-merge')}
          >
            撤销合并
          </Button>
        )}
      </div>
      {evidence && (
        <ul>
          {evidence.map((item) => (
            <li key={item.id}>
              {item.quote || '人工记录，没有对应的聊天原话。'}
              {item.context_key?.startsWith('conversation:') && (
                <>
                  {' '}
                  · <Link to={`/chat/${item.context_key.slice(13)}`}>来源对话</Link>
                </>
              )}
            </li>
          ))}
        </ul>
      )}
      {history && (
        <ol>
          {history.map((item) => (
            <li key={item.revision}>
              修订 {item.revision} · {statusLabel[item.status]} · {item.content}
              {row.status !== 'deleted' && item.revision !== row.revision && (
                <Button
                  className="quiet button-quiet"
                  disabled={busy || dirty}
                  onClick={() => void action('rollback', { target_revision: item.revision })}
                >
                  恢复修订 {item.revision}
                </Button>
              )}
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

export function PreferencesPage() {
  const { version, refresh } = useWorkspace();
  const [tick, setTick] = useState(0);
  const [showDeleted, setShowDeleted] = useState(false);
  const [manageCategories, setManageCategories] = useState(false);
  const categories = useResource<MemoryCategory[]>('/api/memory-categories', tick);
  const rows = useResource<Preference[]>(
    `/api/memories?include_deleted=${showDeleted}`,
    `${version}:${tick}`,
  );
  const [content, setContent] = useState('');
  const [category, setCategory] = useState('');
  const [query, setQuery] = useState('');
  const [topic, setTopic] = useState('');
  const [searchPath, setSearchPath] = useState<string | null>(null);
  const matches = useResource<Preference[]>(searchPath, tick);
  const [selected, setSelected] = useState<Record<string, number>>({});
  const [mergedContent, setMergedContent] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const pending = rows.data?.some((row) => row.classification?.status === 'pending') ?? false;
  useEffect(() => {
    if (!pending) return;
    const timer = setInterval(() => setTick((value) => value + 1), 3000);
    return () => clearInterval(timer);
  }, [pending]);
  const changed = () => {
    setTick((value) => value + 1);
    setSelected({});
    refresh();
  };
  return (
    <>
      <PageTitle eyebrow="MEMORY" title="长期记忆">
        <Button className="button-secondary" onClick={() => setManageCategories(true)}>
          管理类别
        </Button>
      </PageTitle>
      <p>
        管理你明确提供的背景、偏好、习惯和持续目标。当前任务的明确要求优先；记忆不会增加操作权限或预算。
      </p>
      <p className="muted text-muted small text-xs">
        停用或删除会停止后续注入；历史修订和原聊天仍保留，可用于核对。
      </p>
      <ErrorBox message={error || rows.error || categories.error} />
      <form
        className="card mb-5.5 rounded-[11px] border border-line bg-white p-6.5 max-md:p-4.5"
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          setError('');
          try {
            await send('/api/memories', {
              content,
              display_category_id: category || null,
              scope: 'global',
              topic: '',
            });
            setContent('');
            changed();
          } catch (error) {
            setError(messageOf(error));
          } finally {
            setBusy(false);
          }
        }}
      >
        <label>
          新增长期记忆
          <textarea
            value={content}
            disabled={busy}
            maxLength={2000}
            onChange={(e) => setContent(e.target.value)}
            placeholder="例如：讲新概念时，先解释要解决的问题，再给一个例子。"
          />
        </label>
        <CategorySelect
          value={category}
          onChange={setCategory}
          categories={categories.data ?? []}
          disabled={busy}
          automatic
        />
        <p className="text-xs text-muted">
          可以选择已有类别，也可以留空自动分类。分类失败不会影响正文保存；新类别可在“管理类别”中添加。
        </p>
        <Button disabled={busy || !content.trim()}>新增记忆</Button>
      </form>
      <form
        className="card mb-5.5 rounded-[11px] border border-line bg-white p-6.5 max-md:p-4.5 row flex items-center gap-3 wrap flex-wrap"
        onSubmit={(e) => {
          e.preventDefault();
          setSearchPath(
            `/api/memories/search?query=${encodeURIComponent(query)}&topic=${encodeURIComponent(topic)}&limit=20`,
          );
        }}
      >
        <label>
          查找相关记忆
          <input value={query} onChange={(e) => setQuery(e.target.value)} maxLength={500} />
        </label>
        <label>
          适用主题（可留空）
          <input value={topic} onChange={(e) => setTopic(e.target.value)} maxLength={200} />
        </label>
        <Button disabled={!query.trim()}>检索记忆</Button>
        {searchPath && (
          <Button type="button" className="quiet button-quiet" onClick={() => setSearchPath(null)}>
            返回全部记录
          </Button>
        )}
      </form>
      <ErrorBox message={matches.error} />
      <label className="check m-0 flex-row items-center gap-2 font-normal">
        <input
          type="checkbox"
          checked={showDeleted}
          onChange={(e) => setShowDeleted(e.target.checked)}
        />
        显示已删除记录
      </label>
      {(rows.loading || matches.loading) && <Loading />}
      {(searchPath ? matches.data : rows.data)?.length === 0 && (
        <Empty title={searchPath ? '没有匹配的有效记忆' : '还没有长期记忆'}>
          可手动添加，也可以在对话中明确告诉 Agent 你的背景或以后如何配合你。
        </Empty>
      )}
      {(searchPath ? matches.data : rows.data)?.map((row) => (
        <div key={row.id}>
          {['active', 'pending'].includes(row.status) && (
            <label className="check m-0 flex-row items-center gap-2 font-normal">
              <input
                type="checkbox"
                aria-label={`选择合并 ${row.id}`}
                checked={selected[row.id] === row.revision}
                onChange={(e) =>
                  setSelected((previous) => {
                    const next = { ...previous };
                    if (e.target.checked) next[row.id] = row.revision;
                    else delete next[row.id];
                    return next;
                  })
                }
              />
              选择合并
            </label>
          )}
          <PreferenceCard
            row={row}
            rows={rows.data ?? []}
            categories={categories.data ?? []}
            onChanged={changed}
          />
        </div>
      ))}
      {Object.keys(selected).length >= 2 && (
        <form
          className="card mb-5.5 rounded-[11px] border border-line bg-white p-6.5 max-md:p-4.5"
          onSubmit={async (e) => {
            e.preventDefault();
            setBusy(true);
            setError('');
            try {
              await send('/api/memories/merge', { revisions: selected, content: mergedContent });
              setMergedContent('');
              changed();
            } catch (error) {
              setError(messageOf(error));
            } finally {
              setBusy(false);
            }
          }}
        >
          <p>
            已选 {Object.keys(selected).length}{' '}
            条。仅可合并用途、范围和主题兼容的记录。原文与来源保留，可撤销合并。
          </p>
          <label>
            合并后的内容
            <textarea
              value={mergedContent}
              maxLength={2000}
              onChange={(e) => setMergedContent(e.target.value)}
            />
          </label>
          <Button disabled={busy || !mergedContent.trim() || Object.keys(selected).length > 10}>
            确认合并
          </Button>
          <Button type="button" className="quiet button-quiet" onClick={() => setSelected({})}>
            取消选择
          </Button>
        </form>
      )}
      {manageCategories && (
        <CategoryManager
          categories={categories.data ?? []}
          onChanged={changed}
          onClose={() => setManageCategories(false)}
        />
      )}
    </>
  );
}
