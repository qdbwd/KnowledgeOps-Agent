import { useState } from 'react';
import type { ReactNode } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Link } from 'react-router-dom';
import { AlertCircle, ArrowRight, LoaderCircle } from 'lucide-react';
import { messageOf, statusText } from './api';
import type { Change, ChangeSet, Knowledge } from './types';

/** 保留正文内联引用，转到来源查看页；材料中的原始 HTML 不执行。 */
export function Markdown({
  text,
  embeddedEvidence = false,
}: {
  text: string;
  embeddedEvidence?: boolean;
}) {
  return (
    <div className="markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        skipHtml
        components={{
          a: ({ href, children }) => {
            // 文件下载走 HTTP，不能由 SPA 路由截获成一个不存在的页面。
            if (href?.startsWith('/api/workspace/download?')) {
              return (
                <a href={href} download>
                  {children}
                </a>
              );
            }
            if (embeddedEvidence && href?.startsWith('/api/evidence/')) {
              return <a href={`#evidence-${href.slice('/api/evidence/'.length)}`}>{children}</a>;
            }
            const local = href?.replace(/^\/api\/evidence\//, '/evidence/');
            return local?.startsWith('/') ? (
              <Link to={local}>{children}</Link>
            ) : (
              <a href={href} target="_blank" rel="noreferrer noopener">
                {children}
              </a>
            );
          },
          img: ({ src, alt }) => (
            <a href={src} target="_blank" rel="noreferrer noopener">
              [图片：{alt || '查看原图'}]
            </a>
          ),
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}

export function ErrorBox({ message }: { message?: string }) {
  return message ? (
    <div className="error-box" role="alert">
      <AlertCircle size={18} />
      <span>{message}</span>
    </div>
  ) : null;
}
export function Loading() {
  return (
    <div className="loading" role="status">
      <LoaderCircle className="spin" size={18} />
      正在加载…
    </div>
  );
}
export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="empty">
      <h3>{title}</h3>
      {children && <p>{children}</p>}
    </div>
  );
}
export function Badge({ status }: { status: string }) {
  return <span className={`badge status-${status}`}>{statusText[status] ?? status}</span>;
}
export function PageTitle({
  eyebrow,
  title,
  children,
}: {
  eyebrow: string;
  title: string;
  children?: ReactNode;
}) {
  return (
    <header className="page-title">
      <div>
        <p className="eyebrow">{eyebrow}</p>
        <h1>{title}</h1>
      </div>
      {children}
    </header>
  );
}

/** 编辑器保留本地输入直到服务确认成功；冲突时不能清空用户修改。 */
export function KnowledgeEditor({
  article,
  onSave,
  onCancel,
}: {
  article: Knowledge;
  onSave: (edited: Knowledge) => Promise<void>;
  onCancel: () => void;
}) {
  const [value, setValue] = useState(article);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [preview, setPreview] = useState(false);
  async function save() {
    setBusy(true);
    setError('');
    try {
      await onSave(value);
    } catch (error) {
      setError(messageOf(error));
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="editor card">
      <div className="row between">
        <h2>
          编辑文章 <span className="muted">基于 v{article.version}</span>
        </h2>
        <button className="quiet" onClick={() => setPreview(!preview)}>
          {preview ? '返回编辑' : '预览正文'}
        </button>
      </div>
      <ErrorBox message={error} />
      <label>
        标题
        <input
          value={value.title}
          onChange={(e) => setValue({ ...value, title: e.target.value })}
        />
      </label>
      <div className="two-columns">
        <label>
          分类
          <input
            value={value.category}
            onChange={(e) => setValue({ ...value, category: e.target.value })}
          />
        </label>
        <label>
          标签（逗号分隔）
          <input
            value={value.tags.join(', ')}
            onChange={(e) =>
              setValue({
                ...value,
                tags: e.target.value
                  .split(/[,，]/)
                  .map((s) => s.trim())
                  .filter(Boolean),
              })
            }
          />
        </label>
      </div>
      {preview ? (
        <Markdown text={value.body} />
      ) : (
        <label>
          Markdown 正文
          <textarea
            className="code-input"
            rows={20}
            value={value.body}
            onChange={(e) => setValue({ ...value, body: e.target.value })}
          />
        </label>
      )}
      <div className="row">
        <button onClick={save} disabled={busy || !value.title.trim() || !value.body.trim()}>
          {busy ? '保存中…' : '保存新版本'}
        </button>
        <button className="secondary" disabled={busy} onClick={onCancel}>
          取消编辑
        </button>
      </div>
    </section>
  );
}

const actionLabels: Record<string, string> = {
  create: '新增',
  update: '修订',
  archive: '归档',
  restore: '恢复',
};

/** 结构性变更需要用户看见受影响文章并勾选；服务端仍会校验同一修订号。 */
export function ChangeReview({
  draft,
  busy,
  onPublish,
  onReject,
  onEdit,
}: {
  draft: ChangeSet;
  busy: boolean;
  onPublish: (confirmImpact: boolean) => Promise<void>;
  onReject: () => Promise<void>;
  onEdit: (operations: Change[]) => Promise<void>;
}) {
  const [confirmed, setConfirmed] = useState(false);
  const [editing, setEditing] = useState(false);
  const [operations, setOperations] = useState(draft.operations);
  const pending = ['draft', 'approved'].includes(draft.status);
  return (
    <div className="review">
      <div className="card">
        <div className="row between">
          <h2>
            变更概览 <span className="muted">修订 {draft.revision}</span>
          </h2>
          <Badge status={draft.status} />
        </div>
        <p className="muted">
          共 {draft.operations.length} 篇文章。
          {draft.proactive ? '这是 Agent 主动提出的维护建议。' : '以下是本次任务准备的完整变更。'}
        </p>
        {draft.operations.map((item) => (
          <div className="impact-row" key={item.document_id}>
            <span>{actionLabels[item.action]}</span>
            <strong>
              {draft.after?.[item.document_id]?.title ||
                item.title ||
                draft.before[item.document_id]?.title ||
                item.document_id}
            </strong>
            <ArrowRight size={14} />
          </div>
        ))}
      </div>
      {operations.map((item, index) => (
        <section className="card article" key={item.document_id}>
          <h2>
            {actionLabels[item.action]} ·{' '}
            {draft.after?.[item.document_id]?.title ||
              item.title ||
              draft.before[item.document_id]?.title}
          </h2>
          {editing && ['create', 'update'].includes(item.action) ? (
            <>
              <label>
                文章标题
                <input
                  value={item.title}
                  onChange={(e) =>
                    setOperations(
                      operations.map((op, i) =>
                        i === index ? { ...op, title: e.target.value } : op,
                      ),
                    )
                  }
                />
              </label>
              <label>
                草稿正文
                <textarea
                  rows={16}
                  value={item.body}
                  onChange={(e) =>
                    setOperations(
                      operations.map((op, i) =>
                        i === index ? { ...op, body: e.target.value } : op,
                      ),
                    )
                  }
                />
              </label>
            </>
          ) : (
            <Markdown
              text={
                draft.after?.[item.document_id]?.body ??
                (item.body || draft.before[item.document_id]?.body || '此操作保留历史版本。')
              }
            />
          )}
          <details>
            <summary>查看与原文的差异</summary>
            <pre className="diff">
              {draft.differences.find((d) => d.document_id === item.document_id)?.diff ||
                '没有正文差异'}
            </pre>
          </details>
        </section>
      ))}
      {pending && (
        <div className="review-actions card">
          {draft.requires_impact_confirmation && (
            <label className="check">
              <input
                type="checkbox"
                checked={confirmed}
                onChange={(e) => setConfirmed(e.target.checked)}
              />
              我已核对以上受影响文章，确认合并、拆分或归档范围
            </label>
          )}
          <div className="row wrap">
            {editing ? (
              <>
                <button
                  disabled={busy}
                  onClick={() =>
                    void onEdit(operations)
                      .then(() => setEditing(false))
                      .catch(() => {})
                  }
                >
                  保存草稿修订
                </button>
                <button
                  className="secondary"
                  onClick={() => {
                    setOperations(draft.operations);
                    setEditing(false);
                  }}
                >
                  取消编辑
                </button>
              </>
            ) : (
              <>
                <button
                  disabled={busy || (draft.requires_impact_confirmation && !confirmed)}
                  onClick={() => onPublish(confirmed)}
                >
                  确认并发布
                </button>
                <button className="secondary" disabled={busy} onClick={() => setEditing(true)}>
                  编辑草稿
                </button>
                <button className="quiet danger" disabled={busy} onClick={onReject}>
                  拒绝本次变更
                </button>
              </>
            )}
          </div>
          <p className="muted small">
            发布会创建新版本。已有历史和来源引用保留；拒绝后 Agent 无法再自动发布这份草稿。
          </p>
        </div>
      )}
    </div>
  );
}
