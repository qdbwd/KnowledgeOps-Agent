import { Button } from './Button';
import { useState } from 'react';
import type { ReactNode } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Link } from 'react-router-dom';
import { AlertCircle, ArrowRight, LoaderCircle } from 'lucide-react';
import { messageOf, statusText } from '../api/api';
import type { Change, ChangeSet, Knowledge } from '../types/types';
import { EvidenceLink } from './EvidenceLink';

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
            const delivery = href?.match(/^\/api\/deliveries\/([a-f0-9-]+)\/download(#L\d+)?$/);
            if (delivery)
              return <Link to={`/deliveries/${delivery[1]}${delivery[2] ?? ''}`}>{children}</Link>;
            // 交付文件先进入全文阅读器；下载按钮在阅读器中仍走原HTTP接口。
            if (href?.startsWith('/api/workspace/download?')) {
              return (
                <Link to={href.replace('/api/workspace/download?', '/files?')}>{children}</Link>
              );
            }
            if (embeddedEvidence && href?.startsWith('/api/evidence/')) {
              return <a href={`#evidence-${href.slice('/api/evidence/'.length)}`}>{children}</a>;
            }
            if (href?.startsWith('/api/evidence/')) {
              return (
                <EvidenceLink id={href.slice('/api/evidence/'.length)}>{children}</EvidenceLink>
              );
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
    <div
      className="error-box mb-4 flex items-start gap-[9px] rounded-[7px] border border-[#efd1c6] bg-[#fff1ec] px-3.5 py-[11px] text-[13px] text-[#8b3434] [overflow-wrap:anywhere] [&>svg]:mt-0.5 [&>svg]:shrink-0"
      role="alert"
    >
      <AlertCircle size={18} />
      <span>{message}</span>
    </div>
  ) : null;
}
export function Loading() {
  return (
    <div
      className="loading flex items-center justify-center gap-2.5 px-5 py-10 text-sm text-[#7c8c7e]"
      role="status"
    >
      <LoaderCircle
        className="spin animate-spin [animation-duration:1.5s] motion-reduce:animate-none"
        size={18}
      />
      正在加载…
    </div>
  );
}
export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="empty my-5 rounded-[10px] border border-dashed border-[#ced9ce] px-[25px] py-[55px] text-center text-[#809080] [&>h3]:text-base [&>h3]:text-[#516354] [&>p]:mb-0 [&>p]:text-[13px]">
      <h3>{title}</h3>
      {children && <p>{children}</p>}
    </div>
  );
}
export function Badge({ status }: { status: string }) {
  // 完整的静态类名供 Tailwind 构建扫描；不根据服务端状态拼接颜色类。
  const tone = ['running', 'published', 'answered'].includes(status)
    ? 'bg-[#e7f1e8] text-[#347044]'
    : ['failed', 'rejected'].includes(status)
      ? 'bg-[#f8e9e4] text-[#a44b40]'
      : ['interrupted', 'budget_exhausted', 'draft', 'draft_ready'].includes(status)
        ? 'bg-[#f5eedc] text-[#96772d]'
        : 'bg-[#edf1eb] text-[#5f7765]';
  return (
    <span
      className={`badge inline-flex items-center rounded-[5px] px-[9px] py-[3px] text-[11px] leading-[1.6] whitespace-nowrap status-${status} ${tone}`}
    >
      {statusText[status] ?? status}
    </span>
  );
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
    <header className="page-title mb-7 flex items-center justify-between gap-5 max-lg:items-start max-md:mb-5 max-md:flex-col max-md:gap-3">
      <div>
        <p className="eyebrow mb-2 text-[10px] font-[650] tracking-[1.9px] text-[#86968b] max-md:text-[9px]">
          {eyebrow}
        </p>
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
    <section className="editor [&_.row_h2]:mb-0 [&_.row.between]:mb-5 card mb-5.5 rounded-[11px] border border-line bg-white p-6.5 max-md:p-4.5">
      <div className="row flex items-center gap-3 between justify-between">
        <h2>
          编辑文章 <span className="muted text-muted">基于 v{article.version}</span>
        </h2>
        <Button className="quiet button-quiet" onClick={() => setPreview(!preview)}>
          {preview ? '返回编辑' : '预览正文'}
        </Button>
      </div>
      <ErrorBox message={error} />
      <label>
        标题
        <input
          value={value.title}
          onChange={(e) => setValue({ ...value, title: e.target.value })}
        />
      </label>
      <div className="two-columns grid grid-cols-2 gap-5.5 max-lg:gap-[15px] max-md:grid-cols-1">
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
            className="code-input font-mono text-[13px]"
            rows={20}
            value={value.body}
            onChange={(e) => setValue({ ...value, body: e.target.value })}
          />
        </label>
      )}
      <div className="row flex items-center gap-3">
        <Button onClick={save} disabled={busy || !value.title.trim() || !value.body.trim()}>
          {busy ? '保存中…' : '保存新版本'}
        </Button>
        <Button className="secondary button-secondary" disabled={busy} onClick={onCancel}>
          取消编辑
        </Button>
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
      <div className="card mb-5.5 rounded-[11px] border border-line bg-white p-6.5 max-md:p-4.5">
        <div className="row flex items-center gap-3 between justify-between">
          <h2>
            变更概览 <span className="muted text-muted">修订 {draft.revision}</span>
          </h2>
          <Badge status={draft.status} />
        </div>
        <p className="muted text-muted">
          共 {draft.operations.length} 篇文章。
          {draft.proactive ? '这是 Agent 主动提出的维护建议。' : '以下是本次任务准备的完整变更。'}
        </p>
        {draft.operations.map((item) => (
          <div
            className="impact-row flex items-center gap-[15px] border-t border-[#edf0e7] py-3 text-[13px] [&>span]:w-10 [&>span]:text-[11px] [&>span]:text-[#8c9c7e] [&>strong]:flex-1"
            key={item.document_id}
          >
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
        <section
          className="card article mb-5.5 rounded-[11px] border border-line bg-white px-10 py-8 max-lg:p-[25px] max-md:p-4.5 [&>.tags]:mb-5.5"
          key={item.document_id}
        >
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
          <details className="mt-5">
            <summary>查看与原文的差异</summary>
            <pre className="diff border-l-2 border-[#afc69d] text-xs">
              {draft.differences.find((d) => d.document_id === item.document_id)?.diff ||
                '没有正文差异'}
            </pre>
          </details>
        </section>
      ))}
      {pending && (
        <div className="review-actions [&_.check]:mb-5 [&>p]:mt-3.5 [&>p]:mb-0 card mb-5.5 rounded-[11px] border border-line bg-white p-6.5 max-md:p-4.5">
          {draft.requires_impact_confirmation && (
            <label className="check m-0 flex-row items-center gap-2 font-normal">
              <input
                type="checkbox"
                checked={confirmed}
                onChange={(e) => setConfirmed(e.target.checked)}
              />
              我已核对以上受影响文章，确认合并、拆分或归档范围
            </label>
          )}
          <div className="row flex items-center gap-3 wrap flex-wrap">
            {editing ? (
              <>
                <Button
                  disabled={busy}
                  onClick={() =>
                    void onEdit(operations)
                      .then(() => setEditing(false))
                      .catch(() => {})
                  }
                >
                  保存草稿修订
                </Button>
                <Button
                  className="secondary button-secondary"
                  onClick={() => {
                    setOperations(draft.operations);
                    setEditing(false);
                  }}
                >
                  取消编辑
                </Button>
              </>
            ) : (
              <>
                <Button
                  disabled={busy || (draft.requires_impact_confirmation && !confirmed)}
                  onClick={() => onPublish(confirmed)}
                >
                  确认并发布
                </Button>
                <Button
                  className="secondary button-secondary"
                  disabled={busy}
                  onClick={() => setEditing(true)}
                >
                  编辑草稿
                </Button>
                <Button
                  className="quiet button-quiet danger text-danger"
                  disabled={busy}
                  onClick={onReject}
                >
                  拒绝本次变更
                </Button>
              </>
            )}
          </div>
          <p className="muted text-muted small text-xs">
            发布会创建新版本。已有历史和来源引用保留；拒绝后 Agent 无法再自动发布这份草稿。
          </p>
        </div>
      )}
    </div>
  );
}
