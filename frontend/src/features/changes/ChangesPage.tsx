import { Button } from '../../shared/ui/Button';
import { useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { ArrowLeft, FileCheck2 } from 'lucide-react';
import { messageOf, send, usePagedResource, useResource } from '../../shared/api/api';
import {
  Badge,
  ChangeReview,
  Empty,
  ErrorBox,
  Loading,
  PageTitle,
} from '../../shared/ui/components';
import type { ChangeSet } from '../../shared/types/types';
import { useWorkspace } from '../../shared/state/workspace';
import { KnowledgeTabs } from '../knowledge/KnowledgeTabs';

export function ChangesPage() {
  const { version } = useWorkspace();
  const [status, setStatus] = useState('pending');
  const result = usePagedResource<ChangeSet>(`/api/changesets/page?status=${status}`, version);
  return (
    <>
      <PageTitle eyebrow="YOUR KNOWLEDGE, CONNECTED" title="知识库">
        <Link className="button secondary button-secondary" to="/runs">
          查看运行记录 →
        </Link>
      </PageTitle>
      <KnowledgeTabs />
      <p className="intro text-muted">在这里阅读草稿、修改内容并决定是否发布到知识库。</p>
      <label className="mb-5 max-w-xs">
        审阅状态
        <select value={status} onChange={(event) => setStatus(event.target.value)}>
          <option value="pending">待审草稿</option>
          <option value="published">已发布变更</option>
          <option value="rejected">已拒绝</option>
          <option value="">全部记录</option>
        </select>
      </label>
      <ErrorBox message={result.error} />
      {result.loading && !result.data.length ? (
        <Loading />
      ) : result.data?.length ? (
        <div className="list-cards grid gap-3">
          {result.data.map((draft) => (
            <Link
              to={`/knowledge/reviews/${draft.id}`}
              className="list-card flex items-center gap-[17px] rounded-[9px] border border-line bg-white px-[23px] py-4.5 text-[#5c7057] hover:border-[#aec2a5] hover:no-underline max-md:flex-wrap max-md:gap-3 max-md:p-4 [&>div]:min-w-0 [&>div]:flex-1 [&_h3]:mt-0 [&_h3]:mb-[5px] [&_h3]:text-[#344c37] [&_h3]:[overflow-wrap:anywhere] max-md:[&_h3]:text-[13px] [&_p]:m-0 [&_p]:text-[11px] [&_p]:text-[#85917e] [&_p]:[overflow-wrap:anywhere] [&>span:last-child]:text-[11px] [&>span:last-child]:whitespace-nowrap max-md:[&>span:last-child]:hidden"
              key={draft.id}
            >
              <FileCheck2 size={21} />
              <div>
                <h3>
                  {draft.content_removed
                    ? '已删除文章的发布回执'
                    : draft.operations[0]?.title || '知识维护变更'}
                </h3>
                <p>
                  {draft.content_removed ? '正文已清理' : `${draft.operations.length} 篇文章`} ·
                  修订 {draft.revision}
                  {draft.proactive ? ' · 主动建议' : ''}
                </p>
              </div>
              <Badge status={draft.status} />
              <span>查看 →</span>
            </Link>
          ))}
        </div>
      ) : (
        <Empty title="暂无知识变更">Agent 准备的草稿和你保存的编辑会显示在这里。</Empty>
      )}
      {result.next && (
        <Button
          className="secondary button-secondary"
          disabled={result.loading}
          onClick={result.loadMore}
        >
          {result.loading ? '正在加载…' : '加载更多变更'}
        </Button>
      )}
    </>
  );
}

export function ChangePage() {
  const { changesetId } = useParams();
  const { version, refresh } = useWorkspace();
  const result = useResource<ChangeSet>(`/api/changesets/${changesetId}`, version);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function action(path: string, body: unknown, method = 'POST') {
    setBusy(true);
    setError('');
    try {
      await send(path, body, method);
      refresh();
    } catch (error) {
      setError(messageOf(error));
      throw error;
    } finally {
      setBusy(false);
    }
  }
  const draft = result.data;
  return (
    <>
      <Link
        className="back-link mb-[15px] inline-flex items-center gap-1.5 text-xs"
        to="/knowledge/reviews"
      >
        <ArrowLeft size={15} />
        返回知识库审阅
      </Link>
      <PageTitle eyebrow="READ BEFORE PUBLISHING" title="审阅知识变更" />
      <ErrorBox message={error || result.error} />
      {draft?.retention_intent === 'temporary' && draft.status !== 'published' && (
        <div className="mb-5 flex flex-wrap items-center justify-between gap-3 rounded-lg border border-line bg-white p-4">
          <span className="text-sm text-muted">
            这份草稿属于原会话。独立保留后，删除会话也会保留草稿。
          </span>
          <Button
            className="secondary button-secondary"
            disabled={busy}
            onClick={() => {
              void action(`/api/changesets/${draft.id}/save`, {
                expected_revision: draft.revision,
              }).catch(() => {});
            }}
          >
            独立保留草稿
          </Button>
        </div>
      )}
      {draft?.content_removed && (
        <p className="rounded-lg border border-line p-4 text-sm text-muted">
          对应文章已删除，无引用的审阅正文已清理。仅保留发布身份，防止旧请求重复发布。
        </p>
      )}
      {draft?.retention_intent === 'saved' && !draft.content_removed && (
        <p className="text-sm text-muted">已独立保留，可在知识库的“草稿与审阅”中继续查看。</p>
      )}
      {result.error ? null : result.loading ? (
        <Loading />
      ) : (
        draft &&
        !draft.content_removed && (
          <ChangeReview
            key={`${draft.id}:${draft.revision}`}
            draft={draft}
            busy={busy}
            onPublish={async (confirm) => {
              await action(`/api/changesets/${draft.id}/publish`, {
                expected_revision: draft.revision,
                confirm_impact: confirm,
              }).catch(() => {});
            }}
            onReject={async () => {
              await action(`/api/changesets/${draft.id}/reject`, {
                expected_revision: draft.revision,
              }).catch(() => {});
            }}
            onEdit={async (operations) => {
              await action(
                `/api/changesets/${draft.id}`,
                { expected_revision: draft.revision, operations },
                'PUT',
              );
            }}
          />
        )
      )}
    </>
  );
}
