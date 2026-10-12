import { useState } from 'react';
import { Link } from 'react-router-dom';
import { api, messageOf, send, useResource } from './api';
import { Empty, ErrorBox, Loading, PageTitle } from './components';
import { useWorkspace } from './workspace';

type Preference = {
  id: string;
  revision: number;
  content: string;
  scope: 'global' | 'topic';
  topic: string;
  status: 'active' | 'pending' | 'inactive' | 'deleted';
  category?: string;
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

function CategorySelect({
  value,
  onChange,
  disabled = false,
}: {
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
}) {
  return (
    <label>
      类别
      <select value={value} disabled={disabled} onChange={(e) => onChange(e.target.value)}>
        {Object.entries(categoryLabel).map(([key, title]) => (
          <option key={key} value={key}>
            {title}
          </option>
        ))}
      </select>
    </label>
  );
}

/** 修改时附上当前修订号；冲突显示后端原错，用户刷新后才能覆盖较新决定。 */
function PreferenceCard({
  row,
  rows,
  onChanged,
}: {
  row: Preference;
  rows: Preference[];
  onChanged: () => void;
}) {
  const [content, setContent] = useState(row.content);
  const [topic, setTopic] = useState(row.topic);
  const [scope, setScope] = useState(row.scope);
  const [category, setCategory] = useState(row.category ?? 'preference');
  const [replace, setReplace] = useState('');
  const [history, setHistory] = useState<Preference[] | null>(null);
  const [evidence, setEvidence] = useState<Preference['evidence'] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function update(patch: Record<string, unknown>) {
    setBusy(true);
    setError('');
    try {
      await send(`/api/memories/${row.id}`, { expected_revision: row.revision, ...patch }, 'PATCH');
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
      await send(`/api/memories/${row.id}/${name}`, { expected_revision: row.revision, ...body });
      onChanged();
    } catch (error) {
      setError(messageOf(error));
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="card">
      <div className="row between">
        <strong>
          {categoryLabel[row.category ?? 'preference']} · {statusLabel[row.status]}
        </strong>
        <span className="muted small">修订 {row.revision}</span>
      </div>
      <p className="muted small">
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
        <p className="notice">这条背景或目标已有一段时间未确认，请检查是否仍然适用。</p>
      )}
      <ErrorBox message={error} />
      <label>
        记忆内容
        <textarea
          aria-label={`记忆内容 ${row.id}`}
          value={content}
          disabled={row.status === 'deleted'}
          maxLength={2000}
          onChange={(e) => setContent(e.target.value)}
        />
      </label>
      <div className="row wrap">
        <CategorySelect
          value={category}
          onChange={setCategory}
          disabled={row.status === 'deleted'}
        />
        <label>
          适用范围
          <select
            value={scope}
            disabled={row.status === 'deleted'}
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
              disabled={row.status === 'deleted'}
              onChange={(e) => setTopic(e.target.value)}
            />
          </label>
        )}
        {row.status !== 'deleted' && (
          <button
            disabled={busy || !content.trim() || (scope === 'topic' && !topic.trim())}
            onClick={() =>
              void update({ content, category, scope, topic: scope === 'global' ? '' : topic })
            }
          >
            保存修改
          </button>
        )}
      </div>
      {row.status === 'pending' && (
        <div className="row wrap">
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
          <button
            disabled={busy}
            onClick={() =>
              void update({ status: 'active', ...(replace ? { supersedes_id: replace } : {}) })
            }
          >
            确认生效
          </button>
        </div>
      )}
      <div className="row wrap">
        {row.status === 'active' && (
          <button
            className="secondary"
            disabled={busy}
            onClick={() => void update({ status: 'inactive' })}
          >
            停用
          </button>
        )}
        {row.status === 'inactive' && (
          <button
            className="secondary"
            disabled={busy}
            onClick={() => void update({ status: 'active' })}
          >
            重新启用
          </button>
        )}
        {row.status !== 'deleted' && (
          <button
            className="quiet"
            disabled={busy}
            onClick={() => void update({ status: 'deleted' })}
          >
            删除记忆
          </button>
        )}
        <button
          className="quiet"
          onClick={async () => {
            try {
              setHistory(await api<Preference[]>(`/api/memories/${row.id}/history`));
            } catch (error) {
              setError(messageOf(error));
            }
          }}
        >
          查看修订记录
        </button>
        <button
          className="quiet"
          onClick={async () => {
            try {
              setEvidence((await api<Preference>(`/api/memories/${row.id}`)).evidence ?? []);
            } catch (error) {
              setError(messageOf(error));
            }
          }}
        >
          查看来源证据
        </button>
        {row.source.merged_from && row.status === 'active' && (
          <button className="secondary" disabled={busy} onClick={() => void action('undo-merge')}>
            撤销合并
          </button>
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
                <button
                  className="quiet"
                  disabled={busy}
                  onClick={() => void action('rollback', { target_revision: item.revision })}
                >
                  恢复修订 {item.revision}
                </button>
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
  const rows = useResource<Preference[]>(
    `/api/memories?include_deleted=${showDeleted}`,
    `${version}:${tick}`,
  );
  const [content, setContent] = useState('');
  const [category, setCategory] = useState('preference');
  const [query, setQuery] = useState('');
  const [topic, setTopic] = useState('');
  const [searchPath, setSearchPath] = useState<string | null>(null);
  const matches = useResource<Preference[]>(searchPath, tick);
  const [selected, setSelected] = useState<Record<string, number>>({});
  const [mergedContent, setMergedContent] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const changed = () => {
    setTick((value) => value + 1);
    setSelected({});
    refresh();
  };
  return (
    <>
      <PageTitle eyebrow="MEMORY" title="长期记忆" />
      <p>
        管理你明确提供的背景、偏好、习惯和持续目标。当前任务的明确要求优先；记忆不会增加操作权限或预算。
      </p>
      <p className="muted small">停用或删除会停止后续注入；历史修订和原聊天仍保留，可用于核对。</p>
      <ErrorBox message={error || rows.error} />
      <form
        className="card"
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          setError('');
          try {
            await send('/api/memories', { content, category, scope: 'global', topic: '' });
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
            maxLength={2000}
            onChange={(e) => setContent(e.target.value)}
            placeholder="例如：讲新概念时，先解释要解决的问题，再给一个例子。"
          />
        </label>
        <CategorySelect value={category} onChange={setCategory} />
        <button disabled={busy || !content.trim()}>新增记忆</button>
      </form>
      <form
        className="card row wrap"
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
        <button disabled={!query.trim()}>检索记忆</button>
        {searchPath && (
          <button type="button" className="quiet" onClick={() => setSearchPath(null)}>
            返回全部记录
          </button>
        )}
      </form>
      <ErrorBox message={matches.error} />
      <label className="check">
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
        <div key={`${row.id}:${row.revision}`}>
          {['active', 'pending'].includes(row.status) && (
            <label className="check">
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
          <PreferenceCard row={row} rows={rows.data ?? []} onChanged={changed} />
        </div>
      ))}
      {Object.keys(selected).length >= 2 && (
        <form
          className="card"
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
            条。仅可合并相同类别、范围和主题的记录；原文与来源保留，可撤销。
          </p>
          <label>
            合并后的内容
            <textarea
              value={mergedContent}
              maxLength={2000}
              onChange={(e) => setMergedContent(e.target.value)}
            />
          </label>
          <button disabled={busy || !mergedContent.trim() || Object.keys(selected).length > 10}>
            确认合并
          </button>
          <button type="button" className="quiet" onClick={() => setSelected({})}>
            取消选择
          </button>
        </form>
      )}
    </>
  );
}
