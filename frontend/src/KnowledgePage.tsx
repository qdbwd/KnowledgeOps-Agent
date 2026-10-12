import { useState } from 'react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { Download, Search, Pencil, ArrowLeft, BookOpen } from 'lucide-react';
import { messageOf, send, useResource } from './api';
import { Empty, ErrorBox, KnowledgeEditor, Loading, Markdown, PageTitle } from './components';
import type { ChangeSet, Knowledge } from './types';
import { useWorkspace } from './workspace';

export function KnowledgePage() {
  const { version } = useWorkspace();
  const [query, setQuery] = useState('');
  const [search, setSearch] = useState('');
  const [archived, setArchived] = useState(false);
  const result = useResource<Knowledge[]>(
    `/api/knowledge?query=${encodeURIComponent(search)}&include_archived=${archived}`,
    version,
  );
  return (
    <>
      <PageTitle eyebrow="YOUR KNOWLEDGE, CONNECTED" title="知识库">
        <a className="button secondary" href="/api/knowledge/export">
          <Download size={16} />
          导出 Markdown
        </a>
      </PageTitle>
      <div className="row between toolbar">
        <form
          className="search"
          onSubmit={(e) => {
            e.preventDefault();
            setSearch(query);
          }}
        >
          <Search size={18} />
          <input
            aria-label="检索知识库"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索标题、关键词与正文"
          />
          <button className="quiet">检索</button>
        </form>
        <label className="check">
          <input
            type="checkbox"
            checked={archived && !search}
            disabled={Boolean(search)}
            onChange={(e) => setArchived(e.target.checked)}
          />
          含归档（仅浏览）
        </label>
      </div>
      {search && <p className="muted small">搜索覆盖未归档文章；清空搜索后可浏览归档内容。</p>}
      <ErrorBox message={result.error} />
      {result.error ? null : result.loading ? (
        <Loading />
      ) : result.data?.length ? (
        <div className="article-grid">
          {result.data.map((item) => (
            <Link
              className="article-card"
              to={`/knowledge/${item.id || item.document_id}`}
              key={item.id || item.document_id}
            >
              <div className="row between">
                <BookOpen size={18} />
                <span className="muted small">
                  {item.archived ? '已归档' : `v${item.version ?? '·'}`}
                </span>
              </div>
              <h2>{item.title}</h2>
              <p>{item.summary || item.snippet || item.body?.slice(0, 120)}</p>
              <div className="tags">
                {item.category && <span>{item.category}</span>}
                {item.tags?.slice(0, 3).map((tag) => (
                  <span key={tag}>#{tag}</span>
                ))}
              </div>
            </Link>
          ))}
        </div>
      ) : (
        <Empty title={search ? '没有找到匹配的知识' : '还没有发布的知识文章'}>
          {search ? (
            '尝试更具体的词语，或调整检索词。'
          ) : (
            <Link to="/">通过对话整理第一篇文章 →</Link>
          )}
        </Empty>
      )}
    </>
  );
}

export function ArticlePage() {
  const navigate = useNavigate();
  const { documentId } = useParams();
  const [params, setParams] = useSearchParams();
  const { version, refresh } = useWorkspace();
  const selected = params.get('version');
  const article = useResource<Knowledge>(
    `/api/knowledge/${documentId}${selected ? `?version_id=${selected}` : ''}`,
    version,
  );
  const history = useResource<Knowledge[]>(`/api/knowledge/${documentId}/versions`, version);
  const [editing, setEditing] = useState(false);
  const [actionError, setActionError] = useState('');
  const [actionBusy, setActionBusy] = useState(false);
  if (article.loading) return <Loading />;
  if (article.error || !article.data) return <ErrorBox message={article.error || '文章不存在'} />;
  const item = article.data;
  const latest = history.data?.[0];
  async function prepareAction(action: 'archive' | 'restore') {
    if (!latest) return;
    setActionBusy(true);
    setActionError('');
    try {
      const draft = await send<ChangeSet>(`/api/knowledge/${item.id}/actions`, {
        action,
        request_key: crypto.randomUUID(),
        base_version_id: latest.version_id,
        restore_version_id: action === 'restore' ? item.version_id : null,
      });
      refresh();
      navigate(`/changes/${draft.id}`);
    } catch (error) {
      setActionError(messageOf(error));
    } finally {
      setActionBusy(false);
    }
  }
  return (
    <>
      <Link className="back-link" to="/knowledge">
        <ArrowLeft size={15} />
        返回知识库
      </Link>
      <PageTitle eyebrow={`KNOWLEDGE / VERSION ${item.version}`} title={item.title}>
        <div className="row">
          <select
            aria-label="文章历史版本"
            value={item.version_id}
            onChange={(e) => {
              setEditing(false);
              setParams({ version: e.target.value });
            }}
          >
            {history.data?.map((v) => (
              <option value={v.version_id} key={v.version_id}>
                v{v.version}
                {v.archived ? ' · 归档' : ''}
              </option>
            ))}
          </select>
          <button className="secondary" onClick={() => setEditing(!editing)}>
            <Pencil size={15} />
            编辑
          </button>
          {latest && !latest.archived && item.version_id === latest.version_id && (
            <button
              className="secondary"
              disabled={actionBusy}
              onClick={() => prepareAction('archive')}
            >
              归档文章
            </button>
          )}
          {latest && (latest.archived || item.version_id !== latest.version_id) && (
            <button
              className="secondary"
              disabled={actionBusy}
              onClick={() => prepareAction('restore')}
            >
              恢复为此版本
            </button>
          )}
        </div>
      </PageTitle>
      <ErrorBox message={actionError} />
      {selected && selected !== history.data?.[0]?.version_id && (
        <p className="notice">正在查看历史版本。编辑保存时仍会检查当前版本，防止覆盖新内容。</p>
      )}
      {editing ? (
        <KnowledgeEditor
          key={item.version_id}
          article={item}
          onCancel={() => setEditing(false)}
          onSave={async (edited) => {
            await send(`/api/knowledge/${item.id}/edits`, {
              request_key: crypto.randomUUID(),
              base_version_id: item.version_id,
              title: edited.title,
              body: edited.body,
              tags: edited.tags,
              category: edited.category,
              summary: edited.summary,
              relations: edited.relations ?? [],
            });
            setEditing(false);
            setParams({});
            refresh();
          }}
        />
      ) : (
        <article className="card article">
          <div className="tags">
            {item.category && <span>{item.category}</span>}
            {item.tags.map((tag) => (
              <span key={tag}>#{tag}</span>
            ))}
            {item.archived && <span>已归档</span>}
          </div>
          <Markdown text={item.body} />
          {Boolean(item.relations?.length) && (
            <aside className="related">
              <h3>相关阅读</h3>
              {item.relations?.map((link) => (
                <Link key={link.target_id} to={`/knowledge/${link.target_id}`}>
                  {link.label} →
                </Link>
              ))}
            </aside>
          )}
        </article>
      )}
    </>
  );
}
