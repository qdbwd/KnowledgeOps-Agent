import { Button } from '../../shared/ui/Button';
import { useState } from 'react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { Download, Search, Pencil, ArrowLeft, BookOpen, Trash2 } from 'lucide-react';
import { messageOf, send, useResource } from '../../shared/api/api';
import {
  Empty,
  ErrorBox,
  KnowledgeEditor,
  Loading,
  Markdown,
  PageTitle,
} from '../../shared/ui/components';
import type { ChangeSet, Knowledge } from '../../shared/types/types';
import { useWorkspace } from '../../shared/state/workspace';
import { KnowledgeTabs } from './KnowledgeTabs';
import { AssetDeletionDialog, AssetDeletionHistory } from '../sources/AssetDeletion';

export function KnowledgePage() {
  const { version } = useWorkspace();
  const [query, setQuery] = useState('');
  const [search, setSearch] = useState('');
  const [archived, setArchived] = useState(false);
  const [deleting, setDeleting] = useState<Knowledge | null>(null);
  const result = useResource<Knowledge[]>(
    `/api/knowledge?query=${encodeURIComponent(search)}&include_archived=${archived}`,
    version,
  );
  return (
    <>
      <PageTitle eyebrow="YOUR KNOWLEDGE, CONNECTED" title="知识库">
        <a className="button secondary button-secondary" href="/api/knowledge/export">
          <Download size={16} />
          导出 Markdown
        </a>
      </PageTitle>
      <KnowledgeTabs />
      <div className="row flex items-center gap-3 between justify-between toolbar mb-6 flex-wrap">
        <form
          className="search flex min-w-[230px] max-w-[620px] flex-1 items-center gap-[9px] rounded-lg border border-line bg-white py-[3px] pr-[9px] pl-[15px] text-[#8fa08f] [&>input]:border-0 [&>input]:bg-transparent [&>input]:pl-0 [&>input]:text-[13px] [&>button]:whitespace-nowrap"
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
          <Button className="quiet button-quiet">检索</Button>
        </form>
        <label className="check m-0 flex-row items-center gap-2 font-normal">
          <input
            type="checkbox"
            checked={archived && !search}
            disabled={Boolean(search)}
            onChange={(e) => setArchived(e.target.checked)}
          />
          含归档（仅浏览）
        </label>
      </div>
      {search && (
        <p className="muted text-muted small text-xs">
          搜索覆盖未归档文章；清空搜索后可浏览归档内容。
        </p>
      )}
      <ErrorBox message={result.error} />
      {result.error ? null : result.loading ? (
        <Loading />
      ) : result.data?.length ? (
        <div className="article-grid grid grid-cols-[repeat(auto-fit,minmax(260px,1fr))] gap-4.5">
          {result.data.map((item) => (
            <article
              className="article-card flex flex-col rounded-[10px] border border-line bg-white p-5.5 text-[#2c4333] hover:border-[#a6bba1] hover:no-underline [&>h2]:mt-4.5 [&>h2]:mb-2.5 [&>h2]:text-[17px] [&>p]:flex-1 [&>p]:text-xs [&>p]:leading-[1.8] [&>p]:text-[#81907e] [&>p]:[overflow-wrap:anywhere]"
              key={item.id || item.document_id}
            >
              <div className="row flex items-center gap-3 between justify-between">
                <BookOpen size={18} />
                <span className="muted text-muted small text-xs">
                  {item.archived ? '已归档' : `v${item.version ?? '·'}`}
                </span>
              </div>
              <h2>
                <Link to={`/knowledge/${item.id || item.document_id}`}>{item.title}</Link>
              </h2>
              <p>{item.summary || item.snippet || item.body?.slice(0, 120)}</p>
              <div className="tags flex flex-wrap gap-[7px] [&>span]:rounded-sm [&>span]:bg-[#f1f4ec] [&>span]:px-2 [&>span]:py-[3px] [&>span]:text-[10px] [&>span]:text-[#798971]">
                {item.category && <span>{item.category}</span>}
                {item.tags?.slice(0, 3).map((tag) => (
                  <span key={tag}>#{tag}</span>
                ))}
              </div>
              <Button
                className="button-quiet mt-3 self-end text-danger"
                aria-label={`删除文章：${item.title}`}
                onClick={() => setDeleting(item)}
              >
                <Trash2 size={15} />
                删除
              </Button>
            </article>
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
      <AssetDeletionHistory kind="knowledge" />
      {deleting && (
        <AssetDeletionDialog
          kind="knowledge"
          identity={deleting.id || deleting.document_id!}
          title={deleting.title}
          onClose={() => setDeleting(null)}
        />
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
  const [deleting, setDeleting] = useState(false);
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
      navigate(`/knowledge/reviews/${draft.id}`);
    } catch (error) {
      setActionError(messageOf(error));
    } finally {
      setActionBusy(false);
    }
  }
  return (
    <>
      <Link
        className="back-link mb-[15px] inline-flex items-center gap-1.5 text-xs"
        to="/knowledge"
      >
        <ArrowLeft size={15} />
        返回知识库
      </Link>
      <PageTitle eyebrow={`KNOWLEDGE / VERSION ${item.version}`} title={item.title}>
        <div className="row flex items-center gap-3">
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
          <Button
            disabled={Boolean(item.deleted_at)}
            className="secondary button-secondary"
            onClick={() => setEditing(!editing)}
          >
            <Pencil size={15} />
            编辑
          </Button>
          {!item.deleted_at &&
            latest &&
            !latest.archived &&
            item.version_id === latest.version_id && (
              <Button
                className="secondary button-secondary"
                disabled={actionBusy}
                onClick={() => prepareAction('archive')}
              >
                归档文章
              </Button>
            )}
          {!item.deleted_at &&
            latest &&
            (latest.archived || item.version_id !== latest.version_id) && (
              <Button
                className="secondary button-secondary"
                disabled={actionBusy}
                onClick={() => prepareAction('restore')}
              >
                恢复为此版本
              </Button>
            )}
          {!item.deleted_at && (
            <Button className="button-quiet text-danger" onClick={() => setDeleting(true)}>
              <Trash2 size={15} />
              删除文章
            </Button>
          )}
        </div>
      </PageTitle>
      <ErrorBox message={actionError} />
      {item.deleted_at && (
        <p className="rounded-lg border border-line p-4 text-sm text-muted">
          这篇文章已从知识库移除。此版本因仍被引用而保留，仅供查看。
        </p>
      )}
      {deleting && (
        <AssetDeletionDialog
          kind="knowledge"
          identity={item.id}
          title={item.title}
          onClose={() => setDeleting(false)}
          onDeleted={() => navigate('/knowledge')}
        />
      )}
      {selected && selected !== history.data?.[0]?.version_id && (
        <p className="notice border-l-[3px] border-[#c0a76b] bg-[#f5f0e0] px-4 py-3 text-[13px] [overflow-wrap:anywhere]">
          正在查看历史版本。编辑保存时仍会检查当前版本，防止覆盖新内容。
        </p>
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
        <article className="card article mb-5.5 rounded-[11px] border border-line bg-white px-10 py-8 max-lg:p-[25px] max-md:p-4.5 [&>.tags]:mb-5.5">
          <div className="tags flex flex-wrap gap-[7px] [&>span]:rounded-sm [&>span]:bg-[#f1f4ec] [&>span]:px-2 [&>span]:py-[3px] [&>span]:text-[10px] [&>span]:text-[#798971]">
            {item.category && <span>{item.category}</span>}
            {item.tags.map((tag) => (
              <span key={tag}>#{tag}</span>
            ))}
            {item.archived && <span>已归档</span>}
          </div>
          <Markdown text={item.body} />
          {Boolean(item.relations?.length) && (
            <aside className="related mt-[25px] grid gap-1.5 border-t border-line pt-5 text-[13px] [&>h3]:mb-0">
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
