import { useEffect, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { api, messageOf, send, useResource } from '../../shared/api/api';
import { useWorkspace } from '../../shared/state/workspace';
import { Button } from '../../shared/ui/Button';
import { ErrorBox, Loading } from '../../shared/ui/components';

type Preview = {
  version: string;
  eligible: boolean;
  blockers: string[];
  counts: { delete: number; keep: number };
  reasons: string[];
};
type Job = {
  id: string;
  target_kind: string;
  status: string;
  summary: { title: string; deleted: number; kept: number };
};

/** 删除确认基于当前依赖预览；请求只登记作业，页面离开后清理仍会继续。 */
export function AssetDeletionDialog({
  kind,
  identity,
  title,
  conversationId,
  onClose,
  onDeleted,
}: {
  kind: 'source' | 'delivery' | 'knowledge' | 'run';
  identity: string;
  title: string;
  conversationId?: string | null;
  onClose: () => void;
  onDeleted?: () => void;
}) {
  const { refresh } = useWorkspace();
  const [revision, setRevision] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const cancel = useRef<HTMLButtonElement>(null);
  const query = conversationId ? `?conversation_id=${encodeURIComponent(conversationId)}` : '';
  const base = `/api/assets/${kind}/${identity}`;
  const preview = useResource<Preview>(`${base}/deletion-preview${query}`, revision);
  const label = { source: '材料', delivery: '已交付文件', knowledge: '知识文章', run: '运行记录' }[
    kind
  ];
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    cancel.current?.focus();
    return () => previous?.focus();
  }, []);
  async function remove() {
    if (!preview.data?.eligible || preview.loading || busy) return;
    setBusy(true);
    setError('');
    try {
      await send(`${base}/deletion${query}`, { expected_version: preview.data.version });
      refresh();
      onClose();
      onDeleted?.();
    } catch (failure) {
      setError(messageOf(failure));
      setRevision((value) => value + 1);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-[#122e2460] p-4">
      <section
        className="max-h-[90dvh] w-full max-w-lg overflow-y-auto rounded-xl border border-line bg-white p-6 shadow-xl"
        role="dialog"
        aria-modal="true"
        aria-labelledby="asset-delete-title"
        onKeyDown={(event) => {
          if (event.key === 'Escape' && !busy) onClose();
          if (event.key !== 'Tab') return;
          const buttons = Array.from(
            event.currentTarget.querySelectorAll<HTMLButtonElement>('button:not(:disabled)'),
          );
          const first = buttons[0],
            last = buttons.at(-1);
          if (document.activeElement === (event.shiftKey ? first : last)) {
            event.preventDefault();
            (event.shiftKey ? last : first)?.focus();
          }
        }}
      >
        <h2 id="asset-delete-title" className="m-0 text-lg">
          删除{label}
        </h2>
        <p className="my-4 break-words">删除“{title}”。</p>
        {preview.loading ? (
          <Loading />
        ) : (
          preview.data && (
            <>
              <dl className="my-4 grid grid-cols-2 gap-3 rounded-lg bg-[#f5f7f1] p-4 text-sm">
                <div>
                  <dt className="text-muted">
                    {kind === 'run' ? '包含主任务与子任务' : '可回收的固定版本'}
                  </dt>
                  <dd className="m-0 mt-1 text-xl">{preview.data.counts.delete}</dd>
                </div>
                {kind !== 'run' && (
                  <div>
                    <dt className="text-muted">仍需保留的版本</dt>
                    <dd className="m-0 mt-1 text-xl">{preview.data.counts.keep}</dd>
                  </div>
                )}
              </dl>
              {preview.data.reasons.map((reason) => (
                <p key={reason} className="text-sm text-muted">
                  {reason}
                </p>
              ))}
              {preview.data.blockers.length > 0 && kind === 'run' && (
                <p className="text-sm text-danger">
                  任务尚未结束，或仍有等待回答、审批、执行及接续依赖。请先处理任务后再删除。
                </p>
              )}
              {kind !== 'run' &&
                preview.data.blockers.map((reason) => (
                  <p key={reason} className="text-sm text-danger">
                    {reason}
                  </p>
                ))}
              <p className="text-xs text-muted">
                确认后从列表移除，后台完成清理。若清理失败，可在对应页面的删除管理中重试。
              </p>
              {kind === 'delivery' && (
                <p className="text-xs text-muted">
                  这里只管理交付时的固定副本，工作区中的原工作文件另行管理。
                </p>
              )}
            </>
          )
        )}
        <ErrorBox message={error || preview.error} />
        <div className="mt-5 flex flex-wrap justify-end gap-3">
          <Button ref={cancel} className="button-secondary" disabled={busy} onClick={onClose}>
            取消
          </Button>
          <Button
            className="button-quiet"
            disabled={busy || preview.loading}
            onClick={() => setRevision((value) => value + 1)}
          >
            重新核对
          </Button>
          <Button
            className="button-secondary text-danger"
            disabled={busy || preview.loading || !preview.data?.eligible}
            onClick={() => void remove()}
          >
            {busy ? '正在登记…' : kind === 'delivery' ? '确认删除文件' : `确认删除${label}`}
          </Button>
        </div>
      </section>
    </div>
  );
}

/** 刷新浏览器后仍能找到未结束/失败的删除；只有存在运行作业时才轮询。 */
export function AssetDeletionHistory({ kind }: { kind?: 'knowledge' | 'run' } = {}) {
  const [params] = useSearchParams();
  const selected = params.get('deletion');
  const { version, refresh } = useWorkspace();
  const [tick, setTick] = useState(0);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState<string | null>(null);
  const result = useResource<Job[]>(
    `/api/asset-deletions${kind ? `?kind=${kind}` : ''}`,
    `${version}:${tick}`,
  );
  const selection = useResource<Job>(selected ? `/api/asset-deletions/${encodeURIComponent(selected)}` : null, `${version}:${tick}`);
  const selectedJob = selection.data && (kind ? selection.data.target_kind === kind : ['source', 'delivery', 'draft'].includes(selection.data.target_kind)) ? selection.data : null;
  const jobs = selectedJob ? [selectedJob, ...(result.data ?? []).filter((row) => row.id !== selectedJob.id)] : result.data;
  const pending = jobs?.some((row) => ['pending', 'running'].includes(row.status));
  useEffect(() => {
    if (!pending) return;
    const timer = setInterval(() => { if (document.visibilityState !== 'hidden') setTick((value) => value + 1); }, 1500);
    return () => clearInterval(timer);
  }, [pending]);
  if (!jobs?.length && !result.error && !selected) return null;
  return (
    <details
      className="mt-8"
      role="region"
      aria-label={kind ? '删除管理' : '最近的删除'}
      open={Boolean(selected) || !kind || result.data?.some((job) => job.status === 'failed') || undefined}
    >
      <summary className="cursor-pointer text-sm text-muted">
        {kind ? '删除管理' : '最近的删除'}
        {result.data?.some((job) => job.status === 'failed') ? ' · 有清理失败的记录' : ''}
      </summary>
      <ErrorBox message={error || result.error || selection.error} />
      {selected && selection.data && !selectedJob && <p className="text-sm text-muted">此删除记录属于其他管理页面，请从运行记录中的后台操作入口打开。</p>}
      {selection.loading && <Loading />}
      <div className="grid gap-2">
        {jobs?.map((job) => (
          <div
            key={job.id}
            aria-label={job.id === selected ? '选中的删除记录' : undefined}
            className="flex flex-wrap items-center gap-3 rounded-lg border border-line bg-white px-4 py-3 text-sm"
          >
            <div className="min-w-0 flex-1 break-words">
              <strong>{job.summary.title}</strong>
              <p className="m-0 mt-1 text-xs text-muted">
                {job.status === 'completed'
                  ? job.target_kind === 'run'
                    ? '已完成 · 聊天、独立成果和必要的接续信息已保留'
                    : job.target_kind === 'draft'
                      ? `已完成 · 回收 ${job.summary.deleted} 份临时草稿 · 保留 ${job.summary.kept} 份仍被使用的草稿`
                      : `已完成 · 回收 ${job.summary.deleted} 个版本 · 保留 ${job.summary.kept} 个仍被使用的版本`
                  : job.status === 'failed'
                    ? '清理未完成，可重试；已保留的引用不会被强制删除。'
                    : '正在清理，关闭页面后仍会继续。'}
              </p>
            </div>
            {job.status === 'failed' && (
              <Button
                className="button-secondary"
                disabled={busy === job.id}
                onClick={async () => {
                  setBusy(job.id);
                  setError('');
                  try {
                    await send(`/api/asset-deletions/${job.id}/retry`);
                    refresh();
                  } catch (failure) {
                    setError(messageOf(failure));
                  } finally {
                    setBusy(null);
                  }
                }}
              >
                重试清理
              </Button>
            )}
            <Button
              className="button-quiet"
              disabled={busy === job.id}
              onClick={async () => {
                setBusy(job.id);
                setError('');
                try {
                  await api(`/api/asset-deletions/${job.id}`);
                  refresh();
                } catch (failure) {
                  setError(messageOf(failure));
                } finally {
                  setBusy(null);
                }
              }}
            >
              刷新进度
            </Button>
          </div>
        ))}
      </div>
    </details>
  );
}
